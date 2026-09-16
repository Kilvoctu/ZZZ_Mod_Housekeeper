"""Extract mod zip/rar/7z archives into a mods folder."""

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

LogFn = Callable[[str], None]

# CREATE_NO_WINDOW suppresses console allocation for the 7z/Rar child
# (no window flash from the GUI); 0 is a no-op where the constant
# doesn't exist (non-Windows).
_SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def clean_member(name: str) -> str | None:
    """Normalize one zip member name to a safe relative forward-slash path."""
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    cleaned = name.replace("\\", "/")
    cleaned = cleaned.removeprefix("/")
    parts = [part for part in cleaned.split("/") if part and part != "."]
    if any(part == ".." for part in parts) or not parts:
        return None
    return "/".join(parts)


def _is_junk(member: str) -> bool:
    """True for macOS resource-fork entries dropped during extraction."""
    parts = member.split("/")
    if any(part == "__MACOSX" for part in parts):
        return True
    return parts[-1].lower() == ".ds_store"


def _top_component(member: str) -> str:
    """First ``/``-separated component of a cleaned member name."""
    return member.split("/")[0]


def _move_file(source: Path, target: Path) -> None:
    """Move one file via atomic rename, falling back to a copy on OSError."""
    try:
        os.replace(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _merge_staging(
    staging: Path,
    destination: Path,
    *,
    stem: str,
    replace: bool = False,
    log: LogFn | None = None,
) -> int:
    """Merge a staged extraction tree into destination; return files written.

    Applies the extract_zip layout rules: unsafe/junk members dropped, a
    multi-top tree wrapped under ``stem``, existing files skipped (logged when given) unless ``replace``; files are moved with os.replace, so a same-volume staging tree costs no extra read/write pass."""
    members: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(staging):
        for filename in filenames:
            relative = Path(dirpath).relative_to(staging) / filename
            cleaned = clean_member(relative.as_posix())
            if cleaned is None or _is_junk(cleaned):
                continue
            members.append(cleaned)
    if not members:
        return 0
    single_top = len({_top_component(member) for member in members}) == 1
    target_root = destination if single_top else destination / stem
    written = 0
    for member in members:
        target = target_root / Path(*member.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        source = staging / Path(*member.split("/"))
        if target.exists():
            if replace:
                _move_file(source, target)
                written += 1
            elif log:
                log(f"skip existing: {target}")
            continue
        _move_file(source, target)
        written += 1
    return written


def find_7z() -> str | None:
    """Path to 7-Zip's 7z.exe, or None when not installed."""
    candidates = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "7-Zip" / "7z.exe"),
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("7z")


def find_rar_tool() -> str | None:
    """Path to a WinRAR rar-capable executable, or None when not installed."""
    candidates = [
        r"C:\Program Files\WinRAR\Rar.exe",
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files\WinRAR\WinRAR.exe",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("Rar") or shutil.which("WinRAR") or shutil.which("UnRAR")


def _find_extractor() -> list[str] | None:
    """Command prefix for an installed archive extractor, or None."""
    tool = find_7z() or find_rar_tool()
    return [tool] if tool else None


def _extract_external(
    archive: Path,
    destination: Path,
    *,
    replace: bool = False,
    log: LogFn | None = None,
) -> int:
    """Extract a rar/7z archive via 7-Zip or WinRAR into a staging tree.

    Staging is created on the destination's volume so the merge is an atomic
    rename instead of a cross-drive copy; layout rules match extract_zip (junk dropped, single-top kept, no overwrite unless replace) and the return value is the number of files written."""
    tool = _find_extractor()
    if tool is None:
        raise RuntimeError(
            "Install 7-Zip or WinRAR to install .rar/.7z mod archives"
        )
    anchor = Path(destination).resolve().anchor
    staging = Path(tempfile.mkdtemp(prefix="zzz-extract-", dir=anchor or None))
    try:
        result = subprocess.run(
            [*tool, "x", str(archive), "-y", f"-o{staging}"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=_SUBPROCESS_FLAGS,
        )
        if result.returncode > 1:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(
                f"extraction failed: {detail[-1] if detail else result.returncode}"
            )
        return _merge_staging(
            staging, destination, stem=archive.stem, replace=replace, log=log
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def extract_zip(
    archive: Path,
    destination: Path,
    *,
    replace: bool = False,
    log: LogFn | None = None,
) -> int:
    """Extract one mod zip archive under destination; return files written.

    Existing files are skipped (logged when given) unless ``replace``; unsafe and
    junk members are dropped; raises zipfile.BadZipFile for non-zip input."""
    members: list[tuple[str, zipfile.ZipInfo]] = []
    with zipfile.ZipFile(archive) as opened:
        for info in opened.infolist():
            cleaned = clean_member(info.filename)
            if cleaned is None or _is_junk(cleaned):
                continue
            members.append((cleaned, info))
        if not members:
            return 0
        single_top = len({_top_component(cleaned) for cleaned, _info in members}) == 1
        target_root = destination if single_top else destination / Path(archive.stem)
        written = 0
        for cleaned, info in members:
            target = target_root / Path(*cleaned.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.is_dir() or cleaned.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists():
                if replace:
                    with opened.open(info) as src, open(target, "wb") as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
                    written += 1
                elif log:
                    log(f"skip existing: {target}")
                continue
            with opened.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            written += 1
        return written


def extract_archive(
    archive: Path,
    destination: Path,
    *,
    replace: bool = False,
    log: LogFn | None = None,
) -> int:
    """Extract a mod archive by type; zip uses stdlib, rar/7z uses an external tool."""
    suffix = archive.suffix.lower()
    if suffix == ".zip":
        return extract_zip(archive, destination, replace=replace, log=log)
    if suffix in (".rar", ".7z"):
        return _extract_external(archive, destination, replace=replace, log=log)
    raise ValueError(f"unsupported archive type: {suffix or '(none)'}")
