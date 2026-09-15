"""Tests for tools.dumpdata: fix-tool dump cache parsing."""

import json
from pathlib import Path

from tools.characters import CharacterDB
from tools.dumpdata import DumpData, DumpLayout, V2Dump, load_dump_data
from tools.model import Character, Component


def _element(
    semantic: str, semantic_index: str, fmt: str, byte_width: str, slot: str, category: str
) -> dict:
    return {
        "SemanticName": semantic,
        "SemanticIndex": semantic_index,
        "Format": fmt,
        "ByteWidth": byte_width,
        "ExtractSlot": slot,
        "ExtractTechnique": "pointlist",
        "Category": category,
        "DrawCategory": category,
    }


def _buffer_entry(
    filename: str, slot: str, category: str, widths: list[str]
) -> dict:
    return {
        "FileName": filename,
        "Type": "Normal",
        "D3D11ElementList": [
            _element("COLOR", str(i), "R32G32B32A32_FLOAT", width, slot, category)
            for i, width in enumerate(widths)
        ],
    }


def _dump_json(
    vertexlimit: str, ib_file: str, hashes: dict[str, str], entries: list[dict]
) -> dict:
    return {
        "GamePreset": "ZZMI",
        "VertexLimitVB": vertexlimit,
        "CategoryHash": hashes,
        "IndexBufferList": [{"DXGI_FORMAT": "DXGI_FORMAT_R32_UINT", "FileName": ib_file}],
        "CategoryBufferList": entries,
    }


# Live-sampled values: Koleda face (48-byte face texcoord, JSON only) and the
# Ryuyin-style legs layout (24-byte texcoord, shipped binaries).
FACE_JSON = _dump_json(
    vertexlimit="78E1FA18",
    ib_file="0e74656e-8112-0.ib",
    hashes={"Blend": "CD9BABC2", "Position": "42F3695F", "Texcoord": "57994826"},
    entries=[
        _buffer_entry(
            "0e74656e-8112-0-Position.buf", "vb0", "Position", ["12", "12", "16"]
        ),
        _buffer_entry(
            "0e74656e-8112-0-Texcoord.buf", "vb1", "Texcoord", ["16"] + ["4"] * 8
        ),
        _buffer_entry("0e74656e-8112-0-Blend.buf", "vb2", "Blend", ["16", "16"]),
    ],
)

LEGS_JSON = _dump_json(
    vertexlimit="00AA11BB",
    ib_file="1a2b3c4d-5678-0.ib",
    hashes={"Blend": "AA11BB22", "Position": "CC33DD44", "Texcoord": "EE55FF66"},
    entries=[
        _buffer_entry(
            "1a2b3c4d-5678-0-Position.buf", "vb0", "Position", ["12", "12", "16"]
        ),
        _buffer_entry(
            "1a2b3c4d-5678-0-Texcoord.buf", "vb1", "Texcoord", ["4", "4", "8", "4", "4"]
        ),
        _buffer_entry("1a2b3c4d-5678-0-Blend.buf", "vb2", "Blend", ["16", "16"]),
    ],
)


def write_dump(cache: Path, folder_name: str, payload: dict) -> Path:
    folder = cache / folder_name
    folder.mkdir(parents=True)
    (folder / f"{folder.name}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return folder


def _v2_entry(
    component: str, position: str, blend: str, texcoord: str, ib: str
) -> dict:
    return {
        "component_name": component,
        "root_vs": "1234ABCD",
        "draw_vb": "567890EF",
        "position_vb": position,
        "blend_vb": blend,
        "texcoord_vb": texcoord,
        "ib": ib,
        "object_indexes": [0],
        "object_index_counts": [36],
        "object_classifications": [component],
        "texture_hashes": ["11112222"],
    }


def write_v2_mesh(
    cache: Path, name: str, entries: list[dict], vb0_files: dict[str, str]
) -> Path:
    """One v2 mesh folder: hash.json list plus its -vb0=<hash>.txt dumps."""
    folder = cache / "v2" / name
    folder.mkdir(parents=True)
    (folder / "hash.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    for filename, text in vb0_files.items():
        (folder / filename).write_text(text, encoding="utf-8")
    return folder


def test_face_folder_parses_layouts_without_binaries(tmp_path):
    cache = tmp_path / "zzz-model-fix-tool"
    write_dump(cache, "珂蕾妲-脸", FACE_JSON)

    data = load_dump_data(cache)

    assert data.layouts == {
        ("珂蕾妲", "脸", "position"): DumpLayout(40, "vb0", "0e74656e-8112-0-Position.buf"),
        ("珂蕾妲", "脸", "texcoord"): DumpLayout(48, "vb1", "0e74656e-8112-0-Texcoord.buf"),
        ("珂蕾妲", "脸", "blend"): DumpLayout(32, "vb2", "0e74656e-8112-0-Blend.buf"),
    }
    assert data.current_hashes == {
        ("珂蕾妲", "脸", "position"): "42f3695f",
        ("珂蕾妲", "脸", "texcoord"): "57994826",
        ("珂蕾妲", "脸", "blend"): "cd9babc2",
    }
    assert data.vertexlimit == {("珂蕾妲", "脸"): "78e1fa18"}
    assert data.ibs == {("珂蕾妲", "脸"): "0e74656e"}
    assert data.binaries == {}


def test_legs_folder_collects_only_shipped_binaries(tmp_path):
    cache = tmp_path / "zzz-model-fix-tool"
    folder = write_dump(cache, "琉音-腿", LEGS_JSON)
    position = folder / "1a2b3c4d-5678-0-Position.buf"
    position.write_bytes(b"position-bytes")
    texcoord = folder / "1a2b3c4d-5678-0-Texcoord.buf"
    texcoord.write_bytes(b"texcoord-bytes")
    (folder / "1a2b3c4d-5678-0.ib").write_bytes(b"ib-bytes")

    data = load_dump_data(cache)

    assert data.layouts[("琉音", "腿", "texcoord")].stride == 24
    assert data.layouts[("琉音", "腿", "texcoord")].slot == "vb1"
    assert data.layouts[("琉音", "腿", "position")].stride == 40
    assert data.layouts[("琉音", "腿", "blend")].stride == 32
    assert data.binaries == {
        ("琉音", "腿", "position"): position,
        ("琉音", "腿", "texcoord"): texcoord,
    }
    assert data.binaries[("琉音", "腿", "texcoord")].read_bytes() == b"texcoord-bytes"
    assert ("琉音", "腿", "blend") not in data.binaries
    assert data.ibs == {("琉音", "腿"): "1a2b3c4d"}


def test_garbage_folders_and_root_files_are_skipped(tmp_path):
    cache = tmp_path / "zzz-model-fix-tool"
    (cache / "坏档-脸").mkdir(parents=True)
    (cache / "坏档-脸" / "坏档-脸.json").write_bytes(b"{not json")
    (cache / "空档-脸").mkdir(parents=True)
    (cache / "空档-脸" / "列表.json").write_bytes(b"[]")
    (cache / "说明.txt").write_text("readme", encoding="utf-8")

    data = load_dump_data(cache)

    assert data == DumpData()


def test_missing_or_empty_cache_dir_yields_empty_dump_data(tmp_path):
    assert load_dump_data(tmp_path / "no-such-cache") == DumpData()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert load_dump_data(empty) == DumpData()


def _fake_db() -> CharacterDB:
    db = CharacterDB()
    for name, comp in (("珂蕾妲Koleda", "Face-脸"), ("蕾米埃尔Remy", "Body-身体")):
        character = Character(name=name)
        character.components.append(Component(name=comp, fields={}))
        db.characters[name] = character
    return db


def test_folder_split_uses_db_then_first_dash_fallback(tmp_path):
    cache = tmp_path / "zzz-model-fix-tool"
    write_dump(cache, "珂蕾妲-脸", FACE_JSON)
    write_dump(cache, "蕾米埃尔-黑皮-腿", LEGS_JSON)
    db = _fake_db()

    data = load_dump_data(cache, db=db)

    assert ("Koleda", "脸", "texcoord") in data.layouts
    assert data.vertexlimit[("Koleda", "脸")] == "78e1fa18"
    assert ("Remy", "黑皮-腿") in data.vertexlimit
    assert data.ibs[("Remy", "黑皮-腿")] == "1a2b3c4d"

    fallback = load_dump_data(cache)

    assert ("珂蕾妲", "脸") in fallback.vertexlimit
    assert ("蕾米埃尔", "黑皮-腿") in fallback.vertexlimit
    assert fallback.ibs[("蕾米埃尔", "黑皮-腿")] == "1a2b3c4d"


# 48-byte texcoord stream (COLOR R32G32B32A32_FLOAT + TEXCOORD 0..3
# R32G32_FLOAT) inside a 92-byte vb0, four vertices with blend data.
V2_FACE_VB0 = """\
stride: 92
first vertex: 0
vertex count: 4
topology: trianglelist

element[0]:
  SemanticName: POSITION
  SemanticIndex: 0
  Format: R32G32B32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 0

element[1]:
  SemanticName: COLOR
  SemanticIndex: 0
  Format: R32G32B32A32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 12

element[2]:
  SemanticName: TEXCOORD
  SemanticIndex: 0
  Format: R32G32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 28

element[3]:
  SemanticName: TEXCOORD
  SemanticIndex: 1
  Format: R32G32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 36

element[4]:
  SemanticName: TEXCOORD
  SemanticIndex: 2
  Format: R32G32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 44

element[5]:
  SemanticName: TEXCOORD
  SemanticIndex: 3
  Format: R32G32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 52

element[6]:
  SemanticName: BLENDWEIGHTS
  SemanticIndex: 0
  Format: R32G32B32A32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 60

element[7]:
  SemanticName: BLENDINDICES
  SemanticIndex: 0
  Format: R32G32B32A32_UINT
  InputSlot: 0
  AlignedByteOffset: 76

vertex-data:
vb0[0]+0 POSITION0: 0.0, 0.1, 0.2
vb0[0]+12 COLOR0: 255, 128, 64, 255
vb0[0]+28 TEXCOORD0: 0.0, 0.0
vb0[0]+36 TEXCOORD1: 0.25, 0.0
vb0[0]+44 TEXCOORD2: 0.5, 0.0
vb0[0]+52 TEXCOORD3: 0.75, 0.0
vb0[0]+60 BLENDWEIGHTS0: 1.0, 0.0, 0.0, 0.0
vb0[0]+76 BLENDINDICES0: 7, 0, 0, 0
vb0[1]+92 POSITION0: 1.0, 1.1, 1.2
vb0[1]+104 COLOR0: 255, 0, 0, 255
vb0[1]+120 TEXCOORD0: 0.0, 0.25
vb0[1]+128 TEXCOORD1: 0.25, 0.25
vb0[1]+136 TEXCOORD2: 0.5, 0.25
vb0[1]+144 TEXCOORD3: 0.75, 0.25
vb0[1]+152 BLENDWEIGHTS0: 0.5, 0.5, 0.0, 0.0
vb0[1]+168 BLENDINDICES0: 7, 8, 0, 0
vb0[2]+184 POSITION0: 2.0, 2.1, 2.2
vb0[2]+196 COLOR0: 0, 255, 0, 255
vb0[2]+212 TEXCOORD0: 0.0, 0.5
vb0[2]+220 TEXCOORD1: 0.25, 0.5
vb0[2]+228 TEXCOORD2: 0.5, 0.5
vb0[2]+236 TEXCOORD3: 0.75, 0.5
vb0[2]+244 BLENDWEIGHTS0: 0.25, 0.25, 0.25, 0.25
vb0[2]+260 BLENDINDICES0: 1, 2, 3, 4
vb0[3]+276 POSITION0: 3.0, 3.1, 3.2
vb0[3]+288 COLOR0: 0, 0, 255, 255
vb0[3]+304 TEXCOORD0: 0.0, 0.75
vb0[3]+312 TEXCOORD1: 0.25, 0.75
vb0[3]+320 TEXCOORD2: 0.5, 0.75
vb0[3]+328 TEXCOORD3: 0.75, 0.75
vb0[3]+336 BLENDWEIGHTS0: 0.4, 0.3, 0.2, 0.1
vb0[3]+352 BLENDINDICES0: 5, 6, 7, 8
"""

# Same stream without a COLOR element: the TEXCOORDs alone.
V2_NO_COLOR_VB0 = """\
stride: 20
first vertex: 0
vertex count: 2
topology: trianglelist

element[0]:
  SemanticName: POSITION
  SemanticIndex: 0
  Format: R32G32B32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 0

element[1]:
  SemanticName: TEXCOORD
  SemanticIndex: 0
  Format: R16G16_FLOAT
  InputSlot: 0
  AlignedByteOffset: 12

element[2]:
  SemanticName: TEXCOORD
  SemanticIndex: 1
  Format: R16G16_FLOAT
  InputSlot: 0
  AlignedByteOffset: 16

vertex-data:
vb0[0]+0 POSITION0: 1.5, 2.5, 3.5
vb0[0]+12 TEXCOORD0: 0.0, 0.0
vb0[0]+16 TEXCOORD1: 1.0, 0.0
vb0[1]+20 POSITION0: 4.5, 5.5, 6.5
vb0[1]+32 TEXCOORD0: 0.0, 1.0
vb0[1]+36 TEXCOORD1: 1.0, 1.0
"""

# No COLOR/TEXCOORD at all: no texcoord layout to derive.
V2_POSITION_ONLY_VB0 = """\
stride: 12
first vertex: 0
vertex count: 1
topology: trianglelist

element[0]:
  SemanticName: POSITION
  SemanticIndex: 0
  Format: R32G32B32_FLOAT
  InputSlot: 0
  AlignedByteOffset: 0

vertex-data:
vb0[0]+0 POSITION0: 0.0, 0.0, 0.0
"""


def test_v2_mesh_folder_dedupes_and_parses_vertices(tmp_path):
    cache = tmp_path / "zzz-model-fix-tool"
    write_dump(cache, "珂蕾妲-脸", FACE_JSON)
    write_v2_mesh(
        cache,
        "TestChar-Face",
        [
            _v2_entry("FaceA", "C44D2531", "08923D3E", "DAFC9647", "FACB2461"),
            _v2_entry("FaceB", "c44d2531", "11111111", "22222222", "33333333"),
            _v2_entry("FaceC", "DEADBEEF", "08923D3E", "DAFC9647", "FACB2461"),
        ],
        {"TestCharFaceA-vb0=c44d2531.txt": V2_FACE_VB0},
    )

    data = load_dump_data(cache)

    expected = V2Dump(
        char_latin="TestChar",
        comp="Face",
        position_vb="c44d2531",
        blend_vb="08923d3e",
        texcoord_vb="dafc9647",
        ib="facb2461",
        texcoord_format=("4f", "2f", "2f", "2f", "2f"),
        texcoord_stride=48,
        vertex_count=4,
        vb0_path=cache / "v2" / "TestChar-Face" / "TestCharFaceA-vb0=c44d2531.txt",
    )
    assert data.v2 == {("TestChar", "Face"): [expected]}
    assert data.vertexlimit == {("珂蕾妲", "脸"): "78e1fa18"}
    dump = data.v2[("TestChar", "Face")][0]
    positions, blends = dump.vertices()
    assert positions == [
        (0.0, 0.1, 0.2),
        (1.0, 1.1, 1.2),
        (2.0, 2.1, 2.2),
        (3.0, 3.1, 3.2),
    ]
    assert blends == [
        ((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0)),
        ((0.5, 0.5, 0.0, 0.0), (7, 8, 0, 0)),
        ((0.25, 0.25, 0.25, 0.25), (1, 2, 3, 4)),
        ((0.4, 0.3, 0.2, 0.1), (5, 6, 7, 8)),
    ]
    assert dump.vertices() is dump.vertices()


def test_v2_texcoord_layout_fallbacks_and_dict_payload(tmp_path):
    cache = tmp_path / "zzz-model-fix-tool"
    write_v2_mesh(
        cache,
        "TestChar-Body",
        [_v2_entry("BodyA", "AAAAAA01", "AAAAAA02", "AAAAAA03", "AAAAAA04")],
        {"TestCharBodyA-vb0=aaaaaa01.txt": V2_NO_COLOR_VB0},
    )
    write_v2_mesh(
        cache,
        "TestChar-Hair",
        [_v2_entry("HairA", "BBBBBB01", "BBBBBB02", "BBBBBB03", "BBBBBB04")],
        {"TestCharHairA-vb0=bbbbbb01.txt": V2_POSITION_ONLY_VB0},
    )
    extra = cache / "v2" / "TestChar-Extra"
    extra.mkdir(parents=True)
    (extra / "hash.json").write_text("{}", encoding="utf-8")

    data = load_dump_data(cache)

    assert set(data.v2) == {("TestChar", "Body"), ("TestChar", "Hair")}
    body = data.v2[("TestChar", "Body")][0]
    assert body.texcoord_format == ("2e", "2e")
    assert body.texcoord_stride == 8
    assert body.vertices() == (
        [(1.5, 2.5, 3.5), (4.5, 5.5, 6.5)],
        [],
    )
    hair = data.v2[("TestChar", "Hair")][0]
    assert hair.texcoord_format is None
    assert hair.texcoord_stride == 0
