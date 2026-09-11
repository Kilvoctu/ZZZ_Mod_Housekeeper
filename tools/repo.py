"""Fetch/refresh the ZZZ-Model-Hash data into a local cache directory."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO_URL = "https://github.com/hefengchang/ZZZ-Model-Hash.git"
CHANGELOG_NAME = "Hash变动日志.txt"
CHARACTERS_DIR_NAME = "角色hash表"
LOWVARM_REPO_URL = "https://github.com/hefengchang/ZZZ-Model-Hash_LowVarm.git"
LOWVARM_CHARACTERS_DIR_NAME = "角色hash表低显"

_TIMEOUT_SECONDS = 300
_MARKER_NAME = ".zzzhashfix.json"
_USER_AGENT = "ZZZHashFix/1.0"


class RepoError(RuntimeError):
    """Raised when the data repos cannot be downloaded or unpacked."""


@dataclass(frozen=True)
class RepoVariant:
    """One upstream hash-data repo variant (2048p high graphics / 1024p low)."""

    key: str
    repo_url: str
    dir_name: str
    repo_name: str
    branch: str


REPO_VARIANTS: dict[str, RepoVariant] = {
    "2048p": RepoVariant("2048p", REPO_URL, "zzz-model-hash", "ZZZ-Model-Hash", "master"),
    "1024p": RepoVariant("1024p", LOWVARM_REPO_URL, "zzz-model-hash_lowvarm", "ZZZ-Model-Hash_LowVarm", "main"),
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
    """Ensure a local copy of the variant's data repo exists and is up to date.

    Existing copies are refreshed from the upstream archive (an ETag check skips
    redundant downloads); an unrefreshable copy is kept as-is, and only a failed
    first download raises RepoError.
    """
    target = Path(cache_dir) if cache_dir is not None else default_cache_dir(variant)
    if target.is_dir() and (target / _MARKER_NAME).is_file():
        try:
            remote = _head_etag(_archive_url(variant))
            if remote and remote == _read_marker(target).get("etag"):
                return target
            _download_archive(variant, target)
        except RepoError as exc:
            log(f"Could not update ZZZ-Model-Hash data, keeping existing copy: {exc}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading ZZZ-Model-Hash data into {target} ...")
    try:
        _download_archive(variant, target)
    except RepoError:
        _remove_tree(target)
        raise
    log(f"Downloaded ZZZ-Model-Hash data into {target}")
    return target


def repo_head(cache_dir: Path) -> str:
    """Return the commit SHA the cache was fetched at, or \"\" when never fetched."""
    return _read_marker(cache_dir).get("sha", "")


def repo_update_available(
    variant: str = DEFAULT_VARIANT,
    cache_dir: Path | None = None,
) -> bool | None:
    """Whether the upstream repo has commits the local copy lacks.

    True when the upstream ETag differs from the cached one (including nothing
    fetched yet), False when both agree, None when the check could not run.
    """
    target = Path(cache_dir) if cache_dir is not None else default_cache_dir(variant)
    if not (target.is_dir() and (target / _MARKER_NAME).is_file()):
        return True
    try:
        remote = _head_etag(_archive_url(variant))
    except RepoError:
        return None
    if not remote:
        return None
    local = _read_marker(target).get("etag")
    if not local:
        return True
    return remote != local


def _archive_url(variant: str) -> str:
    """codeload zip archive URL for the variant's default branch."""
    info = REPO_VARIANTS[variant]
    return (
        f"https://codeload.github.com/hefengchang/{info.repo_name}/"
        f"zip/refs/heads/{info.branch}"
    )


def _download_archive(variant: str, target: Path) -> None:
    """Download and unpack the variant's tip archive into ``target``."""
    staging = Path(
        tempfile.mkdtemp(prefix=f"{target.name}-staging-", dir=str(target.parent))
    )
    try:
        body, etag = _fetch_url(_archive_url(variant))
        _extract_archive(body, staging)
        _replace_dir(target, staging)
        _write_marker(target, etag=etag, sha=_head_sha(variant))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _fetch_url(url: str, timeout: int = _TIMEOUT_SECONDS) -> tuple[bytes, str | None]:
    """GET url and return (body, ETag header); raise RepoError on any failure."""
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            return body, response.headers.get("ETag")
    except (URLError, OSError) as exc:
        raise RepoError(f"network request failed: {exc}") from None


def _head_etag(url: str, timeout: int = _TIMEOUT_SECONDS) -> str | None:
    """Return the ETag header for url, or None; raise RepoError on failure."""
    request = Request(url, headers={"User-Agent": _USER_AGENT}, method="HEAD")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.headers.get("ETag")
    except (URLError, OSError) as exc:
        raise RepoError(f"network request failed: {exc}") from None


def _head_sha(variant: str) -> str:
    """The upstream default-branch commit SHA, or \"\" on any failure."""
    info = REPO_VARIANTS[variant]
    url = (
        f"https://api.github.com/repos/hefengchang/{info.repo_name}/"
        f"commits/{info.branch}"
    )
    try:
        body, _etag = _fetch_url(url, timeout=_TIMEOUT_SECONDS)
        sha = json.loads(body.decode("utf-8")).get("sha")
        return str(sha) if sha else ""
    except (RepoError, ValueError):
        return ""


def _extract_archive(archive: bytes, destination: Path) -> None:
    """Unpack a GitHub zip archive, dropping its single top-level folder."""
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        members = zf.namelist()
        if not members:
            raise RepoError("downloaded archive is empty")
        top = members[0].split("/", 1)[0]
        prefix = top + "/"
        destination.mkdir(parents=True, exist_ok=True)
        for member in members:
            if not member.startswith(prefix):
                continue
            rel = member[len(prefix):]
            if not rel:
                continue
            rel_parts = rel.split("/")
            if ".." in rel_parts:
                raise RepoError("archive member escapes the target directory")
            if member.endswith("/"):
                destination.joinpath(*rel_parts).mkdir(parents=True, exist_ok=True)
                continue
            dest = destination.joinpath(*rel_parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member))


def _replace_dir(target: Path, staging: Path) -> None:
    """Swap staging into place, replacing any existing target."""
    if target.exists():
        shutil.rmtree(target)
    os.replace(staging, target)


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _read_marker(cache_dir: Path) -> dict[str, str]:
    try:
        raw = json.loads((cache_dir / _MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: str(value) for key, value in raw.items() if value is not None}


def _write_marker(
    cache_dir: Path, etag: str | None = None, sha: str = ""
) -> None:
    marker: dict[str, str] = {"etag": etag} if etag else {}
    if sha:
        marker["sha"] = sha
    (cache_dir / _MARKER_NAME).write_text(
        json.dumps(marker, indent=2), encoding="utf-8"
    )
