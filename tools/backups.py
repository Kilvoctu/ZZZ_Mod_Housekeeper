"""App-local fix-backup store and backup-name helpers.

Fix backups live outside the mods folder, in one per-mods-folder store directory under the app-local backups dir whose tree mirrors the mods folder's tree keyed on the CANONICAL relative path: a leading "DISABLED_" is stripped from every DIRECTORY component (any level) while the FILENAME is never canonicalized, so one logical file keeps a single merged history across mod-manager toggles. New backups are named "{live name} -- {YYYY-MM-DD HH.MM.SS}.bak" (collisions get " (2)", " (3)" ... before ".bak") while legacy backups keep the "DISABLED_versionfix_<utc millis>-<name>" convention, and same-stamp chain ties order by that dup suffix, then filename.
Decoding a store path resolves the ACTUAL current file for the canonical key: the canonical live path (mods_dir / <canonical dirs> / <name>) when it exists; otherwise, for shallow trees (at most 4 directory components), the first existing path that re-prefixes "DISABLED_" onto a subset of those components; otherwise the canonical live path, which revert can still recreate.
"""

import os
import re
import shutil
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

from .repo import project_root

BACKUP_PREFIX = "DISABLED_versionfix_"
FOREIGN_BACKUP_PREFIX = "DISABLED_BACKUP_"
RABBITFX_BACKUP_PREFIX = "DISABLED_RABBITFXBACKUP_"
BACKUP_NAME_RE = re.compile(r"^DISABLED_versionfix_(?P<stamp>\d+)-(?P<name>.+)$")
STORE_BACKUP_NAME_RE = re.compile(
    r"^(?P<name>.+) -- (?P<when>\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})(?: \((?P<dup>\d+)\))?\.bak$"
)


@dataclass
class BackupChain:
    """All fix backups of one live .ini file, ascending by fix stamp."""

    live: Path
    backups: list[tuple[int, Path]]


def default_backups_dir() -> Path:
    """Default store: <project root>/backups (sibling of the data/ cache dir)."""
    return project_root() / "backups"


def folder_key(mods_dir: Path) -> str:
    """Stable per-mods-folder key: sanitized root name + 8-char digest.

    Non-[A-Za-z0-9_-] chars become "_" (empty name -> "mods"); the digest covers the mods path relative to the app root when it lives inside it, and the absolute path otherwise.
    """
    return _digest_key(mods_dir, _key_input(mods_dir))


def _key_input(mods_dir: Path) -> str:
    mods_dir = mods_dir.resolve()
    try:
        return mods_dir.relative_to(project_root().resolve()).as_posix()
    except ValueError:
        return str(mods_dir)


def _legacy_folder_key(mods_dir: Path) -> str:
    return _digest_key(mods_dir, str(Path(mods_dir).resolve()))


def _digest_key(mods_dir: Path, key_input: str) -> str:
    name = mods_dir.name.strip()
    if not name:
        name = "mods"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    return f"{sanitized}-{sha1(key_input.encode('utf-8')).hexdigest()[:8]}"


def store_root(store_dir: Path, mods_dir: Path) -> Path:
    """The store folder for one mods folder: store_dir / folder_key(mods_dir)."""
    migrate_store_key(store_dir, mods_dir)
    return Path(store_dir) / folder_key(mods_dir)


def migrate_store_key(store_dir: Path, mods_dir: Path) -> None:
    """Adopt the pre-relocation (absolute-key) store when it exists.

    Renames the legacy-key store folder to the current key when the current-key folder is absent; no-op otherwise.
    """
    legacy = Path(store_dir) / _legacy_folder_key(mods_dir)
    current = Path(store_dir) / folder_key(mods_dir)
    if legacy == current or not legacy.is_dir() or current.exists():
        return
    legacy.rename(current)


def store_folder_for_mod(store_dir: Path, mods_dir: Path, mod_path: Path) -> Path:
    """Store mirror folder for one mod directory (canonical DISABLED_-stripped key).

    Raises ValueError when mod_path is not strictly under mods_dir.
    """
    rel = Path(mod_path).relative_to(Path(mods_dir))
    return store_root(store_dir, mods_dir).joinpath(*_canonical_dirs(rel.parts))


def store_folder_to_open(store_dir: Path, mods_dir: Path, mod_path: Path) -> Path:
    """Folder to open for a mod's backups: the mod's store folder when it exists.

    Falls back to the store root, then the store dir; when none of them exist the mods folder is returned so the caller always opens something sensible.
    """
    store_dir = Path(store_dir)
    candidates = (
        store_folder_for_mod(store_dir, mods_dir, mod_path),
        store_root(store_dir, mods_dir),
        store_dir,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return Path(mods_dir)


def retarget_store_folder(
    store_dir: Path, mods_dir: Path, old_rel: Path, new_rel: Path
) -> bool:
    """Move the canonical store mirror folder for a renamed folder; True when moved.

    Relative paths are canonicalized like store keys; False when the source is absent, the target exists, or both paths coincide.
    """
    old_store = store_root(store_dir, mods_dir).joinpath(
        *_canonical_dirs(Path(old_rel).parts)
    )
    new_store = store_root(store_dir, mods_dir).joinpath(
        *_canonical_dirs(Path(new_rel).parts)
    )
    if old_store == new_store or not old_store.is_dir() or new_store.exists():
        return False
    new_store.parent.mkdir(parents=True, exist_ok=True)
    old_store.rename(new_store)
    return True


def delete_store_folder(store_dir: Path, mods_dir: Path, rel: Path) -> bool:
    """Delete the canonical store mirror folder for a removed mod or category."""
    mirror = store_root(store_dir, mods_dir).joinpath(
        *_canonical_dirs(Path(rel).parts)
    )
    if not mirror.is_dir():
        return False
    shutil.rmtree(mirror)
    return True


def prune_empty_store_folders(store_dir: Path, mods_dir: Path) -> int:
    """Remove every empty directory under the mods folder's store root; returns the count.

    Consumed backups leave their mirror directories behind; a bottom-up sweep deleting only truly empty directories (deepest first, store root itself never removed) cleans those leftovers without touching live backups, sibling mods, or other mods folders.
    """
    root = store_root(store_dir, Path(mods_dir))
    if not root.is_dir():
        return 0
    removed = 0
    folders = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for folder in folders:
        try:
            if any(folder.iterdir()):
                continue
            folder.rmdir()
        except OSError:
            continue
        removed += 1
    return removed


def prune_store_backups_newer_than(
    live: Path, chosen: Path, log: Callable[[str], None] = print
) -> int:
    """Delete store backups of ``live`` newer than ``chosen``; returns count dropped.

    Walks chosen.parent (the canonical store folder), decodes each sibling .bak via ``_decode_backup_name`` and unlinks those sharing ``live.name`` with a strictly greater (stamp, dup) ordering key; unparseable/foreign files and other live names stay, per-file failures are logged and never raised."""
    decoded = _decode_backup_name(Path(chosen).name)
    if decoded is None or decoded[1] != Path(live).name:
        return 0
    key = (decoded[0], decoded[2])
    dropped = 0
    for backup in _backup_candidates(chosen.parent):
        if backup == chosen:
            continue
        other = _decode_backup_name(backup.name)
        if other is None or other[1] != live.name or (other[0], other[2]) <= key:
            continue
        try:
            backup.unlink()
        except OSError as exc:
            log(f"could not drop stale backup {backup}: {exc}")
            continue
        dropped += 1
    return dropped


_DISABLED_TOGGLE = "DISABLED_"


def _canonical_dirs(dirs: Sequence[str]) -> tuple[str, ...]:
    """Directory components with a leading DISABLED_ stripped (canonical store key).

    This is the canonical form both store writes and store reads key on, so a file's history merges into one chain across mod-manager DISABLED_ toggles at any directory level. Only directories: the filename is never canonicalized (files named DISABLED_x.ini keep exact keying).
    """
    return tuple(part.removeprefix(_DISABLED_TOGGLE) for part in dirs)


def _resolve_live_path(mods_dir: Path, canonical_dirs: Sequence[str], name: str) -> Path:
    """The actual current file for a canonical store path (see module docstring).

    The canonical live path when it exists; otherwise, for at most 4 directory components, the first existing path that re-prefixes DISABLED_ onto a subset of them (masks 1..2^k-1 in ascending numeric order, so component 0 toggles first); otherwise the canonical live path, which may not exist (revert recreates it).
    """
    canonical = mods_dir.joinpath(*canonical_dirs, name)
    if canonical.exists():
        return canonical
    dirs = tuple(canonical_dirs)
    if 0 < len(dirs) <= 4:
        for mask in range(1, 1 << len(dirs)):
            toggled = tuple(
                _DISABLED_TOGGLE + part if (mask >> i) & 1 else part
                for i, part in enumerate(dirs)
            )
            candidate = mods_dir.joinpath(*toggled, name)
            if candidate.exists():
                return candidate
    return canonical


def backup_path_for(
    store_dir: Path, mods_dir: Path, live_path: Path, stamp: int
) -> Path:
    """Store destination for one backup: mirrors the canonical relpath of live_path."""
    mods_dir = Path(mods_dir)
    live_path = Path(live_path)
    try:
        rel = live_path.relative_to(mods_dir)
    except ValueError:
        raise ValueError(f"{live_path} is not under {mods_dir}") from None
    if not rel.parts:
        raise ValueError(f"{live_path} is not strictly under {mods_dir}")
    local = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    rel_dirs = rel.parent.parts
    store_folder = store_root(store_dir, mods_dir).joinpath(*_canonical_dirs(rel_dirs))
    destination = store_folder / f"{live_path.name} -- {local}.bak"
    dup = 1
    while destination.exists():
        dup += 1
        destination = store_folder / f"{live_path.name} -- {local} ({dup}).bak"
    return destination


def parse_store_backup_name(filename: str) -> tuple[int, str] | None:
    """Decode one new-scheme store backup filename.

    Returns (stamp millis, live filename) for a "{name} -- YYYY-MM-DD HH.MM.SS[ (dup)].bak" file, or None otherwise. The timestamp parses as local time (matching how it was written), so a DST fall-back duplicate maps to the same epoch second. Legacy DISABLED_versionfix_<stamp>-<name> files never match here; read them with BACKUP_NAME_RE.
    """
    match = STORE_BACKUP_NAME_RE.match(filename)
    if match is None:
        return None
    stamp = int(
        datetime.strptime(match.group("when"), "%Y-%m-%d %H.%M.%S").astimezone().timestamp() * 1000
    )
    return stamp, match.group("name")


def _decode_backup_name(filename: str) -> tuple[int, str, int] | None:
    """(stamp millis, live filename, dup index) under either scheme, else None.

    New-scheme names ("{name} -- when[ (n)].bak") parse as local time (a DST fall-back duplicate maps to the same epoch second); the dup index is the " (n)" suffix, 0 for a plain name. Legacy DISABLED_versionfix_<stamp>-<name> names parse via BACKUP_NAME_RE with dup index 0. None is returned when neither scheme matches (e.g. foreign backups), so callers skip those.
    """
    match = STORE_BACKUP_NAME_RE.match(filename)
    if match is not None:
        stamp = int(
            datetime.strptime(match.group("when"), "%Y-%m-%d %H.%M.%S").astimezone().timestamp() * 1000
        )
        dup = match.group("dup")
        return stamp, match.group("name"), int(dup) if dup is not None else 0
    match = BACKUP_NAME_RE.match(filename)
    if match is None:
        return None
    return int(match.group("stamp")), match.group("name"), 0


def _backup_candidates(store_dir: Path, recursive: bool = False) -> list[Path]:
    """*.ini and *.bak files in store_dir, sorted by string path.

    ``recursive`` walks subdirectories too (collect_backup_chains scans the whole store tree); the default lists direct children only (collect_backup_chains_for scans one file's canonical dir).
    """
    if recursive:
        found = (*store_dir.rglob("*.ini"), *store_dir.rglob("*.bak"))
    else:
        found = (*store_dir.glob("*.ini"), *store_dir.glob("*.bak"))
    return sorted(found, key=lambda item: str(item))


def is_backup_name(name: str) -> bool:
    """True for files starting with any backup convention (case-sensitive)."""
    return name.startswith(
        (BACKUP_PREFIX, FOREIGN_BACKUP_PREFIX, RABBITFX_BACKUP_PREFIX)
    )


def is_backup_dir_part(part: str) -> bool:
    """True for directory components starting with any backup convention."""
    return is_backup_name(part)


def is_backup_path(parts: tuple[str, ...]) -> bool:
    """True for a relative path whose filename or any directory component
    uses any backup convention."""
    return is_backup_name(parts[-1]) or any(
        is_backup_dir_part(part) for part in parts[:-1]
    )


def included_ini_files(root: Path) -> Iterator[Path]:
    """Every .ini under root that is not backup-named, sorted.

    Yields each *.ini under root recursively, sorted by string path, whose relative-path parts pass is_backup_path: the filename or any directory component starts with none of the backup conventions.
    """
    for path in sorted(root.rglob("*.ini"), key=lambda item: str(item)):
        if not is_backup_path(path.relative_to(root).parts):
            yield path


def move_file(src: Path, dst: Path) -> None:
    """Move src to dst, cross-volume safe.

    Tries os.replace first; when that fails (e.g. across volumes), creates dst's parent, copies src to dst (metadata kept, symlinks not followed) and removes src.
    """
    src = Path(src)
    dst = Path(dst)
    try:
        os.replace(src, dst)
    except OSError:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
        os.remove(src)


def collect_backup_chains(
    mods_dir: Path, store_dir: Path, subtree: Path | None = None
) -> dict[Path, BackupChain]:
    """Group backups in the store by resolved live file, optionally under a subtree."""
    mods_dir = Path(mods_dir)
    root = store_root(store_dir, mods_dir)
    if not root.is_dir():
        return {}
    subtree_root = mods_dir / subtree if subtree is not None else None
    groups: dict[Path, list[tuple[int, Path, int]]] = {}
    for backup in _backup_candidates(root, recursive=True):
        decoded = _decode_backup_name(backup.name)
        if decoded is None:
            continue
        stamp, name, dup = decoded
        canonical_dirs = _canonical_dirs(backup.parent.relative_to(root).parts)
        live = _resolve_live_path(mods_dir, canonical_dirs, name)
        if subtree_root is not None and not live.is_relative_to(subtree_root):
            continue
        groups.setdefault(live, []).append((stamp, backup, dup))
    return {
        live: BackupChain(
            live=live,
            backups=[
                (stamp, backup)
                for stamp, backup, _ in sorted(
                    entries,
                    key=lambda entry: (entry[0], entry[2], entry[1].name),
                )
            ],
        )
        for live, entries in groups.items()
    }


def collect_backup_chains_for(
    paths: Sequence[Path], mods_dir: Path, store_dir: Path
) -> dict[Path, BackupChain]:
    mods_dir = Path(mods_dir)
    root = store_root(store_dir, mods_dir)
    chains: dict[Path, BackupChain] = {}
    seen: set[Path] = set()
    for path in paths:
        path = Path(path)
        if path in seen:
            continue
        seen.add(path)
        rel = path.relative_to(mods_dir)
        entries: list[tuple[int, Path, int]] = []
        scan_dir = root.joinpath(*_canonical_dirs(rel.parent.parts))
        for backup in _backup_candidates(scan_dir):
            decoded = _decode_backup_name(backup.name)
            if decoded is None:
                continue
            stamp, name, dup = decoded
            if name != path.name:
                continue
            entries.append((stamp, backup, dup))
        if entries:
            chains[path] = BackupChain(
                live=path,
                backups=[
                    (stamp, backup)
                    for stamp, backup, _ in sorted(
                        entries,
                        key=lambda entry: (entry[0], entry[2], entry[1].name),
                    )
                ],
            )
    return chains
