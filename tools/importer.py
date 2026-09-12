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


def clean_member(name: str) -> str | None:
    """Normalize one zip member name to a safe relative forward-slash path."""
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/"):
        cleaned = cleaned[1:]
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

    The staged tree is merged into destination with the same safety and
    layout rules as extract_zip (junk dropped, single-top kept, no overwrite
    unless replace).  Returns the number of files written.
    """
    tool = _find_extractor()
    if tool is None:
        raise RuntimeError(
            "Install 7-Zip or WinRAR to install .rar/.7z mod archives"
        )
    staging = Path(tempfile.mkdtemp(prefix="zzz-extract-"))
    try:
        result = subprocess.run(
            [*tool, "x", str(archive), "-y", f"-o{staging}"],
            capture_output=True,
            text=True,
        )
        if result.returncode > 1:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(
                f"extraction failed: {detail[-1] if detail else result.returncode}"
            )
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
        target_root = destination if single_top else destination / Path(archive.stem)
        written = 0
        for member in members:
            target = target_root / Path(*member.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            source = staging / Path(*member.split("/"))
            if target.exists():
                if replace:
                    target.write_bytes(source.read_bytes())
                    written += 1
                elif log:
                    log(f"skip existing: {target}")
                continue
            target.write_bytes(source.read_bytes())
            written += 1
        return written
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

    Existing files are skipped (logged when given) unless ``replace``;
    unsafe and junk members are dropped.  Raises zipfile.BadZipFile for
    non-zip input.
    """
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
                    target.write_bytes(opened.read(info))
                    written += 1
                elif log:
                    log(f"skip existing: {target}")
                continue
            target.write_bytes(opened.read(info))
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
