"""Tests for tools.characters on fixture JSONs in tmp_path (no repo dependency)."""

import json

import pytest

from tools.characters import load_characters

CHAR_DIR_NAME = "角色hash表"

TEST_CHAR = [
    {
        "component_name": "Body-身体",
        "draw_vb": "01B35C45",
        "position_vb": "08C15B45",
        "texcoord_vb": "F6474154",
        "blend_vb": "8C0622D7",
        "ib": "A23AA8A3",
        "object_indexes": [0, 56634],
        "root_vs": "0123456789abcdef",
        "texture_hashes": [["Diffuse", ".dds", "B07C43EF"]],
    },
    {
        "component_name": "Face-脸",
        "VertexLimit": "62A109A8",
        "Position": "38E511A4",
        "Texcoord": "89A25F1A",
        "Blend": "D3A66DB9",
        "ib": "52F5AA74",
        "MatchFirstIndex": ["0", "8442"],
        "PartNameList": ["Face", "Hair"],
    },
    {"_note": "x", "_author": "y"},
]


@pytest.fixture()
def repo_dir(tmp_path):
    char_dir = tmp_path / CHAR_DIR_NAME
    char_dir.mkdir()
    (char_dir / "TestChar.json").write_text(
        json.dumps(TEST_CHAR, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def component_by_name(char, name):
    """The one component whose component_name matches exactly."""
    matches = [component for component in char.components if component.name == name]
    assert len(matches) == 1, name
    return matches[0]


def test_components_parsed_with_normalized_fields(repo_dir):
    db = load_characters(repo_dir)
    assert list(db.characters) == ["TestChar"]
    char = db.characters["TestChar"]
    assert len(char.components) == 2
    body = component_by_name(char, "Body-身体")
    face = component_by_name(char, "Face-脸")
    assert body.fields == {
        "draw_vb": "01b35c45",
        "position_vb": "08c15b45",
        "texcoord_vb": "f6474154",
        "blend_vb": "8c0622d7",
        "ib": "a23aa8a3",
    }
    assert face.fields == {
        "vertexlimit": "62a109a8",
        "position": "38e511a4",
        "texcoord": "89a25f1a",
        "blend": "d3a66db9",
        "ib": "52f5aa74",
    }
    assert body.texture_hashes == [["Diffuse", ".dds", "B07C43EF"]]


def test_reverse_index_refs(repo_dir):
    db = load_characters(repo_dir)
    ib_refs = {(ref.character, ref.component, ref.role) for ref in db.reverse["a23aa8a3"]}
    assert ib_refs == {("TestChar", "Body-身体", "ib")}
    blend_refs = {(ref.component, ref.role) for ref in db.reverse["8c0622d7"]}
    assert blend_refs == {("Body-身体", "blend_vb")}
    diffuse_refs = {(ref.component, ref.role) for ref in db.reverse["b07c43ef"]}
    assert diffuse_refs == {("Body-身体", "diffuse")}
    assert "0123456789abcdef" not in db.reverse


def test_invalid_json_raises_error_naming_the_file(tmp_path):
    char_dir = tmp_path / CHAR_DIR_NAME
    char_dir.mkdir()
    (char_dir / "BadChar.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_characters(tmp_path)
    assert "BadChar.json" in str(excinfo.value)


def test_object_indexes_and_classifications_parsed(tmp_path):
    char_dir = tmp_path / CHAR_DIR_NAME
    char_dir.mkdir()
    entries = [
        {
            "component_name": "Body-身体",
            "ib": "619c5c94",
            "object_indexes": [0, 45060, 45324, 46932],
            "object_classifications": ["A", "B", "C", "D"],
        },
        {
            "component_name": "Face-脸",
            "ib": "52f5aa74",
            "object_indexes": ["0", "8532"],
        },
        {"component_name": "Hair-头发", "ib": "81e925ed"},
        {"component_name": "Hand-手", "ib": "0a0a0a0a", "object_indexes": [0, True]},
    ]
    (char_dir / "Belle.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    db = load_characters(tmp_path)

    char = db.characters["Belle"]
    body = component_by_name(char, "Body-身体")
    assert body.object_indexes == [0, 45060, 45324, 46932]
    assert body.object_classifications == ["A", "B", "C", "D"]
    face = component_by_name(char, "Face-脸")
    assert face.object_indexes is None
    assert face.object_classifications is None
    hair = component_by_name(char, "Hair-头发")
    assert hair.object_indexes is None
    assert hair.object_classifications is None
    hand = component_by_name(char, "Hand-手")
    assert hand.object_indexes is None


def test_lowvarm_ib_only_component(tmp_path):
    char_dir = tmp_path / CHAR_DIR_NAME
    char_dir.mkdir()
    entry = {
        "component_name": "Hair Shadow-头发阴影",
        "ib": "81e925ed",
        "object_indexes": [0],
        "object_classifications": ["A"],
        "texture_hashes": [[]],
    }
    (char_dir / "LowVarmChar.json").write_text(
        json.dumps([entry, {"_note": "作者信息", "_author": "x"}], ensure_ascii=False),
        encoding="utf-8",
    )
    db = load_characters(tmp_path)

    char = db.characters["LowVarmChar"]
    assert len(char.components) == 1
    component = component_by_name(char, "Hair Shadow-头发阴影")
    assert component.fields == {"ib": "81e925ed"}
    assert "draw_vb" not in component.fields
    refs = {(ref.character, ref.component, ref.role) for ref in db.reverse["81e925ed"]}
    assert refs == {("LowVarmChar", "Hair Shadow-头发阴影", "ib")}
