"""Clone/update the ZZZ-Model-Hash data repo into a local cache directory."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os
import shutil
import subprocess
import sys

REPO_URL = "https://github.com/hefengchang/ZZZ-Model-Hash.git"
CHANGELOG_NAME = "Hash变动日志.txt"
CHARACTERS_DIR_NAME = "角色hash表"
LOWVARM_REPO_URL = "https://github.com/hefengchang/ZZZ-Model-Hash_LowVarm.git"
LOWVARM_CHARACTERS_DIR_NAME = "角色hash表低显"

_TIMEOUT_SECONDS = 300
_SNIPPET_CHARS = 300

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class RepoError(RuntimeError):
    """Raised when the data repo cannot be cloned or updated."""


@dataclass(frozen=True)
class RepoVariant:
    """One upstream hash-data repo variant (2048p high graphics / 1024p low)."""

    key: str
    repo_url: str
    dir_name: str


REPO_VARIANTS: dict[str, RepoVariant] = {
    "2048p": RepoVariant("2048p", REPO_URL, "zzz-model-hash"),
    "1024p": RepoVariant("1024p", LOWVARM_REPO_URL, "zzz-model-hash_lowvarm"),
}
DEFAULT_VARIANT = "2048p"


def project_root() -> Path:
    """The project root: the executable's folder when frozen, else the package parent."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir(name: str) -> Path:
    """Path under the app-local data directory."""
    return project_root() / "data" / name


def default_cache_dir(variant: str = DEFAULT_VARIANT) -> Path:
    """Return the default cache directory inside the tool's data folder.

    Unknown variant keys raise KeyError; the variant table is fixed.
    """
    return data_dir(REPO_VARIANTS[variant].dir_name)


def changelog_path(repo_dir: Path) -> Path:
    """Return the path of the changelog file inside the repo."""
    return repo_dir / CHANGELOG_NAME


def characters_dir(repo_dir: Path) -> Path:
    """Return the per-character JSON directory inside the repo, auto-detected.

    The 2048p repo ships 角色hash表 and the 1024p repo ships 角色hash表低显;
    the standard name wins when both exist.  Raises ValueError when neither exists.
    """
    standard = repo_dir / CHARACTERS_DIR_NAME
    if standard.is_dir():
        return standard
    lowvarm = repo_dir / LOWVARM_CHARACTERS_DIR_NAME
    if lowvarm.is_dir():
        return lowvarm
    raise ValueError(
        f"no character hash table found in {repo_dir} (expected "
        f"{CHARACTERS_DIR_NAME!r} or {LOWVARM_CHARACTERS_DIR_NAME!r})"
    )


def ensure_repo(
    variant: str = DEFAULT_VARIANT,
    cache_dir: Path | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Ensure a local clone of the variant's data repo exists and is up to date.

    Missing or incomplete cache dirs get a fresh shallow clone; an unupdatable
    existing clone is kept as-is, and only a failed clone raises RepoError.
    """
    target = Path(cache_dir) if cache_dir is not None else default_cache_dir(variant)
    if target.is_dir() and (target / ".git").exists():
        try:
            _run_git(["-C", str(target), "pull", "--ff-only"])
            log(f"ZZZ-Model-Hash data updated: {target}")
        except RepoError as exc:
            log(f"Could not update ZZZ-Model-Hash data, keeping existing copy: {exc}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    log(f"Cloning ZZZ-Model-Hash data repo into {target} ...")
    _run_git(["clone", "--depth", "1", REPO_VARIANTS[variant].repo_url, str(target)])
    log(f"Cloned ZZZ-Model-Hash data into {target}")
    return target


def repo_head(cache_dir: Path) -> str:
    """Return the short HEAD SHA of the cloned repo, or "" on any failure."""
    return _run_git(
        ["-C", str(cache_dir), "rev-parse", "--short", "HEAD"], check=False
    ).strip()


def repo_update_available(
    variant: str = DEFAULT_VARIANT,
    cache_dir: Path | None = None,
) -> bool | None:
    """Whether the upstream repo has commits the local clone lacks.

    True when the upstream HEAD differs from the local HEAD (including
    nothing cloned yet), False when both agree, None when the check could not run.
    """
    target = Path(cache_dir) if cache_dir is not None else default_cache_dir(variant)
    if not (target.is_dir() and (target / ".git").exists()):
        return True
    try:
        advertisement = _run_git(["-C", str(target), "ls-remote", "origin", "HEAD"])
    except RepoError:
        return None
    remote_sha = (
        advertisement.split("\t", 1)[0].strip().lower()
        if advertisement.strip()
        else ""
    )
    if not remote_sha:
        return None
    local = _run_git(
        ["-C", str(target), "rev-parse", "HEAD"], check=False
    ).strip().lower()
    if not local:
        return None
    return remote_sha != local


@lru_cache(maxsize=1)
def _git_exe() -> str | None:
    """The resolved git executable path, or None when git is not installed."""
    return shutil.which("git")


def _run_git(args: list[str], check: bool = True) -> str:
    """Run a git command and return its stdout; raise RepoError on failure."""
    git = _git_exe()
    if git is None:
        if not check:
            return ""
        raise RepoError("git executable not found")
    try:
        result = subprocess.run(
            [git, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        if not check:
            return ""
        raise RepoError(
            f"git {args[-1]} timed out after {_TIMEOUT_SECONDS}s"
            f"{_output_snippet(exc.stdout, exc.stderr)}"
        ) from exc
    except OSError as exc:
        if not check:
            return ""
        raise RepoError(f"failed to run git: {exc}") from exc
    if result.returncode != 0:
        if not check:
            return ""
        raise RepoError(
            f"git {' '.join(args)} failed with exit code {result.returncode}"
            f"{_output_snippet(result.stdout, result.stderr)}"
        )
    return result.stdout


def _output_snippet(*outputs: str | bytes | None) -> str:
    """Format a short diagnostic snippet from subprocess output (str or bytes)."""

    def decode(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    text = "\n".join(decode(part) for part in outputs).strip()
    if not text:
        return ""
    if len(text) > _SNIPPET_CHARS:
        text = text[:_SNIPPET_CHARS] + "..."
    return f": {text}"
