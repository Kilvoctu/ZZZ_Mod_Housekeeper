"""Remap bone indices in stride-32 blend .buf files bound by blend draw sections.

Game skeleton updates renumber bone indices; a mod's blend draw `hash =` line gets re-pointed to the current hash while its own .buf still carries the old numbering, so skinning breaks. This module ports VgRemapper's ini parsing: it finds the buffers bound by matching draw sections, remaps their per-vertex little-endian uint32 bone indices, and backs the original bytes up into the app's fix-backup store. The mappings are not involutions (68->127 while 127->126), so an applied marker in the store's "_blend_remaps.json" prevents double application from corrupting the data. The bound resource name is compared case-insensitively, a slight deliberate leniency over the C# reference's ordinal comparison.
"""

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .backups import backup_path_for, included_ini_files, store_root
from .fixer import read_ini_text
from .repo import data_dir

_HASH_LINE_RE = re.compile(r"^hash ?= ?(?P<h>[0-9A-Fa-f]{8})$", re.IGNORECASE)
_VB2_LINE_RE = re.compile(r"^vb2 ?= ?Resource(?P<name>.+)$", re.IGNORECASE)
_TYPE_LINE_RE = re.compile(r"^type ?= ?Buffer$", re.IGNORECASE)
_STRIDE_LINE_RE = re.compile(r"^stride ?= ?32$", re.IGNORECASE)
_FILENAME_LINE_RE = re.compile(r"^filename ?= ?(?P<file>.+)$", re.IGNORECASE)
_RECORD_STRIDE = 32
_RESOURCE_HEADER_PREFIX = "[Resource"
_DISABLED_TOGGLE = "DISABLED_"


@dataclass(frozen=True)
class BlendTables:
    """Parsed blend-remap dataset: per-blend-hash index tables plus position aliases."""

    mappings: dict[str, dict[int, int]]
    position_to_blend: dict[str, str]


@dataclass(frozen=True)
class BlendTarget:
    """One blend .buf to remap, with the resolved blend hash, table, and resource."""

    hash: str
    table: dict[int, int]
    resource: str
    path: Path


def blend_remaps_path() -> Path:
    """Static dataset: <project root>/data/blend_remaps.json."""
    return data_dir("blend_remaps.json")


def _decimal(value: object) -> int | None:
    """int for a decimal digit string, else None."""
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)


def load_blend_remaps(path: Path | None = None) -> BlendTables:
    """Parse the blend-remaps dataset into BlendTables; TypeError on bad structure."""
    source = Path(path) if path is not None else blend_remaps_path()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("blend remaps data is not an object")
    raw_mappings = data.get("mappings")
    raw_aliases = data.get("position_to_blend")
    if not isinstance(raw_mappings, dict) or not isinstance(raw_aliases, dict):
        raise TypeError("blend remaps data lacks mappings or position_to_blend")
    mappings: dict[str, dict[int, int]] = {}
    for hash_value, raw_table in raw_mappings.items():
        if not isinstance(raw_table, dict):
            raise TypeError(f"blend mapping {hash_value!r} is not an object")
        table: dict[int, int] = {}
        for raw_old, raw_new in raw_table.items():
            old = _decimal(raw_old)
            new = _decimal(raw_new)
            if old is None or new is None:
                raise ValueError(
                    f"blend mapping {hash_value!r} has a non-decimal entry "
                    f"{raw_old!r}: {raw_new!r}"
                )
            table[old] = new
        mappings[hash_value] = table
    position_to_blend: dict[str, str] = {}
    for key, raw_target in raw_aliases.items():
        if not isinstance(raw_target, str):
            raise TypeError(f"position_to_blend entry {key!r} is not a string")
        position_to_blend[key] = raw_target
    return BlendTables(mappings=mappings, position_to_blend=position_to_blend)


def resolve_blend_table(
    draw_hash: str, tables: BlendTables
) -> tuple[str, dict[int, int]] | None:
    """(blend hash, index table) for a draw hash, directly or via a position alias, else None."""
    hash_value = draw_hash.lower()
    table = tables.mappings.get(hash_value)
    if table is not None:
        return hash_value, table
    blend_hash = tables.position_to_blend.get(hash_value)
    if blend_hash is None:
        return None
    table = tables.mappings.get(blend_hash)
    if table is None:
        return None
    return blend_hash, table


def _ini_lines(ini_text: str) -> list[str]:
    """Trimmed non-empty non-comment lines (VgRemapper's GetIniLines)."""
    return [
        line
        for line in (raw.strip() for raw in ini_text.replace("\r\n", "\n").split("\n"))
        if line and not line.startswith(";")
    ]


def _resource_header_name(line: str) -> str | None:
    """Captured name of a "[Resource<name>]" line, else None."""
    if not line.startswith(_RESOURCE_HEADER_PREFIX) or not line.endswith("]"):
        return None
    return line[len(_RESOURCE_HEADER_PREFIX) : -1]


def find_targets(
    ini_text: str, ini_dir: Path, tables: BlendTables
) -> list[BlendTarget]:
    """Blend .buf targets bound by resolving draw sections in one ini text."""
    lines = _ini_lines(ini_text)
    blocks: list[tuple[str, str]] = []
    for index in range(len(lines)):
        name = _resource_header_name(lines[index])
        if name is None:
            continue
        rest = lines[index + 1 : index + 4]
        if len(rest) < 3:
            continue
        type_match = _TYPE_LINE_RE.match(rest[0])
        stride_match = _STRIDE_LINE_RE.match(rest[1])
        file_match = _FILENAME_LINE_RE.match(rest[2])
        if type_match is None or stride_match is None or file_match is None:
            continue
        blocks.append((name, file_match.group("file")))
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        hash_match = _HASH_LINE_RE.match(lines[index])
        resolved = (
            resolve_blend_table(hash_match.group("h"), tables)
            if hash_match is not None
            else None
        )
        if resolved is None:
            index += 1
            continue
        resolved_hash, _table = resolved
        found = None
        scan = index
        while scan < len(lines):
            next_line = lines[scan]
            if next_line.startswith("["):
                break
            vb2_match = _VB2_LINE_RE.match(next_line)
            if vb2_match is not None:
                found = vb2_match.group("name")
                break
            scan += 1
        index = scan + 1
        if found is not None:
            pairs.append((resolved_hash, found))
    targets: list[BlendTarget] = []
    seen: set[tuple[str, Path]] = set()
    for resolved_hash, resource_name in pairs:
        for header_name, filename in blocks:
            if header_name.lower() != resource_name.lower():
                continue
            path = Path(ini_dir) / filename
            if not path.exists():
                continue
            if (resolved_hash, path) in seen:
                continue
            seen.add((resolved_hash, path))
            targets.append(
                BlendTarget(
                    hash=resolved_hash,
                    table=tables.mappings[resolved_hash],
                    resource=resource_name,
                    path=path,
                )
            )
    return targets


def remap_bytes(data: bytes, table: dict[int, int]) -> tuple[bytes, int]:
    """Remapped bytes and changed-value count; a trailing partial record is untouched."""
    out = bytearray(data)
    changed = 0
    for base in range(0, (len(out) // _RECORD_STRIDE) * _RECORD_STRIDE, _RECORD_STRIDE):
        for k in range(4):
            offset = base + 16 + 4 * k
            old = int.from_bytes(out[offset : offset + 4], "little")
            new = table.get(old, old)
            if new != old:
                out[offset : offset + 4] = new.to_bytes(4, "little")
                changed += 1
    return bytes(out), changed


def scan_blend_targets(folder: Path, tables: BlendTables) -> list[BlendTarget]:
    """Deduped blend targets across every includable .ini under folder."""
    targets: list[BlendTarget] = []
    seen: set[tuple[str, Path]] = set()
    for ini in included_ini_files(Path(folder)):
        for target in find_targets(read_ini_text(ini), ini.parent, tables):
            key = (target.hash, target.path)
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
    return targets


def blend_state_path(store_dir: Path, mods_dir: Path) -> Path:
    """Marker file: <store root for mods_dir>/_blend_remaps.json."""
    return store_root(store_dir, mods_dir) / "_blend_remaps.json"


def load_blend_state(store_dir: Path, mods_dir: Path) -> dict[str, dict]:
    """Recorded blend markers keyed by canonical live path, {} when absent or unreadable."""
    try:
        return json.loads(
            blend_state_path(store_dir, mods_dir).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def blend_state_key(mods_dir: Path, path: Path) -> str:
    """Canonical marker key: DISABLED_-stripped relative dirs plus filename."""
    rel = Path(path).relative_to(Path(mods_dir))
    return "/".join(
        (
            *(part.removeprefix(_DISABLED_TOGGLE) for part in rel.parent.parts),
            rel.name,
        )
    )


def write_blend_state(store_dir: Path, mods_dir: Path, state: dict[str, dict]) -> None:
    """Persist the marker dict as the marker file (sorted keys, two-space indent)."""
    state_path = blend_state_path(store_dir, mods_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def apply_remap(
    target: BlendTarget,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None] = print,
    backup: bool = True,
) -> bool:
    """Remap one buffer once, recording the marker; True only when indices changed."""
    data = target.path.read_bytes()
    sha_now = sha256(data).hexdigest()
    state = load_blend_state(store_dir, mods_dir)
    key = blend_state_key(mods_dir, target.path)
    recorded = state.get(key)
    if isinstance(recorded, dict) and recorded.get("after") == sha_now:
        return False
    new, changed = remap_bytes(data, target.table)
    stamp = int(time.time() * 1000)
    destination = None
    if changed and backup:
        destination = backup_path_for(store_dir, mods_dir, target.path, stamp)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    state[key] = {
        "after": sha256(new).hexdigest(),
        "before": sha_now,
        "hash": target.hash,
        "stamp": stamp,
        "action": "table",
        "source": "",
    }
    write_blend_state(store_dir, mods_dir, state)
    if changed:
        target.path.write_bytes(new)
        message = (
            f"remapped blend indices: {target.path.name} ({target.hash}) — "
            f"{changed} index values changed"
        )
        if destination is not None:
            message += f" (backup: {destination.name})"
        log(message)
        return True
    log(f"blend indices already current: {target.path.name} ({target.hash})")
    return False


def rewrite_blend_state_keys(
    store_dir: Path, mods_dir: Path, old_prefix: str, new_prefix: str
) -> int:
    """Rewrite applied-remap marker keys after a folder rename; returns the count changed.

    Keys equal to old_prefix or under it swap that prefix; no-op without a marker file; a rewritten key overwrites a colliding existing key.
    """
    state = load_blend_state(store_dir, mods_dir)
    if not state or not old_prefix or old_prefix == new_prefix:
        return 0
    rewritten = dict(state)
    changed = 0
    for key, marker in state.items():
        if key == old_prefix:
            new_key = new_prefix
        elif key.startswith(old_prefix + "/"):
            new_key = new_prefix + key[len(old_prefix) :]
        else:
            continue
        del rewritten[key]
        rewritten[new_key] = marker
        changed += 1
    if changed:
        write_blend_state(store_dir, mods_dir, rewritten)
    return changed


def remove_blend_state_keys(store_dir: Path, mods_dir: Path, prefix: str) -> int:
    """Remove applied-remap marker keys under a deleted folder; returns count removed."""
    state = load_blend_state(store_dir, mods_dir)
    if not state or not prefix:
        return 0
    kept: dict[str, dict] = {}
    removed = 0
    for key, marker in state.items():
        if key == prefix or key.startswith(prefix + "/"):
            removed += 1
        else:
            kept[key] = marker
    if removed:
        write_blend_state(store_dir, mods_dir, kept)
    return removed


def blend_marker_kind(store_dir: Path, mods_dir: Path, live_path: Path) -> str | None:
    """Fix-kind annotation for a live blend from its marker; None without one.

    "blend remap (vote)" marks a buffer remapped from dump data by the vote pass, "blend remap (table)" one remapped from the shipped tables; a legacy marker without an "action" field predates the distinction and counts as a table remap.
    """
    marker = load_blend_state(store_dir, mods_dir).get(
        blend_state_key(mods_dir, live_path)
    )
    if not isinstance(marker, dict):
        return None
    return (
        "blend remap (vote)"
        if marker.get("action") in ("vote", "grid")
        else "blend remap (table)"
    )


def prune_blend_markers(
    store_dir: Path, mods_dir: Path, live_paths: Iterable[Path]
) -> int:
    """Drop markers of blends a revert put back to pre-remap bytes; returns the count dropped.

    A marker is dropped when its live .buf exists again with exactly the content the marker's "before" hash recorded, i.e. nothing remapped remains on disk; non-.buf paths are ignored, unreadable live files are skipped, and the state file is rewritten at most once, only when something was dropped.
    """
    state = load_blend_state(store_dir, mods_dir)
    if not state:
        return 0
    dropped = 0
    for live in live_paths:
        live = Path(live)
        if live.suffix != ".buf":
            continue
        key = blend_state_key(mods_dir, live)
        marker = state.get(key)
        if not isinstance(marker, dict):
            continue
        try:
            data = live.read_bytes()
        except OSError:
            continue
        if sha256(data).hexdigest() != marker.get("before"):
            continue
        del state[key]
        dropped += 1
    if dropped:
        write_blend_state(store_dir, mods_dir, state)
    return dropped
