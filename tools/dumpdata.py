"""Parse the ZZZ-Model-Fix-Tool dump data (版本修复工具/dump subfolder).

v1: per-component folders (JSON + .buf + mesh .ib); stride = sum of ByteWidths.
v2: <cache>/v2/<MeshFolder>/ hash.json + *-vb0=<hash>.txt dumps, lazy body parse.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from .characters import CharacterDB
from .structure import latin_suffix

_ROLES = frozenset({"position", "texcoord", "blend"})

_DXGI_TOKEN = {
    "R8G8B8A8_UNORM": "4B",
    "R16G16_FLOAT": "2e",
    "R32G32_FLOAT": "2f",
    "R16G16B16A16_FLOAT": "4e",
    "R32G32B32_FLOAT": "3f",
    "R32G32B32A32_FLOAT": "4f",
    "R32G32_UINT": "2I",
    "R32G32B32A32_UINT": "4I",
    "R16G16B16A16_UINT": "4H",
}
_TOKEN_BYTES = {"B": 1, "e": 2, "f": 4, "I": 4, "H": 2}

_VB0Vertices = tuple[
    list[tuple[float, float, float]],
    list[tuple[tuple[float, ...], tuple[int, ...]]],
]

_VB0Texcoords = dict[int, list[tuple[float, float] | None]]

_ELEMENT_RE = re.compile(
    r"SemanticName:\s*(\w+)\s*\n\s*"
    r"SemanticIndex:\s*(\d+)\s*\n\s*"
    r"Format:\s*(\w+)\s*\n\s*"
    r"InputSlot:\s*(\d+)\s*\n\s*"
    r"AlignedByteOffset:\s*(\d+)"
)
_VERTEX_LINE_RE = re.compile(
    r"(?m)^vb\d+\[(\d+)]\+(\d+)\s+([A-Za-z_]+)(\d+)?:\s*(.*)$"
)
_VERTEX_COUNT_RE = re.compile(r"(?m)^vertex count:\s*(\d+)")


@dataclass(frozen=True)
class DumpLayout:
    """One dumped buffer: byte stride, its ExtractSlot and shipped filename."""

    stride: int
    slot: str
    filename: str


@dataclass
class V2Dump:
    """One v2 mesh reference: a hash.json entry plus its vb0 dump.

    Hash fields are lowercased ('' when absent); ``texcoord_format`` holds the
    header layout (COLOR first, then TEXCOORDs by index); ``vertices``/``texcoords``
    parse the vb0 body lazily."""

    char_latin: str
    comp: str
    position_vb: str
    blend_vb: str
    texcoord_vb: str
    ib: str
    texcoord_format: tuple[str, ...] | None
    texcoord_stride: int
    vertex_count: int
    vb0_path: Path | None
    _vertex_cache: _VB0Vertices | None = field(default=None, repr=False, compare=False)
    _texcoord_cache: _VB0Texcoords | None = field(
        default=None, repr=False, compare=False
    )

    def vertices(self) -> _VB0Vertices:
        """(positions, blends) parsed from the vb0 body; cached after first call."""
        cached = self._vertex_cache
        if cached is None:
            cached = _parse_vb0_vertices(self.vb0_path)
            self._vertex_cache = cached
        return cached

    def texcoords(self) -> _VB0Texcoords:
        """{slot: per-vertex (u, v) or None} parsed from the vb0 body; cached after first call."""
        cached = self._texcoord_cache
        if cached is None:
            cached = _parse_vb0_texcoords(self.vb0_path)
            self._texcoord_cache = cached
        return cached


@dataclass
class DumpData:
    """Everything parsed from the fix-tool dump cache.

    ``layouts``/``current_hashes``/``binaries`` key by (char_latin, comp, role);
    ``binaries`` holds only shipped files; ``vertexlimit``/``ibs``/``v2`` key by
    (char_latin, comp)."""

    layouts: dict[tuple[str, str, str], DumpLayout] = field(default_factory=dict)
    current_hashes: dict[tuple[str, str, str], str] = field(default_factory=dict)
    vertexlimit: dict[tuple[str, str], str] = field(default_factory=dict)
    binaries: dict[tuple[str, str, str], Path] = field(default_factory=dict)
    ibs: dict[tuple[str, str], str] = field(default_factory=dict)
    v2: dict[tuple[str, str], list[V2Dump]] = field(default_factory=dict)


def load_dump_data(cache_dir: Path, db: CharacterDB | None = None) -> DumpData:
    """Parse every component dump folder under the fix-tool cache root.

    ``cache_dir`` is the extracted 版本修复工具/dump root; subfolder names split via
    _split_folder; undecodable JSONs skip silently; a missing/empty cache yields
    an empty DumpData; the ``v2`` subfolder follows."""
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
    _load_v2_dumps(cache_dir, db, data)
    return data


def _split_folder(name: str, db: CharacterDB | None) -> tuple[str, str]:
    """Split one dump folder name into (char_latin, comp label).

    The character part is the longest dash-prefix that startswith-matches a
    table character name (latin suffix recorded); else the first dash splits."""
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

    Role and ExtractSlot come from the first D3D11ElementList element (all share
    them); stride sums element ByteWidths (ints may arrive as strings)."""
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


def _load_v2_dumps(cache_dir: Path, db: CharacterDB | None, data: DumpData) -> None:
    """Fold every ``<cache>/v2`` mesh folder into ``data.v2`` (v2 schema above)."""
    v2_root = cache_dir / "v2"
    if not v2_root.is_dir():
        return
    for folder in sorted(v2_root.iterdir(), key=lambda path: path.name):
        if not folder.is_dir():
            continue
        try:
            payload = json.loads(
                folder.joinpath("hash.json").read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        _collect_v2_folder(payload, folder, _split_folder(folder.name, db), data)


def _collect_v2_folder(
    payload: list, folder: Path, split: tuple[str, str], data: DumpData
) -> None:
    """Append one V2Dump per deduped hash.json entry that has a vb0 dump."""
    char_latin, comp = split
    key2 = (char_latin, comp)
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        position_vb = _v2_hash(entry, "position_vb")
        if not position_vb or position_vb in seen:
            continue
        vb0_path = _find_v2_vb0(folder, position_vb)
        if vb0_path is None:
            continue
        seen.add(position_vb)
        elements, vertex_count = _parse_vb0_header(vb0_path)
        texcoord_format = _texcoord_layout(elements)
        data.v2.setdefault(key2, []).append(
            V2Dump(
                char_latin=char_latin,
                comp=comp,
                position_vb=position_vb,
                blend_vb=_v2_hash(entry, "blend_vb"),
                texcoord_vb=_v2_hash(entry, "texcoord_vb"),
                ib=_v2_hash(entry, "ib"),
                texcoord_format=texcoord_format,
                texcoord_stride=_texcoord_stride(texcoord_format),
                vertex_count=vertex_count,
                vb0_path=vb0_path,
            )
        )


def _v2_hash(entry: dict, key: str) -> str:
    """Lowercased string hash from one hash.json entry; '' when absent."""
    value = entry.get(key)
    return value.lower() if isinstance(value, str) else ""


def _find_v2_vb0(folder: Path, position_vb: str) -> Path | None:
    """The ``*-vb0=<position_vb>.txt`` dump, matched by the filename hash."""
    matches = sorted(folder.glob(f"*-vb0={position_vb}.txt"), key=lambda path: path.name)
    return matches[0] if matches else None


def _parse_vb0_header(path: Path) -> tuple[list[tuple[str, int, str]], int]:
    """(element triples, declared vertex count) from one -vb0 txt header.

    Elements are the SemanticName/SemanticIndex/Format blocks before the
    ``vertex-data:`` line; unreadable or malformed files give ([], 0)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    head, _sep, _body = text.partition("vertex-data:")
    elements = [
        (match.group(1).upper(), int(match.group(2)), match.group(3))
        for match in _ELEMENT_RE.finditer(head)
    ]
    count_match = _VERTEX_COUNT_RE.search(head)
    count = int(count_match.group(1)) if count_match else 0
    return elements, count


def _texcoord_layout(
    elements: list[tuple[str, int, str]],
) -> tuple[str, ...] | None:
    """Texcoord stream tokens: the COLOR element first, then TEXCOORDs sorted.

    Upstream rule: COLOR sits in front of the stream and the TEXCOORDs follow
    ordered by SemanticIndex; unknown D3D formats or no COLOR/TEXCOORD at all give None."""
    picked = [element for element in elements if element[0] == "COLOR"][:1]
    picked += sorted(
        (element for element in elements if element[0] == "TEXCOORD"),
        key=lambda element: element[1],
    )
    tokens = [_DXGI_TOKEN.get(element[2], "") for element in picked]
    if not tokens or not all(tokens):
        return None
    return tuple(tokens)


def _texcoord_stride(texcoord_format: tuple[str, ...] | None) -> int:
    """Total byte width of the texcoord tokens ('4f' is 16, '4B' is 4)."""
    if texcoord_format is None:
        return 0
    return sum(_TOKEN_BYTES[token[-1]] * int(token[:-1]) for token in texcoord_format)


def _parse_vb0_vertices(path: Path | None) -> _VB0Vertices:
    """(positions, blends) from one -vb0 body; failures yield empty lists.

    Each ``vb0[i]+<offset>`` line fills vertex i: POSITION0 → (x, y, z);
    BLENDWEIGHTS0 → four weights, BLENDINDICES0 → four indices (both required)."""
    if path is None:
        return [], []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    _head, _sep, body = text.partition("vertex-data:")
    positions: dict[int, tuple[float, float, float]] = {}
    weights: dict[int, tuple[float, ...]] = {}
    indices: dict[int, tuple[int, ...]] = {}
    for match in _VERTEX_LINE_RE.finditer(body):
        try:
            values = [float(value) for value in match.group(5).split(",")]
        except ValueError:
            continue
        index = int(match.group(1))
        semantic = match.group(3).upper()
        if semantic == "POSITION" and match.group(4) in (None, "0"):
            if len(values) < 3:
                continue
            positions[index] = (values[0], values[1], values[2])
        elif semantic == "BLENDWEIGHTS":
            weights[index] = tuple(values[:4])
        elif semantic == "BLENDINDICES":
            indices[index] = tuple(int(value) for value in values[:4])
    return (
        [positions[key] for key in sorted(positions)],
        [(weights[key], indices[key]) for key in sorted(weights) if key in indices],
    )


def _parse_vb0_texcoords(path: Path | None) -> _VB0Texcoords:
    """TEXCOORD rows of one -vb0 body as {slot: per-vertex (u, v) or None}.

    A bare ``TEXCOORD`` line or one with index 0 fills slot 0 and higher slots
    carry their SemanticIndex; missing or non-finite values stay None."""
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    head, _sep, body = text.partition("vertex-data:")
    count_match = _VERTEX_COUNT_RE.search(head)
    total = int(count_match.group(1)) if count_match else 0
    found: dict[int, dict[int, tuple[float, float]]] = {}
    highest = -1
    for match in _VERTEX_LINE_RE.finditer(body):
        if match.group(3).upper() != "TEXCOORD":
            continue
        suffix = match.group(4)
        index = int(match.group(1))
        highest = max(highest, index)
        try:
            values = [float(value) for value in match.group(5).split(",")]
        except ValueError:
            continue
        if len(values) < 2:
            continue
        u, v = values[0], values[1]
        if not (isfinite(u) and isfinite(v)):
            continue
        slot = 0 if suffix in (None, "0") else int(suffix)
        found.setdefault(slot, {})[index] = (u, v)
    length = max(total, highest + 1)
    return {
        slot: [rows.get(index) for index in range(length)]
        for slot, rows in sorted(found.items())
    }
