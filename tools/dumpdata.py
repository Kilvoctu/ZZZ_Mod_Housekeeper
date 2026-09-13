"""Parse the ZZZ-Model-Fix-Tool dump data (版本修复工具/dump subfolder).

Provenance: the dump repo (hefengchang/ZZZ-Model-Fix-Tool) ships one folder
per component under 版本修复工具/dump, named ``<角色中文名>-<部件>`` possibly
with extra dash-separated qualifiers (e.g. ``蕾米埃尔-黑皮-腿``).  Each folder
holds one JSON description plus the referenced ``.buf`` binaries and the mesh
``.ib``; face folders ship the JSON only.

Stride rule: a buffer's byte stride is the sum of its ``D3D11ElementList``
``ByteWidth`` values (all elements share one ``ExtractSlot``), e.g. a face
Texcoord of 16 + 4×8 = 48 bytes.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .characters import CharacterDB
from .structure import latin_suffix

_ROLES = frozenset({"position", "texcoord", "blend"})


@dataclass(frozen=True)
class DumpLayout:
    """One dumped buffer: byte stride, its ExtractSlot and shipped filename."""

    stride: int
    slot: str
    filename: str


@dataclass
class DumpData:
    """Everything parsed from the fix-tool dump cache.

    ``layouts``/``current_hashes``/``binaries`` key by (char_latin, comp, role)
    with role the lowercased buffer Category; ``vertexlimit``/``ibs`` key by
    (char_latin, comp).  ``binaries`` holds only shipped (existing) files.
    """

    layouts: dict[tuple[str, str, str], DumpLayout] = field(default_factory=dict)
    current_hashes: dict[tuple[str, str, str], str] = field(default_factory=dict)
    vertexlimit: dict[tuple[str, str], str] = field(default_factory=dict)
    binaries: dict[tuple[str, str, str], Path] = field(default_factory=dict)
    ibs: dict[tuple[str, str], str] = field(default_factory=dict)


def load_dump_data(cache_dir: Path, db: CharacterDB | None = None) -> DumpData:
    """Parse every component dump folder under the fix-tool cache root.

    ``cache_dir`` is the extracted 版本修复工具/dump root.  Each immediate
    subdirectory is one component dump; the folder name splits into
    (character, component) via the longest dash-prefix startswith-matching a
    table character name (whose latin name is recorded), falling back to the
    first dash when no db is given or nothing matches.  Undecodable JSONs are
    skipped silently; a missing or empty cache yields an empty DumpData.
    """
    data = DumpData()
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return data
    for folder in sorted(cache_dir.iterdir(), key=lambda path: path.name):
        if not folder.is_dir():
            continue
        json_paths = sorted(folder.glob("*.json"), key=lambda path: path.name)
        if not json_paths:
            continue
        try:
            payload = json.loads(json_paths[0].read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        _parse_dump_json(payload, folder, _split_folder(folder.name, db), data)
    return data


def _split_folder(name: str, db: CharacterDB | None) -> tuple[str, str]:
    """Split one dump folder name into (char_latin, comp label).

    The character part is the longest dash-prefix that startswith-matches a
    table character name (latin suffix recorded); without a db or a match the
    first dash splits and the raw prefix is kept as-is.
    """
    if "-" not in name:
        return name, ""
    best: tuple[str, str] | None = None
    if db is not None:
        pos = name.find("-")
        while pos >= 0:
            if pos > 0:
                char_part = name[:pos]
                matches = [
                    table_name
                    for table_name in db.characters
                    if table_name.startswith(char_part)
                ]
                if matches:
                    table_name = max(matches, key=len)
                    best = (latin_suffix(table_name), name[pos + 1:])
            pos = name.find("-", pos + 1)
    if best is not None:
        return best
    char_part, comp = name.split("-", 1)
    return char_part, comp


def _parse_dump_json(
    payload: dict, folder: Path, split: tuple[str, str], data: DumpData
) -> None:
    """Fold one dump JSON into ``data`` under the (char_latin, comp) split."""
    char_latin, comp = split
    key2 = (char_latin, comp)
    hash_by_role = _lowered_hash_map(payload.get("CategoryHash"))
    buffer_list = payload.get("CategoryBufferList")
    if isinstance(buffer_list, list):
        for entry in buffer_list:
            if not isinstance(entry, dict):
                continue
            parsed = _parse_buffer_entry(entry, hash_by_role)
            if parsed is None:
                continue
            role, layout, hash_value = parsed
            key3 = (char_latin, comp, role)
            data.layouts[key3] = layout
            if hash_value:
                data.current_hashes[key3] = hash_value
            binary = folder / layout.filename
            if binary.is_file():
                data.binaries[key3] = binary
    index_list = payload.get("IndexBufferList")
    if isinstance(index_list, list) and index_list:
        first = index_list[0]
        if isinstance(first, dict) and isinstance(first.get("FileName"), str):
            data.ibs[key2] = first["FileName"].split("-", 1)[0].lower()
    vertexlimit = payload.get("VertexLimitVB")
    if isinstance(vertexlimit, str):
        data.vertexlimit[key2] = vertexlimit.lower()


def _lowered_hash_map(raw: object) -> dict[str, str]:
    """CategoryHash as {lowercased role: lowercased hash}; non-strings dropped."""
    if not isinstance(raw, dict):
        return {}
    return {
        key.lower(): value.lower()
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _parse_buffer_entry(
    entry: dict, hash_by_role: Mapping[str, str]
) -> tuple[str, DumpLayout, str] | None:
    """(role, layout, current hash) from one CategoryBufferList entry, or None.

    Role and ExtractSlot are read from the first D3D11ElementList element (all
    elements share them); the stride sums the element ByteWidth values (ints
    may arrive as strings); unparseable entries return None.
    """
    elements = entry.get("D3D11ElementList")
    if not isinstance(elements, list) or not elements:
        return None
    first = elements[0]
    if not isinstance(first, dict):
        return None
    category = first.get("Category")
    if not isinstance(category, str):
        return None
    role = category.lower()
    if role not in _ROLES:
        return None
    stride = 0
    for element in elements:
        if not isinstance(element, dict):
            return None
        try:
            byte_width = element.get("ByteWidth")
            if not isinstance(byte_width, (str, int)):
                return None
            stride += int(byte_width)
        except (TypeError, ValueError):
            return None
    slot = first.get("ExtractSlot")
    filename = entry.get("FileName")
    if not isinstance(slot, str) or not isinstance(filename, str) or not filename:
        return None
    layout = DumpLayout(stride=stride, slot=slot, filename=filename)
    return role, layout, hash_by_role.get(role, "")
