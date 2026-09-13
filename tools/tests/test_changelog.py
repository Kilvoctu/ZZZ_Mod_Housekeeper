"""Tests for tools.changelog on synthetic changelog text (no repo dependency)."""

from tools import changelog

SYNTHETIC = """\
===============================================================================
  版本 3.1 -> 3.11
===============================================================================
【希格莉德Sigrid】
IB: a23aa8a3 -> 38daef11（身体）
  draw_vb: 01b35c45 -> d0bf0e87
  blend_vb: 8c0622d7 -> 018ea72c
  object_indexes: [0, 42759] -> [0, 42963]

=======================================================================
  版本 2.0 → 2.1
=======================================================================
【月城柳Yanagi】
IB: 44d9123b
  draw_vb: （新增） -> 178a1ff8
【哲Wise / 哲-皮肤WiseSkin（头发共用）】
  IB: 1fdaf388
    Texcoord: ebe9f31b -> c83b6cbf
  IB: cb272754 -> 0ec31440（头发）※ 模型变化不可修复
  IB: d9e49957（身体）/b4f608f5(转轮轴承)
  Diffuse: b07c43ef -> 8874c184

--------------------
作者: someone
网址: https://example.com
"""


def parse():
    return changelog.parse_changelog(SYNTHETIC)


def test_exactly_expected_transitions():
    entries = parse()
    assert {(e.role, e.from_hash, e.to_hash) for e in entries} == {
        ("ib", "a23aa8a3", "38daef11"),
        ("draw_vb", "01b35c45", "d0bf0e87"),
        ("blend_vb", "8c0622d7", "018ea72c"),
        ("object_indexes", "a23aa8a3", "38daef11"),
        ("draw_vb", None, "178a1ff8"),
        ("texcoord", "ebe9f31b", "c83b6cbf"),
        ("ib", "cb272754", "0ec31440"),
        ("diffuse", "b07c43ef", "8874c184"),
    }
    assert len(entries) == 8


def test_no_entry_for_unchanged_ib_or_trailer():
    entries = parse()
    assert all("44d9123b" not in (e.from_hash or "") + (e.to_hash or "") for e in entries)


def test_version_indexes_chronological_despite_file_order():
    for entry in parse():
        if entry.version_label == "2.0 → 2.1":
            assert entry.version_index == 1
        else:
            assert entry.version_label == "3.1 -> 3.11"
            assert entry.version_index == 2
    indexes = [entry.version_index for entry in parse()]
    assert indexes == sorted(indexes)


def test_characters_split_and_normalized():
    entries = parse()
    diffuse = next(e for e in entries if e.role == "diffuse")
    assert diffuse.characters == ["哲wise", "哲皮肤wiseskin"]
    sigrid = next(e for e in entries if e.role == "ib" and e.from_hash == "a23aa8a3")
    assert sigrid.characters == ["希格莉德sigrid"]
    yanagi = next(e for e in entries if e.role == "draw_vb" and e.from_hash is None)
    assert yanagi.characters == ["月城柳yanagi"]


def test_component_labels():
    entries = parse()
    body = next(e for e in entries if e.role == "ib" and e.from_hash == "a23aa8a3")
    assert body.component_label == "身体"
    hair = next(e for e in entries if e.role == "ib" and e.from_hash == "cb272754")
    assert hair.component_label == "头发"


def test_object_indexes_attributed_to_preceding_ib_transition():
    idx = next(e for e in parse() if e.role == "object_indexes")
    assert idx.from_indexes == [0, 42759]
    assert idx.to_indexes == [0, 42963]
    assert idx.from_hash == "a23aa8a3"
    assert idx.to_hash == "38daef11"


def test_build_chain_index_buckets():
    entries = parse()
    index = changelog.build_chain_index(entries)
    assert set(index) == {
        "a23aa8a3",
        "01b35c45",
        "8c0622d7",
        "ebe9f31b",
        "cb272754",
        "b07c43ef",
    }
    assert [entry.role for entry in index["a23aa8a3"]] == ["ib", "object_indexes"]
    for bucket in index.values():
        versions = [entry.version_index for entry in bucket]
        assert versions == sorted(versions)


def test_crlf_text_same_result():
    crlf_entries = changelog.parse_changelog(SYNTHETIC.replace("\n", "\r\n"))
    assert crlf_entries == parse()


def test_parse_changelog_file(tmp_path):
    path = tmp_path / "Hash变动日志.txt"
    path.write_text(SYNTHETIC, encoding="utf-8")
    assert changelog.parse_changelog_file(path) == parse()


def test_parse_face_texcoord_transitions():
    text = (
        "===============================================================================\n"
        "  版本 3.1 -> 3.2\n"
        "===============================================================================\n"
        "【橘福福jufufu】\n"
        "IB: 321768df（脸）\n"
        "  Texcoord: 8267358b -> 768c9ec4\n"
        "\n"
        "【琉音Dialyn】\n"
        "IB: d860525e（眉毛）\n"
        "  texcoord_vb: d90368ed -> 27fd9193\n"
        "IB: facb2461（脸部）\n"
        "  texcoord_vb: f6c5296e -> dafc9647\n"
        "【Someone】\n"
        "IB: 11111111 -> 22222222（身体）\n"
        "  texcoord_vb: 33333333 -> 44444444\n"
        "  Texcoord: 55555555 -> 66666666\n"
        "  Texcoord: 新增 -> 77777777\n"
    )
    transitions = changelog.parse_face_texcoord_transitions(text)
    assert transitions == {"8267358b": "768c9ec4", "f6c5296e": "dafc9647"}
