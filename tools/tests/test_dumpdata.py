"""Tests for tools.dumpdata: fix-tool dump cache parsing."""

import json
from pathlib import Path

from tools.dumpdata import DumpData, DumpLayout, load_dump_data
from tools.model import Character, Component
from tools.characters import CharacterDB


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
