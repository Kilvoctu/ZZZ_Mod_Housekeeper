"""Collect the vertex/index buffer binds a mod .ini declares for its hashes.

A ``[TextureOverride…]`` section binds buffers via ``vb0``/``vb1``/``vb2``/``ib`` lines; the referenced ``[Resource<Name>]`` block carries the shipped binary's ``filename`` and, when declared, the vertex ``stride``.
Binds whose Resource block is missing, is not a ``Buffer``, or matches nothing are not collectable and are skipped - diagnosis only covers binds a block declares.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_RESOURCE_HEADER_PREFIX = "[Resource"
_HASH_LINE_RE = re.compile(r"^hash ?= ?(?P<h>[0-9A-Fa-f]{8})$", re.IGNORECASE)
_BIND_LINE_RE = re.compile(r"^(?P<slot>vb\d|ib) ?= ?(?P<name>.+)$", re.IGNORECASE)
_TYPE_LINE_RE = re.compile(r"^type ?= ?Buffer$", re.IGNORECASE)
_STRIDE_LINE_RE = re.compile(r"^stride ?= ?(?P<stride>\d+)$", re.IGNORECASE)
_FILENAME_LINE_RE = re.compile(r"^filename ?= ?(?P<file>.+)$", re.IGNORECASE)
_TRAILING_VARIANT_RE = re.compile(r"\.\d+$")


@dataclass(frozen=True)
class BufferBind:
    """One (hash, slot) buffer bind with its Resource block's declaration."""

    hash: str
    slot: str
    resource: str
    stride: int | None
    filename: str
    path: Path | None
    exists: bool


@dataclass(frozen=True)
class _BufferBlock:
    """One [Resource…] Buffer block parsed from an ini."""

    name: str
    stride: int | None
    filename: str


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
    """Full section name of a "[Resource<name>]" line, else None."""
    if line.startswith(_RESOURCE_HEADER_PREFIX) and line.endswith("]"):
        return line[1:-1]
    return None


def _resource_blocks(lines: list[str]) -> list[_BufferBlock]:
    """Buffer resource blocks with whatever stride/filename they declare."""
    blocks: list[_BufferBlock] = []
    for index, line in enumerate(lines):
        name = _resource_header_name(line)
        if name is None:
            continue
        is_buffer = False
        stride: int | None = None
        filename = ""
        for follow in lines[index + 1 :]:
            if follow.startswith("["):
                break
            if not is_buffer and _TYPE_LINE_RE.match(follow) is not None:
                is_buffer = True
            elif stride is None:
                stride_match = _STRIDE_LINE_RE.match(follow)
                if stride_match is not None:
                    stride = int(stride_match.group("stride"))
            if not filename:
                file_match = _FILENAME_LINE_RE.match(follow)
                if file_match is not None:
                    filename = file_match.group("file").strip()
        if is_buffer:
            blocks.append(_BufferBlock(name=name, stride=stride, filename=filename))
    return blocks


def _bind_for_block(
    hash_value: str,
    slot: str,
    resource: str,
    block: _BufferBlock,
    ini_dir: Path,
    exists: Callable[[Path], bool] | None = None,
) -> BufferBind:
    """One BufferBind resolved against the filesystem or a supplied lookup."""
    if block.filename:
        path = _normalize_path(ini_dir / block.filename)
        present = path.is_file() if exists is None else exists(path)
    else:
        path = None
        present = False
    return BufferBind(
        hash=hash_value,
        slot=slot,
        resource=resource,
        stride=block.stride,
        filename=block.filename,
        path=path,
        exists=present,
    )


def collect_buffer_binds(
    ini_text: str, ini_path: Path, exists: Callable[[Path], bool] | None = None
) -> list[BufferBind]:
    """Buffer binds declared by this ini's TextureOverride hashes.

    Each (hash, vbN/ib) bind pairs with every Resource block whose variant-suffix-stripped name matches the bound resource; ``exists`` is evaluated against the real filesystem at collection time, or via the supplied lookup callable when one is passed (analyze reuses the walk's file set to avoid a stat per bind).
    """
    ini_dir = Path(ini_path).parent
    lines = _ini_lines(ini_text)
    blocks = _resource_blocks(lines)
    triples: list[tuple[str, str, str]] = []
    seen_triples: set[tuple[str, str, str]] = set()
    hashes: list[str] = []
    binds: list[tuple[str, str]] = []
    in_override = False

    def flush() -> None:
        if not in_override:
            return
        for bound_hash in hashes:
            for bound_slot, bound_resource in binds:
                key = (bound_hash, bound_slot, bound_resource.lower())
                if key not in seen_triples:
                    seen_triples.add(key)
                    triples.append((bound_hash, bound_slot, bound_resource))

    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            flush()
            in_override = line[1:-1].strip().lower().startswith("textureoverride")
            hashes.clear()
            binds.clear()
            continue
        if not in_override:
            continue
        hash_match = _HASH_LINE_RE.match(line)
        if hash_match is not None:
            hashes.append(hash_match.group("h").lower())
            continue
        bind_match = _BIND_LINE_RE.match(line)
        if bind_match is not None:
            binds.append((bind_match.group("slot").lower(), bind_match.group("name")))
    flush()

    result: list[BufferBind] = []
    for hash_value, slot, resource_name in triples:
        base = _TRAILING_VARIANT_RE.sub("", resource_name).lower()
        for block in blocks:
            if _TRAILING_VARIANT_RE.sub("", block.name).lower() != base:
                continue
            result.append(
                _bind_for_block(hash_value, slot, resource_name, block, ini_dir, exists)
            )
    return result
