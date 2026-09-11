"""Tests for tools.legacy on synthetic datasets (no repo, no network)."""

import json

from tools import changelog
from tools.characters import CharacterDB
from tools.fixer import FixerData, resolve_hash_chain
from tools.legacy import _INF, _label_sort_key, merge_entries, parse_legacy_chains
from tools.model import ChangeEntry


def test_parse_orders_labels_chronologically(tmp_path):
    path = tmp_path / "legacy_chains.json"
    path.write_text(
        json.dumps(
            [
                {
                    "from": "22222222",
                    "to": "33333333",
                    "characters": ["名字乙"],
                    "label": "1.1 -> 1.2",
                },
                {
                    "from": "11111111",
                    "to": "22222222",
                    "characters": ["名字甲"],
                    "label": "1.1 -> 1.2",
                },
                {
                    "from": "0e0e0e0e",
                    "to": "1f1f1f1f",
                    "characters": ["名字丙"],
                    "label": "1.0 -> 1.1",
                },
                {
                    "from": "5a5a5a5a",
                    "to": "5b5b5b5b",
                    "characters": ["名字丁"],
                    "label": "1.5A -> 1.5B",
                },
                {
                    "from": "notahex!",
                    "to": "1f1f1f1f",
                    "characters": [],
                    "label": "1.0 -> 1.1",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    entries = parse_legacy_chains(path)
    assert all(e.from_hash != "notahex!" for e in entries)
    assert len(entries) == 4
    assert all(e.role == "legacy" for e in entries)
    by_index: dict[int, list[ChangeEntry]] = {}
    for entry in entries:
        by_index.setdefault(entry.version_index, []).append(entry)
    assert set(by_index) == {1, 2, 3}
    assert {e.version_label for e in by_index[1]} == {"1.0 -> 1.1"}
    assert {e.version_label for e in by_index[2]} == {"1.1 -> 1.2"}
    assert {e.version_label for e in by_index[3]} == {"1.5A -> 1.5B"}
    assert [e.characters for e in by_index[1]] == [["名字丙"]]
    assert [e.characters for e in by_index[2]] == [["名字乙"], ["名字甲"]]
    assert [e.characters for e in by_index[3]] == [["名字丁"]]


def test_label_sort_key_single_version_between_arrow_labels():
    assert _label_sort_key("2.5") == ((2.5, "2.5"), (2.5, "2.5"))
    assert _label_sort_key("2.4 -> 2.5") < _label_sort_key("2.5")
    assert _label_sort_key("2.5") < _label_sort_key("2.8 -> 3.0")
    assert _label_sort_key("2.5") < _label_sort_key("2.5 -> 3.0")
    assert _label_sort_key("2.0 → 2.1") == _label_sort_key("2.0 -> 2.1")
    assert _label_sort_key("nonsense") == ((_INF, ""), (_INF, ""))
    assert _label_sort_key("nonsense") > _label_sort_key("2.8 -> 3.0")


def test_parse_orders_single_version_labels(tmp_path):
    path = tmp_path / "legacy_chains.json"
    path.write_text(
        json.dumps(
            [
                {
                    "from": "11111111",
                    "to": "22222222",
                    "characters": ["名字甲"],
                    "label": "2.8 -> 3.0",
                },
                {
                    "from": "33333333",
                    "to": "44444444",
                    "characters": ["名字乙"],
                    "label": "2.5",
                },
                {
                    "from": "55555555",
                    "to": "66666666",
                    "characters": ["名字丙"],
                    "label": "2.4 -> 2.5",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    entries = parse_legacy_chains(path)
    assert [e.version_label for e in entries] == ["2.4 -> 2.5", "2.5", "2.8 -> 3.0"]
    assert [e.version_index for e in entries] == [1, 2, 3]
    assert all(e.role == "legacy" for e in entries)


def test_merge_shifts_real_entries():
    legacy = [
        ChangeEntry(
            from_hash="1a1a1a1a",
            to_hash="2b2b2b2b",
            version_index=1,
            role="legacy",
            version_label="1.0 -> 1.1",
        ),
        ChangeEntry(
            from_hash="2b2b2b2b",
            to_hash="3c3c3c3c",
            version_index=2,
            role="legacy",
            version_label="1.1 -> 1.2",
        ),
    ]
    real = [
        ChangeEntry(
            from_hash="a1a1a1a1",
            to_hash="b2b2b2b2",
            version_index=1,
            role="ib",
            version_label="2.0 -> 2.1",
        ),
        ChangeEntry(
            from_hash="b2b2b2b2",
            to_hash="c3c3c3c3",
            version_index=2,
            role="ib",
            version_label="2.0 -> 2.1",
        ),
        ChangeEntry(
            from_hash="c3c3c3c3",
            to_hash="d4d4d4d4",
            version_index=3,
            role="ib",
            version_label="2.0 -> 2.1",
        ),
    ]
    merged = merge_entries(legacy, real)
    assert merged[:2] == legacy
    assert [(e.from_hash, e.version_index) for e in merged[2:]] == [
        ("a1a1a1a1", 3),
        ("b2b2b2b2", 4),
        ("c3c3c3c3", 5),
    ]
    indexes = [e.version_index for e in merged]
    assert indexes == sorted(indexes) == [1, 2, 3, 4, 5]


def test_chain_resolves_legacy_then_real():
    legacy = [
        ChangeEntry(
            from_hash="11111111",
            to_hash="22222222",
            version_index=1,
            role="legacy",
            version_label="1.0 -> 1.1",
            characters=["名字甲charaa"],
        )
    ]
    real = changelog.parse_changelog(
        "版本 2.0 -> 2.1\n【名字甲charaa】\nIB: 22222222 -> 33333333\n"
    )
    assert [(e.from_hash, e.to_hash, e.version_index) for e in real] == [
        ("22222222", "33333333", 1)
    ]
    merged = merge_entries(legacy, real)
    data = FixerData(
        chains=changelog.build_chain_index(merged),
        ib_index_changes={},
        db=CharacterDB(),
        entries=merged,
    )
    steps = resolve_hash_chain("11111111", "名字甲charaa", data)
    assert steps is not None
    assert [(s.from_hash, s.to_hash, s.version_index, s.role) for s in steps] == [
        ("11111111", "22222222", 1, "legacy"),
        ("22222222", "33333333", 2, "ib"),
    ]
