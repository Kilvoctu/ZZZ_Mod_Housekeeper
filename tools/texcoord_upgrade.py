"""Upgrade face texcoord .buf files to the post-2.54 float32 format.

Game 2.54 switched face texcoord records from 36 bytes (first component as
four packed UNORM8 bytes) to 48 bytes (the same component as four float32
values, byte / 255 — verified byte-exact against an author-shipped pair). Each
upgrade rewrites the buffer and its Resource stride line together and records a
marker in the store's "_texcoord_upgrades.json" so an interrupted run never
re-converts. Body/hair texcoords keep the old stride, so upgrades gate on face
hashes and a still-36 stride; dump data routes and cross-checks.
"""

import json
import os
import re
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .backups import backup_path_for, included_ini_files, store_root
from .dumpdata import DumpData
from .fixer import FixerData, read_ini_text

_OLD_STRIDE = 36
_NEW_STRIDE = 48
_TEXCOORD_STATE_NAME = "_texcoord_upgrades.json"

_RESOURCE_HEADER_PREFIX = "[Resource"
_HASH_LINE_RE = re.compile(r"^hash ?= ?(?P<h>[0-9A-Fa-f]{8})$", re.IGNORECASE)
_VB_LINE_RE = re.compile(r"^vb\d ?= ?Resource(?P<name>.+)$", re.IGNORECASE)
_TYPE_LINE_RE = re.compile(r"^type ?= ?Buffer$", re.IGNORECASE)
_STRIDE_36_LINE_RE = re.compile(r"^stride ?= ?36$", re.IGNORECASE)
_FILENAME_LINE_RE = re.compile(r"^filename ?= ?(?P<file>.+)$", re.IGNORECASE)
_TRAILING_VARIANT_RE = re.compile(r"\.\d+$")
_DISABLED_TOGGLE = "DISABLED_"


@dataclass(frozen=True)
class _ResourceBlock:
    """One [Resource…] Buffer block parsed from an ini."""

    name: str
    filename: str


@dataclass(frozen=True)
class TexcoordTarget:
    """One face-texcoord buffer to upgrade, with the ini and override hash bound to it."""

    hash: str
    resource: str
    path: Path
    ini_path: Path


def upgrade_bytes(data: bytes) -> bytes:
    """36-byte packed-UNORM8 face texcoord records -> 48-byte float32 records.

    The first four bytes of each record unpack to four little-endian float32
    values (byte / 255.0); the remaining 32 bytes pass through unchanged.
    Raises ValueError when the length is not a whole number of 36-byte records.
    """
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


def _resource_blocks(lines: list[str]) -> list[_ResourceBlock]:
    """Buffer resource blocks declaring the old 36-byte stride."""
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
        if _STRIDE_36_LINE_RE.match(rest[1]) is None:
            continue
        file_match = _FILENAME_LINE_RE.match(rest[2])
        if file_match is None:
            continue
        blocks.append(
            _ResourceBlock(name=name, filename=file_match.group("file").strip())
        )
    return blocks


def find_targets(
    ini_text: str, ini_path: Path, face_hashes: frozenset[str] | set[str]
) -> list[TexcoordTarget]:
    """Face-texcoord buffers bound by this ini's TextureOverride hashes.

    A target exists per Resource block whose variant-stripped name matches a
    resource a face-texcoord-hash override binds, and whose block still declares
    stride = 36.
    """
    ini_dir = Path(ini_path).parent
    lines = _ini_lines(ini_text)
    blocks = _resource_blocks(lines)
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
    targets: list[TexcoordTarget] = []
    for hash_value, resource_name in pairs:
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
    """Rewrite bound Resource blocks' stride 36 -> 48 in the ini; True when written."""
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
        if _STRIDE_36_LINE_RE.match(rest[1]) is None:
            continue
        file_match = _FILENAME_LINE_RE.match(rest[2])
        if file_match is None:
            continue
        resolved = _normalize_path(
            Path(ini_path.parent) / file_match.group("file").strip()
        )
        if resolved != Path(target.path):
            continue
        changed[index + 3] = "stride = 48"
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
        f"updated face texcoord stride 36 -> 48: {ini_path.name} "
        f"({len(changed)} resource block(s))"
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
    so nothing is backed up.  Returns the backup file name or None.
    """
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


def apply_upgrade(
    target: TexcoordTarget,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None] = print,
    backup: bool = True,
    dumps: DumpData | None = None,
) -> bool:
    """Upgrade one face-texcoord buffer and its Resource stride line; True when written.

    Idempotent: once the marker's after-hash matches the live buffer, only a
    still-36 stride line is completed; a buffer that is not 36-byte aligned is
    skipped with a log line.  Dump data routes the write: a hash matched in the
    dump restores or cross-checks the dump's current texcoord binary and a
    missing buffer is restored from it; without a dump the conversion is
    unverified.
    """
    mods_dir = Path(mods_dir)
    buf_path = Path(target.path)
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

    "dump restore" marks a buffer whose bytes came verbatim from a fix-tool
    dump, "buffer conversion" a genuinely converted one; a legacy marker
    without an "action" field predates the distinction and counts as a
    conversion.
    """
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

    A marker is dropped when its live .buf exists again with exactly the
    content the marker's "before" hash recorded, i.e. nothing converted
    remains on disk; non-.buf paths are ignored and unreadable live files
    are skipped.  The state file is rewritten at most once, only when
    something was dropped; returns the dropped count.
    """
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
