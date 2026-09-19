"""Upgrade face texcoord .buf files to the post-2.54 float32 format.

Game 2.54 switched face texcoord records from 36 bytes (first component as four packed UNORM8 bytes) to 48 bytes (the same component as four float32 values, byte / 255 — verified byte-exact against an author-shipped pair); each upgrade rewrites the buffer and its Resource stride line together and records a marker in the store's "_texcoord_upgrades.json" so an interrupted run never re-converts, and body/hair texcoords keep the old stride, so upgrades gate on face hashes and a still-36 stride with dump data routing and cross-checking.
Beyond that fixed rule, ``scan_v2_texcoord_targets`` uses v2 dump layouts to upgrade buffers of any declared stride: the live buffer's real per-vertex size is matched against the dump's ``texcoord_format`` by shrinking one '4f' block to packed bytes or half floats (buffers with fewer chunks yet — old 2-UV faces — derive their layout from UV value ranges against the dump's vb0 TEXCOORD rows), and ``convert_bytes`` rewrites record by record — equal tokens pass through, only four-wide 4B/4e/4f pairs convert, anything else raises rather than guessing; a texcoord hash claimed by several dump folders is used only when all claiming dumps agree on its format and stride, conflicting claims are refused and noted (optional refusals out-list) rather than first-matched; format tokens are count+kind over the same table as tools.dumpdata ("4B" is 4 bytes, "4e" and "2f" are 8).
"""

import json
import os
import re
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path

from .backups import backup_path_for, included_ini_files, store_root
from .dumpdata import DumpData, V2Dump
from .fixer import FixerData, read_ini_text

_OLD_STRIDE = 36
_NEW_STRIDE = 48
_TEXCOORD_STATE_NAME = "_texcoord_upgrades.json"

_TOKEN_BYTES = {"B": 1, "e": 2, "f": 4, "I": 4, "H": 2}

_RESOURCE_HEADER_PREFIX = "[Resource"
_HASH_LINE_RE = re.compile(r"^hash ?= ?(?P<h>[0-9A-Fa-f]{8})$", re.IGNORECASE)
_VB_LINE_RE = re.compile(r"^vb\d ?= ?Resource(?P<name>.+)$", re.IGNORECASE)
_TYPE_LINE_RE = re.compile(r"^type ?= ?Buffer$", re.IGNORECASE)
_STRIDE_LINE_RE = re.compile(r"^stride ?= ?(?P<n>\d+)$", re.IGNORECASE)
_FILENAME_LINE_RE = re.compile(r"^filename ?= ?(?P<file>.+)$", re.IGNORECASE)
_TRAILING_VARIANT_RE = re.compile(r"\.\d+$")
_DISABLED_TOGGLE = "DISABLED_"


@dataclass(frozen=True)
class _ResourceBlock:
    """One [Resource…] Buffer block parsed from an ini, with its declared stride."""

    name: str
    filename: str
    stride: int


@dataclass(frozen=True)
class TexcoordTarget:
    """One face-texcoord buffer to upgrade, with the ini and override hash bound to it.

    Legacy 36 -> 48 targets keep the default strides and no format tokens; v2
    targets carry the dump-inferred old tokens, the dump's new tokens, their strides and a "char/comp" provenance source (fewer-chunks ones add the old-to-new element map)."""

    hash: str
    resource: str
    path: Path
    ini_path: Path
    old_stride: int = _OLD_STRIDE
    new_stride: int = _NEW_STRIDE
    fmt_old: tuple[str, ...] | None = None
    fmt_new: tuple[str, ...] | None = None
    source: str = ""
    element_map: tuple[int, ...] | None = None


def _token_parts(token: str) -> tuple[int, str]:
    """(count, kind) of one count+kind token ("4f" -> (4, "f")); ValueError when malformed."""
    if len(token) < 2 or not token[:-1].isdigit():
        raise ValueError(f"malformed texcoord token: {token!r}")
    count = int(token[:-1])
    if token[-1] not in _TOKEN_BYTES or count <= 0:
        raise ValueError(f"malformed texcoord token: {token!r}")
    return count, token[-1]


def _token_width(token: str) -> int:
    """Byte width of one count+kind token ("4B" is 4, "2e" and "2f" are 8)."""
    count, kind = _token_parts(token)
    return count * _TOKEN_BYTES[kind]


def _fmt_stride(fmt: tuple[str, ...]) -> int:
    """Total byte width of one record in ``fmt`` ('4B' is 4, '2e' and '2f' are 8).

    Tokens are count+kind over the same table as tools.dumpdata's _TOKEN_BYTES.
    """
    return sum(_token_width(token) for token in fmt)


def upgrade_bytes(data: bytes) -> bytes:
    """36-byte packed-UNORM8 face texcoord records -> 48-byte float32 records.

    The first four bytes of each record unpack to four little-endian float32
    values (byte / 255.0), the remaining 32 bytes pass through unchanged; ValueError when the length is not a whole number of 36-byte records."""
    if len(data) % _OLD_STRIDE:
        raise ValueError(f"texcoord buffer is not {_OLD_STRIDE}-byte aligned: {len(data)}")
    out = bytearray(len(data) // _OLD_STRIDE * _NEW_STRIDE)
    for index in range(0, len(data), _OLD_STRIDE):
        packed = data[index : index + 4]
        base = index // _OLD_STRIDE * _NEW_STRIDE
        for k in range(4):
            struct.pack_into("<f", out, base + 4 * k, packed[k] / 255.0)
        out[base + 16 : base + _NEW_STRIDE] = data[index + 4 : index + _OLD_STRIDE]
    return bytes(out)


def _unorm_to_floats(chunk: bytes, token: str) -> bytes:
    """Packed UNORM8 bytes re-packed as ``token`` floats (byte / 255.0)."""
    return struct.pack("<" + token, *(byte / 255.0 for byte in chunk))


def _floats_to_unorm(chunk: bytes, token: str) -> bytes:
    """``token``-packed floats or halves as clamped round(value * 255) bytes."""
    return bytes(
        min(255, max(0, round(value * 255)))
        for value in struct.unpack("<" + token, chunk)
    )


def _convert_chunk(old_token: str, new_token: str, chunk: bytes) -> bytes:
    """One token position's converted bytes; ValueError for any unconvertible change."""
    if old_token == new_token:
        return bytes(chunk)
    pair = (old_token, new_token)
    if pair in (("4B", "4f"), ("4B", "4e")):
        return _unorm_to_floats(chunk, new_token)
    if pair in (("4f", "4B"), ("4e", "4B")):
        return _floats_to_unorm(chunk, old_token)
    if pair in (("4f", "4e"), ("4e", "4f")):
        return struct.pack("<" + new_token, *struct.unpack("<" + old_token, chunk))
    raise ValueError(
        f"unsupported texcoord token conversion: {old_token} -> {new_token}"
    )


def convert_bytes(
    data: bytes,
    old_fmt: tuple[str, ...],
    new_fmt: tuple[str, ...],
    element_map: tuple[int, ...] | None = None,
) -> bytes:
    """Convert texcoord records from ``old_fmt`` to ``new_fmt``, block by block.

    Without ``element_map`` both formats need the same block count and blocks convert position by position; with it old block i lands on new position element_map[i] (one entry per old block, targets unique and in range) and untargeted new blocks zero-fill.
    ``data`` needs whole old-stride records; equal tokens pass through byte-for-byte, supported numeric conversions pair the four-wide packed-UNORM8/float32/half16 blocks, anything else raises; records land exactly on the new stride."""
    if element_map is None:
        if len(old_fmt) != len(new_fmt):
            raise ValueError("old_fmt and new_fmt must have the same block count")
        if not old_fmt:
            raise ValueError("empty texcoord format")
        sources: dict[int, int] = {index: index for index in range(len(old_fmt))}
    else:
        if not old_fmt:
            raise ValueError("empty texcoord format")
        if len(element_map) != len(old_fmt):
            raise ValueError("element_map must have one entry per old block")
        if len(set(element_map)) != len(element_map):
            raise ValueError("element_map has duplicate targets")
        if any(target < 0 or target >= len(new_fmt) for target in element_map):
            raise ValueError("element_map target out of range")
        sources = {target: index for index, target in enumerate(element_map)}
    old_stride = _fmt_stride(old_fmt)
    new_stride = _fmt_stride(new_fmt)
    if len(data) % old_stride:
        raise ValueError(f"texcoord buffer is not {old_stride}-byte aligned: {len(data)}")
    old_offsets = [0]
    for token in old_fmt:
        old_offsets.append(old_offsets[-1] + _token_width(token))
    out = bytearray()
    for base in range(0, len(data), old_stride):
        record = bytearray()
        for new_index, new_token in enumerate(new_fmt):
            old_index = sources.get(new_index)
            if old_index is None:
                record.extend(bytes(_token_width(new_token)))
                continue
            old_token = old_fmt[old_index]
            start = base + old_offsets[old_index]
            chunk = data[start : start + _token_width(old_token)]
            record.extend(_convert_chunk(old_token, new_token, chunk))
        if len(record) < new_stride:
            record.extend(bytes(new_stride - len(record)))
        elif len(record) > new_stride:
            del record[new_stride:]
        out.extend(record)
    return bytes(out)


def infer_old_formats(
    new_fmt: tuple[str, ...], per_vertex: int
) -> list[tuple[str, ...]]:
    """Old layouts fitting ``per_vertex``: each '4f' block shrunk to 4B then 4e.

    Candidates are tried left-to-right by block position, '4B' before '4e' within
    a position, and kept only when their stride equals the real per-vertex byte size; empty when nothing matches."""
    candidates: list[tuple[str, ...]] = []
    for index, token in enumerate(new_fmt):
        if token != "4f":
            continue
        for shrink in ("4B", "4e"):
            trial = list(new_fmt)
            trial[index] = shrink
            if _fmt_stride(tuple(trial)) == per_vertex:
                candidates.append(tuple(trial))
    return candidates


@dataclass(frozen=True)
class DerivedLayout:
    """One fewer-chunks old layout: kept tokens, their new positions, unmatched marks.

    ``old_format`` holds the kept tokens in order (any shrunk one already applied), ``element_map`` maps old block i onto new position element_map[i], and ``unmatched`` lists kept positions that are neither '2f' nor the shrunk one."""

    old_format: tuple[str, ...]
    element_map: tuple[int, ...]
    unmatched: tuple[int, ...]


_LAYOUT_RANGE_EPS = 0.02


def layout_candidates(
    new_fmt: tuple[str, ...], per_vertex: int
) -> tuple[DerivedLayout, ...]:
    """Old fewer-chunks layouts fitting ``per_vertex``: kept token subsets, one '4f' shrunk.

    Enumerates kept subsets of ``new_fmt`` (each kept '4f' may shrink to '4B' then '4e'), keeping only byte widths equal to ``per_vertex`` and deduping by (old_format, element_map); ``unmatched`` marks kept positions that are neither '2f' nor the shrunk one."""
    sizes = [_token_width(token) for token in new_fmt]
    out: list[DerivedLayout] = []
    seen: set[tuple[tuple[str, ...], tuple[int, ...]]] = set()
    for mask in range(1, 1 << len(new_fmt)):
        kept = [index for index in range(len(new_fmt)) if mask >> index & 1]
        width = sum(sizes[index] for index in kept)
        choices: list[tuple[int | None, str | None]] = [(None, None)]
        choices += [
            (index, shrink)
            for index in kept
            if new_fmt[index] == "4f"
            for shrink in ("4B", "4e")
        ]
        for shrunk, shrink_token in choices:
            got = width
            if shrunk is not None and shrink_token is not None:
                got = width - sizes[shrunk] + _token_width(shrink_token)
            if got != per_vertex:
                continue
            old_format = tuple(
                shrink_token if index == shrunk else new_fmt[index] for index in kept
            )
            key = (old_format, tuple(kept))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                DerivedLayout(
                    old_format=old_format,
                    element_map=tuple(kept),
                    unmatched=tuple(
                        index
                        for index in kept
                        if new_fmt[index] != "2f" and index != shrunk
                    ),
                )
            )
    return tuple(out)


def _value_range(
    data: bytes, count: int, offset: int, stride: int
) -> tuple[float, float] | None:
    """(low, high) of the '2f' values at each record's ``offset``; None without any.

    Non-finite values are skipped and a finite value of magnitude over 1e6 rejects the whole range."""
    low: float | None = None
    high: float | None = None
    for index in range(count):
        u, v = struct.unpack_from("<2f", data, index * stride + offset)
        for value in (u, v):
            if not isfinite(value):
                continue
            if abs(value) > 1e6:
                return None
            low = value if low is None or value < low else low
            high = value if high is None or value > high else high
    return None if low is None else (low, high)


def chunk_uv_ranges(
    data: bytes, count: int, chunks: tuple[str, ...]
) -> tuple[tuple[float, float] | None, ...]:
    """Per chunk: the (low, high) range of its float values, None for non-'2f' or implausible.

    NaN and inf values are skipped (real buffers contain them); a finite value of magnitude over 1e6 marks the whole chunk implausible (the buffer is not cut that way)."""
    offsets = [0]
    for token in chunks:
        offsets.append(offsets[-1] + _token_width(token))
    if len(data) < count * offsets[-1]:
        return (None,) * len(chunks)
    return tuple(
        None
        if token != "2f"
        else _value_range(data, count, offsets[index], offsets[-1])
        for index, token in enumerate(chunks)
    )


def _row_range(row: list[tuple[float, float] | None]) -> tuple[float, float] | None:
    """(low, high) over a slot's non-None (u, v) points; None without any usable value.

    Non-finite values are skipped and a finite value of magnitude over 1e6 rejects the whole row."""
    low: float | None = None
    high: float | None = None
    for point in row:
        if point is None:
            continue
        for value in point:
            if not isfinite(value):
                continue
            if abs(value) > 1e6:
                return None
            low = value if low is None or value < low else low
            high = value if high is None or value > high else high
    return None if low is None else (low, high)


def dump_uv_ranges(
    entry: V2Dump,
) -> tuple[tuple[float, float] | None, ...] | None:
    """Per texcoord token: the (low, high) range of its dump UV values, None without usable data.

    Aligned to ``entry.texcoord_format`` positions — '2f' tokens take the dump's TEXCOORD slots in order while COLOR and other tokens stay None; unusable UV data refuses entirely."""
    fmt = entry.texcoord_format
    if fmt is None:
        return None
    rows = entry.texcoords()
    ranges: list[tuple[float, float] | None] = [None] * len(fmt)
    found = False
    slot = 0
    for position, token in enumerate(fmt):
        count, kind = _token_parts(token)
        if kind != "f" or count != 2:
            continue
        row = rows.get(slot)
        slot += 1
        if not row:
            return None
        row_low_high = _row_range(row)
        if row_low_high is None:
            return None
        ranges[position] = row_low_high
        found = True
    return tuple(ranges) if found else None


def match_uv_chunks_to_dump(
    mod_ranges: tuple[tuple[float, float] | None, ...],
    dump_ranges: tuple[tuple[float, float] | None, ...],
) -> dict[int, int] | None:
    """Match every mod UV chunk to a dump chunk: {dump position: old index}, None if unclear.

    A mod chunk must fit inside the dump chunk's range within _LAYOUT_RANGE_EPS and the tightest fit wins; two equally tight fits cannot be told apart."""
    options: list[tuple[int, list[int]]] = []
    for old_index, mod_range in enumerate(mod_ranges):
        if mod_range is None:
            continue
        hits: list[tuple[float, int]] = []
        for dump_index, dump_range in enumerate(dump_ranges):
            if dump_range is None:
                continue
            if (
                mod_range[0] >= dump_range[0] - _LAYOUT_RANGE_EPS
                and mod_range[1] <= dump_range[1] + _LAYOUT_RANGE_EPS
            ):
                hits.append((dump_range[1] - dump_range[0], dump_index))
        if not hits:
            return None
        hits.sort()
        if len(hits) > 1 and abs(hits[0][0] - hits[1][0]) < 1e-9:
            return None
        options.append((old_index, [item[1] for item in hits]))
    options.sort(key=lambda item: len(item[1]))
    used: set[int] = set()
    assignment: dict[int, int] = {}
    for old_index, targets in options:
        for dump_index in targets:
            if dump_index in used:
                continue
            assignment[dump_index] = old_index
            used.add(dump_index)
            break
        else:
            return None
    return assignment


def derive_missing_chunk_layout(
    data: bytes, per_vertex: int, entry: V2Dump
) -> DerivedLayout | None:
    """Old layout for a buffer with fewer chunks than the dump, or None on any refusal.

    Candidates by byte size first; with usable dump UV data only those whose chunk ranges line up with it in the order the candidate claims survive, else a single unmatched-free candidate by size is trusted."""
    candidates = layout_candidates(entry.texcoord_format or (), per_vertex)
    if not candidates:
        return None
    dump_ranges = dump_uv_ranges(entry)
    matched: list[DerivedLayout] = []
    if dump_ranges is not None:
        for candidate in candidates:
            if candidate.unmatched:
                continue
            mod_ranges = chunk_uv_ranges(data, entry.vertex_count, candidate.old_format)
            expected: dict[int, int] = {}
            broken = False
            for old_index, token in enumerate(candidate.old_format):
                if token != "2f":
                    continue
                if mod_ranges[old_index] is None:
                    broken = True
                    break
                expected[candidate.element_map[old_index]] = old_index
            if broken:
                continue
            if match_uv_chunks_to_dump(mod_ranges, dump_ranges) == expected:
                matched.append(candidate)
    if len(matched) == 1:
        return matched[0]
    if not matched and len(candidates) == 1 and not candidates[0].unmatched:
        return candidates[0]
    return None


def _normalize_path(path: Path) -> Path:
    """Lexically normalized path (collapses "." and ".." parts without touching the disk)."""
    return Path(os.path.normpath(str(path)))


def _ini_lines(text: str) -> list[str]:
    """Trimmed non-empty non-comment lines."""
    return [
        line
        for line in (raw.strip() for raw in text.replace("\r\n", "\n").split("\n"))
        if line and not line.startswith(";")
    ]


def _resource_header_name(line: str) -> str | None:
    """Captured name of a "[Resource<name>]" line, else None."""
    if line.startswith(_RESOURCE_HEADER_PREFIX) and line.endswith("]"):
        return line[len(_RESOURCE_HEADER_PREFIX) : -1]
    return None


def _stride_text(line: str) -> str | None:
    """Raw stride digits of a "stride = <n>" line, else None."""
    match = _STRIDE_LINE_RE.match(line)
    return None if match is None else match.group("n")


def _resource_blocks(
    lines: list[str], stride: int | None = _OLD_STRIDE
) -> list[_ResourceBlock]:
    """Buffer resource blocks declaring ``stride`` (None keeps every declared stride)."""
    blocks: list[_ResourceBlock] = []
    for index in range(len(lines)):
        name = _resource_header_name(lines[index])
        if name is None:
            continue
        rest = lines[index + 1 : index + 4]
        if len(rest) < 3:
            continue
        if _TYPE_LINE_RE.match(rest[0]) is None:
            continue
        raw_stride = _stride_text(rest[1])
        if raw_stride is None:
            continue
        if stride is not None and raw_stride != str(stride):
            continue
        file_match = _FILENAME_LINE_RE.match(rest[2])
        if file_match is None:
            continue
        blocks.append(
            _ResourceBlock(
                name=name,
                filename=file_match.group("file").strip(),
                stride=int(raw_stride),
            )
        )
    return blocks


def _override_pairs(
    lines: list[str], face_hashes: frozenset[str] | set[str]
) -> list[tuple[str, str]]:
    """Deduped (hash, resource) pairs bound by face-hash TextureOverride sections."""
    pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    hashes: list[str] = []
    resources: list[str] = []
    in_override = False

    def flush() -> None:
        if not in_override:
            return
        for bound_hash in hashes:
            if bound_hash not in face_hashes:
                continue
            for bound_resource in resources:
                key = (bound_hash, bound_resource.lower())
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    pairs.append((bound_hash, bound_resource))

    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            flush()
            in_override = line[1:-1].strip().lower().startswith("textureoverride")
            hashes.clear()
            resources.clear()
            continue
        if not in_override:
            continue
        hash_match = _HASH_LINE_RE.match(line)
        if hash_match is not None:
            hashes.append(hash_match.group("h").lower())
            continue
        vb_match = _VB_LINE_RE.match(line)
        if vb_match is not None:
            resources.append(vb_match.group("name"))
    flush()
    return pairs


def find_targets(
    ini_text: str, ini_path: Path, face_hashes: frozenset[str] | set[str]
) -> list[TexcoordTarget]:
    """Face-texcoord buffers bound by this ini's TextureOverride hashes.

    A target exists per Resource block whose variant-stripped name matches a
    resource a face-texcoord-hash override binds and that still declares stride = 36."""
    ini_dir = Path(ini_path).parent
    lines = _ini_lines(ini_text)
    blocks = _resource_blocks(lines)
    targets: list[TexcoordTarget] = []
    for hash_value, resource_name in _override_pairs(lines, face_hashes):
        base = _TRAILING_VARIANT_RE.sub("", resource_name).lower()
        for block in blocks:
            if _TRAILING_VARIANT_RE.sub("", block.name).lower() != base:
                continue
            targets.append(
                TexcoordTarget(
                    hash=hash_value,
                    resource=block.name,
                    path=_normalize_path(Path(ini_dir) / block.filename),
                    ini_path=Path(ini_path),
                )
            )
    return targets


def scan_texcoord_targets(
    folder: Path, face_hashes: frozenset[str] | set[str]
) -> list[TexcoordTarget]:
    """Deduped face-texcoord targets across every includable .ini under folder."""
    targets: list[TexcoordTarget] = []
    seen: set[tuple[str, str]] = set()
    for ini in included_ini_files(Path(folder)):
        try:
            text = read_ini_text(ini)
        except (ValueError, OSError):
            continue
        for target in find_targets(text, ini, face_hashes):
            key = (str(target.ini_path), str(target.path))
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
    return targets


def _is_face_comp(comp: str) -> bool:
    """Whether a dump component label names a face part ("脸" or Face*)."""
    return "脸" in comp or comp.lower().startswith("face")


def dump_face_texcoord_hashes(dumps: DumpData) -> frozenset[str]:
    """Current face-component texcoord hashes recorded by the dump data."""
    return frozenset(
        hash_value
        for (_char, comp, role), hash_value in dumps.current_hashes.items()
        if role == "texcoord" and _is_face_comp(comp)
    )


def dump_match_for_hash(dumps: DumpData, target_hash: str) -> tuple[str, str] | None:
    """First sorted (char_latin, comp) whose current texcoord hash equals ``target_hash``."""
    lowered = target_hash.lower()
    matches = sorted(
        (char, comp)
        for (char, comp, role), value in dumps.current_hashes.items()
        if role == "texcoord" and value == lowered
    )
    return matches[0] if matches else None


def buffer_gate(data: FixerData) -> frozenset[str]:
    """Hash gate for buffer passes: override face hashes plus dump face hashes."""
    return data.face_texcoord_hashes | dump_face_texcoord_hashes(data.dumps)


def _v2_dump_candidates(dumps: DumpData, target_hash: str) -> list[V2Dump]:
    """Every usable v2 dump claiming ``target_hash``, in sorted (char_latin, comp) order."""
    lowered = target_hash.lower()
    return [
        entry
        for key2 in sorted(dumps.v2)
        for entry in dumps.v2[key2]
        if entry.texcoord_vb == lowered and entry.texcoord_format is not None
    ]


def _v2_dump_for_hash(dumps: DumpData, target_hash: str) -> V2Dump | None:
    """First usable v2 dump for the hash only when all claiming dumps agree.

    Candidates are the sorted dumps claiming ``texcoord_vb``; when their
    (texcoord_format, texcoord_stride) claims differ the hash is refused (None)."""
    candidates = _v2_dump_candidates(dumps, target_hash)
    if not candidates:
        return None
    first = candidates[0]
    claims = (first.texcoord_format, first.texcoord_stride)
    for entry in candidates[1:]:
        if (entry.texcoord_format, entry.texcoord_stride) != claims:
            return None
    return first


def _v2_conflict_note(
    ini_path: Path, hash_value: str, candidates: list[V2Dump]
) -> str:
    """Refusal reason naming every (char_latin/comp, texcoord_format) claim on a hash."""
    claims = ", ".join(
        f"({entry.char_latin}/{entry.comp}, {'.'.join(entry.texcoord_format)})"
        for entry in candidates
    )
    return (
        f"refusing v2 texcoord upgrade: hash {hash_value} bound by {ini_path} "
        f"has conflicting dump claims — {claims}"
    )


def _missing_chunk_target(
    entry: V2Dump,
    hash_value: str,
    block: _ResourceBlock,
    buf_path: Path,
    ini_path: Path,
    per_vertex: int,
) -> TexcoordTarget | None:
    """A v2 target whose old layout is derived from UV ranges (fewer chunks than the dump)."""
    try:
        data = buf_path.read_bytes()
    except OSError:
        return None
    layout = derive_missing_chunk_layout(data, per_vertex, entry)
    if layout is None:
        return None
    return TexcoordTarget(
        hash=hash_value,
        resource=block.name,
        path=buf_path,
        ini_path=Path(ini_path),
        old_stride=per_vertex,
        new_stride=entry.texcoord_stride,
        fmt_old=layout.old_format,
        fmt_new=entry.texcoord_format,
        element_map=layout.element_map,
        source=f"{entry.char_latin}/{entry.comp}",
    )


def _v2_target(
    entry: V2Dump, hash_value: str, block: _ResourceBlock, ini_path: Path
) -> TexcoordTarget | None:
    """A v2 target when the buffer's real per-vertex size infers an old layout.

    Same-chunk buffers infer by shrinking one '4f' block; buffers with fewer chunks
    than the dump derive their layout from UV value ranges (vb0 TEXCOORD rows) or None."""
    buf_path = _normalize_path(Path(ini_path).parent / block.filename)
    try:
        size = buf_path.stat().st_size
    except OSError:
        return None
    if entry.vertex_count <= 0 or size % entry.vertex_count:
        return None
    per_vertex = size // entry.vertex_count
    if per_vertex == entry.texcoord_stride or per_vertex == _OLD_STRIDE:
        return None
    if entry.texcoord_format is None:
        return None
    candidates = infer_old_formats(entry.texcoord_format, per_vertex)
    if not candidates:
        if per_vertex >= entry.texcoord_stride or entry.vb0_path is None:
            return None
        return _missing_chunk_target(
            entry, hash_value, block, buf_path, ini_path, per_vertex
        )
    return TexcoordTarget(
        hash=hash_value,
        resource=block.name,
        path=buf_path,
        ini_path=Path(ini_path),
        old_stride=per_vertex,
        new_stride=entry.texcoord_stride,
        fmt_old=candidates[0],
        fmt_new=entry.texcoord_format,
        source=f"{entry.char_latin}/{entry.comp}",
    )


def _v2_ini_targets(
    ini_text: str,
    ini_path: Path,
    dumps: DumpData,
    face_hashes: frozenset[str] | set[str],
    refusals: list[str] | None = None,
) -> list[TexcoordTarget]:
    """V2 targets bound by one ini's face-hash overrides over any-stride blocks.

    Hashes claimed by several dump folders bind only when every claim agrees; a conflict refuses the hash and appends one reason to ``refusals`` per (ini, hash) when given."""
    lines = _ini_lines(ini_text)
    blocks = _resource_blocks(lines, None)
    targets: list[TexcoordTarget] = []
    refused: set[str] = set()
    for hash_value, resource_name in _override_pairs(lines, face_hashes):
        entry = _v2_dump_for_hash(dumps, hash_value)
        if entry is None:
            if refusals is not None and hash_value not in refused:
                candidates = _v2_dump_candidates(dumps, hash_value)
                if candidates:
                    refused.add(hash_value)
                    refusals.append(
                        _v2_conflict_note(ini_path, hash_value, candidates)
                    )
            continue
        base = _TRAILING_VARIANT_RE.sub("", resource_name).lower()
        for block in blocks:
            if _TRAILING_VARIANT_RE.sub("", block.name).lower() != base:
                continue
            target = _v2_target(entry, hash_value, block, ini_path)
            if target is not None:
                targets.append(target)
    return targets


def scan_v2_texcoord_targets(
    folder: Path,
    dumps: DumpData,
    face_hashes: frozenset[str] | set[str],
    refusals: list[str] | None = None,
) -> list[TexcoordTarget]:
    """Deduped v2 texcoord targets: dump-declared formats over any-stride blocks.

    Each face-hash override binding a Resource block pairs with the v2 dump whose ``texcoord_vb`` equals the hash (multi-dump claims bind only when they agree; conflicts are refused and noted in ``refusals`` when given), and the live buffer's real per-vertex size must infer to a convertible old layout (never the dump's own stride, never the legacy 36).
    Missing or unusable buffers are skipped silently."""
    targets: list[TexcoordTarget] = []
    seen: set[tuple[str, str]] = set()
    for ini in included_ini_files(Path(folder)):
        try:
            text = read_ini_text(ini)
        except (ValueError, OSError):
            continue
        for target in _v2_ini_targets(text, ini, dumps, face_hashes, refusals):
            key = (str(target.ini_path), str(target.path))
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
    return targets


def texcoord_state_path(store_dir: Path, mods_dir: Path) -> Path:
    """Marker file: <store root for mods_dir>/_texcoord_upgrades.json."""
    return store_root(store_dir, mods_dir) / _TEXCOORD_STATE_NAME


def load_texcoord_state(store_dir: Path, mods_dir: Path) -> dict[str, dict]:
    """Recorded upgrade markers keyed by canonical buffer path, {} when absent."""
    try:
        return json.loads(
            texcoord_state_path(store_dir, mods_dir).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(store_dir: Path, mods_dir: Path, state: dict[str, dict]) -> None:
    state_path = texcoord_state_path(store_dir, mods_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _state_key(mods_dir: Path, path: Path) -> str:
    """Canonical marker key: DISABLED_-stripped relative dirs plus filename."""
    rel = Path(path).relative_to(Path(mods_dir))
    return "/".join(
        (
            *(part.removeprefix(_DISABLED_TOGGLE) for part in rel.parent.parts),
            rel.name,
        )
    )


def _decode_ini(data: bytes) -> tuple[str, str]:
    """(text, encoding name) with the same decode rules as the fixer scanner."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("gbk"), "gbk"


def _split_eol(line: str) -> tuple[str, str]:
    """Split one kept-ends line into (content, eol suffix)."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def _complete_stride(
    target: TexcoordTarget,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None],
    backup: bool,
) -> bool:
    """Rewrite bound Resource blocks' declared stride to the new one; True when written."""
    ini_path = Path(target.ini_path)
    try:
        raw = ini_path.read_bytes()
        text, encoding = _decode_ini(raw)
    except (ValueError, OSError):
        log(f"could not re-read {ini_path.name} for the stride update")
        return False
    lines = text.splitlines(keepends=True)
    changed: dict[int, str] = {}
    for index in range(len(lines) - 3):
        name = _resource_header_name(lines[index].strip())
        if name is None:
            continue
        rest = [line.strip() for line in lines[index + 1 : index + 4]]
        if _TYPE_LINE_RE.match(rest[0]) is None:
            continue
        if _stride_text(rest[1]) != str(target.old_stride):
            continue
        file_match = _FILENAME_LINE_RE.match(rest[2])
        if file_match is None:
            continue
        resolved = _normalize_path(
            Path(ini_path.parent) / file_match.group("file").strip()
        )
        if resolved != Path(target.path):
            continue
        changed[index + 3] = f"stride = {target.new_stride}"
    if not changed:
        return False
    fixed = list(lines)
    for line_no, replacement in changed.items():
        _content, line_eol = _split_eol(fixed[line_no - 1])
        fixed[line_no - 1] = replacement + line_eol
    if backup:
        stamp = int(time.time() * 1000)
        backup_path = backup_path_for(store_dir, mods_dir, ini_path, stamp)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_bytes(raw)
    ini_path.write_bytes("".join(fixed).encode(encoding))
    log(
        f"updated face texcoord stride {target.old_stride} -> {target.new_stride}: "
        f"{ini_path.name} ({len(changed)} resource block(s))"
    )
    return True


def _record_upgrade(
    buf_path: Path,
    live: bytes | None,
    new: bytes,
    before: str,
    target: TexcoordTarget,
    state: dict[str, dict],
    key: str,
    store_dir: Path,
    mods_dir: Path,
    backup: bool,
    action: str,
    source: str,
) -> str | None:
    """Back up the live bytes, write ``new`` and record one upgrade marker.

    ``live`` is the pre-write buffer content; None when the buffer was absent,
    so nothing is backed up; returns the backup file name or None."""
    stamp = int(time.time() * 1000)
    destination = None
    if backup and live is not None:
        destination = backup_path_for(store_dir, mods_dir, buf_path, stamp)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(live)
    state[key] = {
        "after": sha256(new).hexdigest(),
        "before": before,
        "hash": target.hash,
        "stamp": stamp,
        "action": action,
        "source": source,
    }
    _write_state(store_dir, mods_dir, state)
    buf_path.write_bytes(new)
    return None if destination is None else destination.name


def _apply_format_upgrade(
    target: TexcoordTarget,
    buf_path: Path,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None],
    backup: bool,
) -> bool:
    """Convert one v2 target's buffer to its dump-declared format; True when written.

    There is no restore source for a v2 buffer, so a missing file only logs; an
    existing buffer is converted block-by-block from the target's inferred old tokens to the dump's new tokens."""
    fmt_old = target.fmt_old
    fmt_new = target.fmt_new
    if fmt_old is None or fmt_new is None:
        raise ValueError(f"v2 texcoord target without an old format: {target.resource}")
    state = load_texcoord_state(store_dir, mods_dir)
    key = _state_key(mods_dir, buf_path)
    if not buf_path.is_file():
        log(f"no face texcoord buffer for {target.resource}: {buf_path.name} missing")
        return False
    data = buf_path.read_bytes()
    sha_now = sha256(data).hexdigest()
    recorded = state.get(key)
    if isinstance(recorded, dict) and recorded.get("after") == sha_now:
        return _complete_stride(target, store_dir, mods_dir, log, backup)
    if len(data) % target.old_stride:
        log(
            f"skipping face texcoord upgrade: {buf_path.name} is not "
            f"{target.old_stride}-byte aligned"
        )
        return False
    new = convert_bytes(data, fmt_old, fmt_new, target.element_map)
    backup_name = _record_upgrade(
        buf_path,
        data,
        new,
        sha_now,
        target,
        state,
        key,
        store_dir,
        mods_dir,
        backup,
        "convert",
        target.source,
    )
    message = (
        f"converted texcoord format: {buf_path.name} ({target.hash}) — "
        f"{len(data) // target.old_stride} vertices, "
        f"stride {target.old_stride} -> {target.new_stride}"
    )
    if backup_name is not None:
        message += f" (backup: {backup_name})"
    log(message)
    _complete_stride(target, store_dir, mods_dir, log, backup)
    return True


def apply_upgrade(
    target: TexcoordTarget,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None] = print,
    backup: bool = True,
    dumps: DumpData | None = None,
) -> bool:
    """Upgrade one face-texcoord buffer and its Resource stride line; True when written.

    Idempotent: once the marker's after-hash matches the live buffer only a still-old stride line is completed, and a buffer not aligned to the target's old stride is skipped with a log line; v2 targets (``fmt_new`` set) convert block-by-block via their dump-declared formats with no dump restore path.
    Legacy targets route through dump data: a hash matched in the dump restores or cross-checks the dump's current texcoord binary (a missing buffer is restored from it); without a dump the conversion is unverified."""
    mods_dir = Path(mods_dir)
    buf_path = Path(target.path)
    if target.fmt_new is not None:
        return _apply_format_upgrade(
            target, buf_path, store_dir, mods_dir, log=log, backup=backup
        )
    dumps = DumpData() if dumps is None else dumps
    match = dump_match_for_hash(dumps, target.hash)
    dump_key: tuple[str, str, str] | None = (
        None if match is None else (match[0], match[1], "texcoord")
    )
    dump_path = None if dump_key is None else dumps.binaries.get(dump_key)
    state = load_texcoord_state(store_dir, mods_dir)
    key = _state_key(mods_dir, buf_path)

    if not buf_path.is_file():
        if dump_path is None:
            log(f"no face texcoord buffer for {target.resource}: {buf_path.name} missing")
            return False
        _record_upgrade(
            buf_path,
            None,
            dump_path.read_bytes(),
            "",
            target,
            state,
            key,
            store_dir,
            mods_dir,
            backup,
            "restore",
            dump_path.name,
        )
        log(
            f"restored missing face texcoord from dump: {buf_path.name} "
            f"({target.hash}) — source {dump_path.name}"
        )
        _complete_stride(target, store_dir, mods_dir, log, backup)
        return True

    data = buf_path.read_bytes()
    sha_now = sha256(data).hexdigest()
    recorded = state.get(key)
    if isinstance(recorded, dict) and recorded.get("after") == sha_now:
        return _complete_stride(target, store_dir, mods_dir, log, backup)
    if len(data) % _OLD_STRIDE:
        log(f"skipping face texcoord upgrade: {buf_path.name} is not 36-byte aligned")
        return False
    new = upgrade_bytes(data)
    action = "convert"
    source = ""
    message = (
        f"upgraded face texcoord: {buf_path.name} ({target.hash}) — "
        f"{len(data) // _OLD_STRIDE} vertices, stride 36 -> 48, "
        "unverified (no dump for this component)"
    )
    if dump_key is not None and dump_path is not None:
        dump_bytes = dump_path.read_bytes()
        layout = dumps.layouts.get(dump_key)
        dump_stride = 0 if layout is None else layout.stride
        if dump_stride != _NEW_STRIDE:
            new = dump_bytes
            action = "restore"
            source = dump_path.name
            message = (
                f"no conversion rule for stride 36 → {dump_stride}; restored "
                f"dump bytes (buffer content not preserved): {buf_path.name} "
                f"({target.hash})"
            )
        elif dump_bytes == new:
            new = dump_bytes
            action = "restore"
            source = dump_path.name
            message = (
                f"restored face texcoord from dump: {buf_path.name} "
                f"({target.hash}) — converted bytes match dump {dump_path.name}"
            )
        else:
            message = (
                f"upgraded face texcoord: {buf_path.name} ({target.hash}) — "
                f"{len(data) // _OLD_STRIDE} vertices, stride 36 -> 48; mod "
                f"buffer had edits — converted, dump kept as reference "
                f"({dump_path.name})"
            )
    backup_name = _record_upgrade(
        buf_path,
        data,
        new,
        sha_now,
        target,
        state,
        key,
        store_dir,
        mods_dir,
        backup,
        action,
        source,
    )
    if backup_name is not None:
        message += f" (backup: {backup_name})"
    log(message)
    _complete_stride(target, store_dir, mods_dir, log, backup)
    return True


def marker_kind(store_dir: Path, mods_dir: Path, live_path: Path) -> str | None:
    """Fix-kind annotation for a live buffer from its upgrade marker; None without one.

    "dump restore" marks a buffer whose bytes came verbatim from a fix-tool dump,
    "buffer conversion" a genuinely converted one; a legacy marker without an "action" field predates the distinction and counts as a conversion."""
    marker = load_texcoord_state(store_dir, mods_dir).get(
        _state_key(mods_dir, live_path)
    )
    if not isinstance(marker, dict):
        return None
    return "dump restore" if marker.get("action") == "restore" else "buffer conversion"


def prune_texcoord_markers(
    store_dir: Path, mods_dir: Path, live_paths: Iterable[Path]
) -> int:
    """Drop upgrade markers of buffers a revert put back to pre-upgrade bytes.

    A marker is dropped when its live .buf exists again with exactly the content the marker's "before" hash recorded (nothing converted remains on disk); non-.buf paths are ignored and unreadable live files are skipped.
    The state file is rewritten at most once, only when something was dropped; returns the dropped count."""
    state = load_texcoord_state(store_dir, mods_dir)
    if not state:
        return 0
    dropped = 0
    for live in live_paths:
        live = Path(live)
        if live.suffix != ".buf":
            continue
        key = _state_key(mods_dir, live)
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
        _write_state(store_dir, mods_dir, state)
    return dropped


def rewrite_texcoord_state_keys(
    store_dir: Path, mods_dir: Path, old_prefix: str, new_prefix: str
) -> int:
    """Rewrite applied-upgrade marker keys after a folder rename; returns the count changed.

    Keys equal to old_prefix or under it swap that prefix; no-op without a marker file; a rewritten key overwrites a colliding existing key.
    """
    state = load_texcoord_state(store_dir, mods_dir)
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
        _write_state(store_dir, mods_dir, rewritten)
    return changed


def remove_texcoord_state_keys(store_dir: Path, mods_dir: Path, prefix: str) -> int:
    """Remove applied-upgrade marker keys under a deleted folder; returns count removed."""
    state = load_texcoord_state(store_dir, mods_dir)
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
        _write_state(store_dir, mods_dir, kept)
    return removed
