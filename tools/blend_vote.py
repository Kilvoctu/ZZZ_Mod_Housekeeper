"""Derive an old→new blend-index map by voting over position-matched records.

Bone indices drift between game versions: a mod authored against an older
skeleton still carries the old bone numbering, while a dump of the same mesh
uses the new one. Matching each mod vertex's position record to a dump vertex
pairs the vertices, and comparing each pair's blend records casts one old→new
vote per disagreeing index slot.

The derivation refuses to guess: derive_blend_vote_map() returns an empty map
with a refused report unless strict alignment, count, ambiguity, consistency,
injectivity, and match-rate gates all pass, and it never writes anything.
scan_blend_vote_targets() pairs the same buffers in mod .inis against
dump-covered components (skipping hashes the table-driven remap owns);
apply_blend_vote_remap() rewrites the blend buffer from a voted map and
records a marker in the shared "_blend_remaps.json" used by the table pass.
"""

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .backups import backup_path_for, included_ini_files
from .blend_remap import (
    BlendTables,
    blend_state_key,
    load_blend_state,
    remap_bytes,
    resolve_blend_table,
    write_blend_state,
)
from .dumpdata import DumpData
from .fixer import read_ini_text

_BLEND_STRIDE = 32
_POSITION_STRIDE = 40
_MIN_MATCH_RATE = 0.99
_RESOURCE_HEADER_PREFIX = "[Resource"
_HASH_LINE_RE = re.compile(r"^hash ?= ?(?P<h>[0-9A-Fa-f]{8})$", re.IGNORECASE)
_VB0_LINE_RE = re.compile(r"^vb0 ?= ?Resource(?P<name>.+)$", re.IGNORECASE)
_VB2_LINE_RE = re.compile(r"^vb2 ?= ?Resource(?P<name>.+)$", re.IGNORECASE)
_TYPE_LINE_RE = re.compile(r"^type ?= ?Buffer$", re.IGNORECASE)
_STRIDE_LINE_RE = re.compile(r"^stride ?= ?(?P<stride>\d+)$", re.IGNORECASE)
_FILENAME_LINE_RE = re.compile(r"^filename ?= ?(?P<file>.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class VoteReport:
    """Outcome of one derivation attempt.

    ``total`` is the mod vertex count, ``matched``/``unmatched``/``ambiguous``
    partition it by how each mod vertex's position record was found in the dump.
    ``conflicts`` maps an old index to the disagreeing new indices it was voted
    to (empty when ``ok``). ``map`` carries the derived old→new indices only
    when ``ok``; ``refusal`` is "" when ``ok``, else a short reason.
    """

    ok: bool
    total: int
    matched: int
    unmatched: int
    ambiguous: int
    conflicts: dict[int, frozenset[int]]
    map: dict[int, int]
    refusal: str


def _refusal_report(
    total: int,
    matched: int,
    unmatched: int,
    ambiguous: int,
    conflicts: dict[int, frozenset[int]],
    refusal: str,
) -> VoteReport:
    """An ok=False report with an empty map: a refused derivation writes nothing."""
    return VoteReport(
        ok=False,
        total=total,
        matched=matched,
        unmatched=unmatched,
        ambiguous=ambiguous,
        conflicts=conflicts,
        map={},
        refusal=refusal,
    )


def _vertex_count(buffer: bytes, stride: int) -> int | None:
    """Vertex count when the buffer length is stride-aligned, else None."""
    if len(buffer) % stride:
        return None
    return len(buffer) // stride


def _record_index(dump_position: bytes, count: int) -> dict[bytes, list[int]]:
    """Dump vertex indices keyed by their full 40-byte position record.

    Records appearing on several dump vertices keep every occurrence, so the
    caller can detect ambiguity instead of guessing a partner.
    """
    index: dict[bytes, list[int]] = {}
    for vertex in range(count):
        base = vertex * _POSITION_STRIDE
        index.setdefault(dump_position[base : base + _POSITION_STRIDE], []).append(
            vertex
        )
    return index


def _aligned_vertex_counts(
    mod_blend: bytes,
    mod_position: bytes,
    dump_blend: bytes,
    dump_position: bytes,
) -> tuple[tuple[int, int, int, int], str]:
    """All four stride-32/40 vertex counts, or a refusal reason for the first misaligned buffer."""
    specs = (
        ("mod blend", mod_blend, _BLEND_STRIDE),
        ("mod position", mod_position, _POSITION_STRIDE),
        ("dump blend", dump_blend, _BLEND_STRIDE),
        ("dump position", dump_position, _POSITION_STRIDE),
    )
    counts: list[int] = []
    for what, buf, stride in specs:
        count = _vertex_count(buf, stride)
        if count is None:
            return (0, 0, 0, 0), f"{what} length {len(buf)} is not a multiple of {stride}"
        counts.append(count)
    return (counts[0], counts[1], counts[2], counts[3]), ""


def derive_blend_vote_map(
    mod_blend: bytes, mod_position: bytes, dump_blend: bytes, dump_position: bytes
) -> tuple[dict[int, int], VoteReport]:
    """(map, report) voted from position-matched blend records; ({}, refusal) when the derivation refuses.

    ``mod_blend``/``dump_blend`` hold stride-32 blend records (4 float32
    weights then 4 little-endian uint32 bone indices); ``mod_position``/
    ``dump_position`` hold stride-40 position records of the same mesh on both
    sides. The map is empty when a strict gate fails: a stride-misaligned
    length, mismatched mod/dump vertex counts, zero vertices, a mod record
    matching several dump records (ambiguous), an old index voted to several
    new indices, a non-injective result, or a match rate below 0.99.
    """
    (mod_count, mod_positions, dump_count, dump_positions), refusal = (
        _aligned_vertex_counts(mod_blend, mod_position, dump_blend, dump_position)
    )
    if refusal:
        return {}, _refusal_report(0, 0, 0, 0, {}, refusal)
    if mod_count != mod_positions:
        return {}, _refusal_report(
            mod_count,
            0,
            0,
            0,
            {},
            f"mod blend has {mod_count} vertices but mod position has {mod_positions}",
        )
    if dump_count != dump_positions:
        return {}, _refusal_report(
            mod_count,
            0,
            0,
            0,
            {},
            f"dump blend has {dump_count} vertices but dump position has {dump_positions}",
        )
    if mod_count != dump_count:
        return {}, _refusal_report(
            mod_count,
            0,
            0,
            0,
            {},
            f"mod and dump vertex counts differ ({mod_count} vs {dump_count})",
        )
    if mod_count == 0:
        return {}, _refusal_report(0, 0, 0, 0, {}, "no vertices to vote on")

    candidates = _record_index(dump_position, dump_count)
    votes: dict[int, set[int]] = {}
    matched = 0
    unmatched = 0
    ambiguous = 0
    for vertex in range(mod_count):
        base = vertex * _POSITION_STRIDE
        found = candidates.get(mod_position[base : base + _POSITION_STRIDE])
        if not found:
            unmatched += 1
            continue
        if len(found) > 1:
            ambiguous += 1
            continue
        matched += 1
        mod_base = vertex * _BLEND_STRIDE + 16
        dump_base = found[0] * _BLEND_STRIDE + 16
        for slot in range(4):
            offset = 4 * slot
            old = int.from_bytes(
                mod_blend[mod_base + offset : mod_base + offset + 4], "little"
            )
            new = int.from_bytes(
                dump_blend[dump_base + offset : dump_base + offset + 4], "little"
            )
            if old != new:
                votes.setdefault(old, set()).add(new)

    conflicts = {
        old: frozenset(news) for old, news in sorted(votes.items()) if len(news) > 1
    }
    derived = {
        old: next(iter(news)) for old, news in sorted(votes.items()) if len(news) == 1
    }
    if matched / mod_count < _MIN_MATCH_RATE:
        return {}, _refusal_report(
            mod_count,
            matched,
            unmatched,
            ambiguous,
            conflicts,
            f"match rate {matched}/{mod_count} below {_MIN_MATCH_RATE}",
        )
    if conflicts:
        first_old = min(conflicts)
        return {}, _refusal_report(
            mod_count,
            matched,
            unmatched,
            ambiguous,
            conflicts,
            f"conflicting votes: old index {first_old} voted to "
            f"{sorted(conflicts[first_old])}",
        )
    seen: dict[int, int] = {}
    for old, new in sorted(derived.items()):
        collision = seen.get(new)
        if collision is not None:
            return {}, _refusal_report(
                mod_count,
                matched,
                unmatched,
                ambiguous,
                conflicts,
                f"non-injective mapping (old {collision} and old {old} both map to "
                f"new {new})",
            )
        seen[new] = old
    return derived, VoteReport(
        ok=True,
        total=mod_count,
        matched=matched,
        unmatched=unmatched,
        ambiguous=ambiguous,
        conflicts={},
        map=derived,
        refusal="",
    )


@dataclass(frozen=True)
class BlendVoteTarget:
    """One blend .buf to vote-remap, with its bound position buffer and dump sources."""

    hash: str
    resource: str
    blend_path: Path
    position_path: Path
    dump_blend: Path
    dump_position: Path
    source: str


@dataclass(frozen=True)
class _IniSection:
    """One [...] section's first hash/vb0/vb2 lines plus its Buffer declaration."""

    name: str
    hash: str | None
    vb0: str | None
    vb2: str | None
    is_resource: bool
    buffer: bool
    stride: int | None
    filename: str


def _ini_lines(text: str) -> list[str]:
    """Trimmed non-empty non-comment lines."""
    return [
        line
        for line in (raw.strip() for raw in text.replace("\r\n", "\n").split("\n"))
        if line and not line.startswith(";")
    ]


def _resource_header_name(line: str) -> str | None:
    """Captured name of a "[Resource<name>]" line, else None."""
    if not line.startswith(_RESOURCE_HEADER_PREFIX) or not line.endswith("]"):
        return None
    return line[len(_RESOURCE_HEADER_PREFIX) : -1]


def _scan_sections(lines: list[str]) -> list[_IniSection]:
    """One pass over the ini lines recording every [...] section it opens.

    Each section keeps its first ``hash =`` line and first ``vb0``/``vb2``
    Resource binds; ``[Resource…]`` sections additionally keep whether a
    ``type = Buffer`` line appeared and their first ``stride``/``filename``
    values, in either order.
    """
    sections: list[_IniSection] = []
    name = ""
    is_resource = False
    hash_value: str | None = None
    vb0: str | None = None
    vb2: str | None = None
    is_buffer = False
    stride: int | None = None
    filename = ""

    def flush() -> None:
        if not name:
            return
        sections.append(
            _IniSection(
                name=name,
                hash=hash_value,
                vb0=vb0,
                vb2=vb2,
                is_resource=is_resource,
                buffer=is_buffer,
                stride=stride,
                filename=filename,
            )
        )

    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            flush()
            resource_name = _resource_header_name(line)
            name = resource_name if resource_name is not None else line[1:-1]
            is_resource = resource_name is not None
            hash_value = None
            vb0 = None
            vb2 = None
            is_buffer = False
            stride = None
            filename = ""
            continue
        if hash_value is None:
            hash_match = _HASH_LINE_RE.match(line)
            if hash_match is not None:
                hash_value = hash_match.group("h").lower()
        if vb0 is None:
            bind_match = _VB0_LINE_RE.match(line)
            if bind_match is not None:
                vb0 = bind_match.group("name")
        if vb2 is None:
            bind_match = _VB2_LINE_RE.match(line)
            if bind_match is not None:
                vb2 = bind_match.group("name")
        if not is_resource:
            continue
        if _TYPE_LINE_RE.match(line) is not None:
            is_buffer = True
        if stride is None:
            stride_match = _STRIDE_LINE_RE.match(line)
            if stride_match is not None:
                stride = int(stride_match.group("stride"))
        if not filename:
            file_match = _FILENAME_LINE_RE.match(line)
            if file_match is not None:
                filename = file_match.group("file").strip()
    flush()
    return sections


def _bind_block(
    bind_name: str, blocks: list[_IniSection], stride: int
) -> _IniSection | None:
    """The bind's Resource block when it is a Buffer of the wanted stride with a filename."""
    lowered = bind_name.lower()
    for block in blocks:
        if block.name.lower() != lowered:
            continue
        if block.buffer and block.stride == stride and block.filename:
            return block
        return None
    return None


def _normalize_path(path: Path) -> Path:
    """Lexically normalized path (collapses "." and ".." parts without touching the disk)."""
    return Path(os.path.normpath(str(path)))


def _file_vertex_count(path: Path, stride: int) -> int | None:
    """Vertex count by file size, or None when the file cannot be stat'ed."""
    try:
        return Path(path).stat().st_size // stride
    except OSError:
        return None


def _dump_key2_for_hash(dumps: DumpData, section_hash: str) -> tuple[str, str] | None:
    """(char, comp) a section hash resolves to: by index buffer, else by role hash.

    The dump's index-buffer hash identifies the mesh first; otherwise the
    first sorted position/blend component whose current hash matches wins.
    None when the dump covers the hash neither way.
    """
    value = section_hash.lower()
    ib_matches = sorted(key2 for key2, ib in dumps.ibs.items() if ib == value)
    if ib_matches:
        return ib_matches[0]
    role_matches = sorted(
        key3
        for key3, current in dumps.current_hashes.items()
        if key3[2] in ("position", "blend") and current == value
    )
    return (role_matches[0][0], role_matches[0][1]) if role_matches else None


def _vote_targets_in_ini(
    ini_text: str, ini_path: Path, dumps: DumpData, tables: BlendTables
) -> list[BlendVoteTarget]:
    """Vote targets from one ini's draw sections pairing position and blend buffers.

    A section is skipped unless the dump covers its hash and both buffers align
    to their strides and match the dump's vertex counts; shipped-table hashes
    belong to the table-driven pass.
    """
    ini_dir = Path(ini_path).parent
    sections = _scan_sections(_ini_lines(ini_text))
    blocks = [section for section in sections if section.is_resource]
    targets: list[BlendVoteTarget] = []
    for section in sections:
        if section.is_resource or section.hash is None:
            continue
        if section.vb0 is None or section.vb2 is None:
            continue
        position_block = _bind_block(section.vb0, blocks, _POSITION_STRIDE)
        blend_block = _bind_block(section.vb2, blocks, _BLEND_STRIDE)
        if position_block is None or blend_block is None:
            continue
        position_path = _normalize_path(ini_dir / position_block.filename)
        blend_path = _normalize_path(ini_dir / blend_block.filename)
        if not position_path.exists() or not blend_path.exists():
            continue
        key2 = _dump_key2_for_hash(dumps, section.hash)
        if key2 is None:
            continue
        dump_blend_path = dumps.binaries.get((*key2, "blend"))
        dump_position_path = dumps.binaries.get((*key2, "position"))
        if dump_blend_path is None or dump_position_path is None:
            continue
        blend_layout = dumps.layouts.get((*key2, "blend"))
        position_layout = dumps.layouts.get((*key2, "position"))
        if blend_layout is None or blend_layout.stride != _BLEND_STRIDE:
            continue
        if position_layout is None or position_layout.stride != _POSITION_STRIDE:
            continue
        if resolve_blend_table(section.hash, tables) is not None:
            continue  # the table-driven remap pass owns this hash
        dump_blend_count = _file_vertex_count(dump_blend_path, _BLEND_STRIDE)
        dump_position_count = _file_vertex_count(dump_position_path, _POSITION_STRIDE)
        mod_blend_count = _file_vertex_count(blend_path, _BLEND_STRIDE)
        mod_position_count = _file_vertex_count(position_path, _POSITION_STRIDE)
        if (
            mod_blend_count is None
            or mod_position_count is None
            or dump_blend_count is None
            or dump_position_count is None
        ):
            continue
        if (
            mod_blend_count != dump_blend_count
            or mod_position_count != dump_position_count
            or mod_blend_count != mod_position_count
        ):
            continue
        targets.append(
            BlendVoteTarget(
                hash=section.hash,
                resource=section.vb2,
                blend_path=blend_path,
                position_path=position_path,
                dump_blend=dump_blend_path,
                dump_position=dump_position_path,
                source=f"{key2[0]}/{key2[1]}",
            )
        )
    return targets


def scan_blend_vote_targets(
    folder: Path, dumps: DumpData, tables: BlendTables
) -> list[BlendVoteTarget]:
    """Deduped vote targets across every includable .ini under folder."""
    targets: list[BlendVoteTarget] = []
    seen: set[Path] = set()
    for ini in included_ini_files(Path(folder)):
        try:
            text = read_ini_text(ini)
        except (ValueError, OSError):
            continue
        for target in _vote_targets_in_ini(text, ini, dumps, tables):
            if target.blend_path in seen:
                continue
            seen.add(target.blend_path)
            targets.append(target)
    return targets


def apply_blend_vote_remap(
    target: BlendVoteTarget,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None] = print,
    backup: bool = True,
) -> bool:
    """Vote-remap one blend buffer once from the dump; True only when indices changed.

    Idempotent like apply_remap: when the store marker's after-hash already
    matches the live buffer nothing is read further, written, or logged. The
    marker lands in the shared "_blend_remaps.json" before the buffer write.
    """
    mods_dir = Path(mods_dir)
    blend_path = Path(target.blend_path)
    try:
        blend_bytes = blend_path.read_bytes()
    except OSError:
        log(f"blend vote skipped: {blend_path.name} — could not read the blend buffer")
        return False
    sha_now = sha256(blend_bytes).hexdigest()
    state = load_blend_state(store_dir, mods_dir)
    key = blend_state_key(mods_dir, blend_path)
    recorded = state.get(key)
    if isinstance(recorded, dict) and recorded.get("after") == sha_now:
        return False
    others: list[bytes] = []
    for path in (target.position_path, target.dump_blend, target.dump_position):
        try:
            others.append(Path(path).read_bytes())
        except OSError:
            log(
                f"blend vote skipped: {blend_path.name} — "
                f"could not read {Path(path).name}"
            )
            return False
    mod_position, dump_blend, dump_position = others
    vote_map, report = derive_blend_vote_map(
        blend_bytes, mod_position, dump_blend, dump_position
    )
    if not report.ok:
        log(
            f"blend vote skipped: {blend_path.name} — {report.refusal} "
            f"(match {report.matched}/{report.total})"
        )
        return False
    if not vote_map:
        log(f"blend indices already current (vote): {blend_path.name}")
        return False
    new, changed = remap_bytes(blend_bytes, vote_map)
    stamp = int(time.time() * 1000)
    destination = None
    if backup:
        destination = backup_path_for(store_dir, mods_dir, blend_path, stamp)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blend_bytes)
    state[key] = {
        "after": sha256(new).hexdigest(),
        "before": sha_now,
        "hash": target.hash,
        "stamp": stamp,
        "action": "vote",
        "source": target.source,
    }
    write_blend_state(store_dir, mods_dir, state)
    blend_path.write_bytes(new)
    message = (
        f"remapped blend indices (vote): {blend_path.name} — {changed} index "
        f"values changed, {len(vote_map)} mappings derived from {target.source} "
        f"(match {report.matched}/{report.total})"
    )
    if destination is not None:
        message += f" (backup: {destination.name})"
    log(message)
    return True
