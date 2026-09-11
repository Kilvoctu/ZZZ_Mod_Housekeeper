"""Tests for tools.pcdata against the real static PlayerCharacterData.json dataset.

The dataset is user-provided, so the whole module is skipped when the file
is missing (mirroring the live smoke guard in test_fixer.py).
"""

import json

import pytest

from tools.model import HASH_RE
from tools.pcdata import parse_player_character_data, player_character_data_path
from tools.tests._test_data import make_repo

pytestmark = pytest.mark.skipif(
    not player_character_data_path().exists(),
    reason="static PlayerCharacterData.json dataset not present",
)


def _raw_rows():
    return json.loads(player_character_data_path().read_text(encoding="utf-8-sig"))


def test_parses_expected_row_count():
    raw_rows = _raw_rows()
    entries = parse_player_character_data(player_character_data_path())
    assert len(entries) == 244
    assert len(raw_rows) - len(entries) == 7


def test_kind_histogram_matches():
    entries = parse_player_character_data(player_character_data_path())
    assert len(entries) == 244
    assert sum(1 for e in entries if e.from_indexes is not None) == 14
    assert sum(1 for e in entries if e.from_index_counts is not None) == 8
    assert sum(1 for e in entries if e.to_index_counts is not None) == 8
    assert all(e.role == "pcdata" for e in entries)


def test_known_ib_rows_embed_expected_arrays():
    entries = parse_player_character_data(player_character_data_path())
    first = [e for e in entries if e.from_hash == "1817f3ca"]
    assert len(first) == 1
    row = first[0]
    assert row.to_hash == "c2b4ce3a"
    assert row.from_indexes == [0]
    assert row.to_indexes == [0, 31275]
    assert row.from_index_counts is None
    assert row.to_index_counts is None
    assert row.role == "pcdata"
    assert row.version_label == "importer #2"
    assert row.version_index == 2
    assert row.characters == ["belle"]
    assert row.component_label == "Body"

    second = [e for e in entries if e.from_hash == "62ed56cc"]
    assert len(second) == 1
    row = second[0]
    assert row.to_hash == "d0627e1f"
    assert row.from_indexes == [0]
    assert row.to_indexes == [0, 960]
    assert row.from_index_counts == [960]
    assert row.to_index_counts == [960, 36]
    assert row.version_index == 8
    assert row.characters == ["bellesunlight"]
    assert row.component_label == "Neck"


def test_version_index_equals_ordinal_and_label():
    entries = parse_player_character_data(player_character_data_path())
    for entry in entries:
        assert 1 <= entry.version_index <= 10
        assert entry.version_label == f"importer #{entry.version_index}"


def test_characters_normalized_lowercase():
    entries = parse_player_character_data(player_character_data_path())
    names = set()
    for entry in entries:
        assert len(entry.characters) == 1
        name = entry.characters[0]
        assert name
        assert name == name.lower()
        names.add(name)
    assert len(names) == 36


def test_empty_to_rows_are_skipped():
    entries = parse_player_character_data(player_character_data_path())
    for entry in entries:
        assert entry.to_hash
        assert HASH_RE.fullmatch(entry.to_hash)
    assert not any(e.from_hash == "5714e5e6" for e in entries)
    empty_to = [r for r in _raw_rows() if isinstance(r, dict) and r.get("To") == ""]
    assert len(empty_to) == 7


def test_file_order_preserved():
    entries = parse_player_character_data(player_character_data_path())
    again = parse_player_character_data(player_character_data_path())
    assert entries == again
    raw_rows = _raw_rows()
    assert entries[0].from_hash == raw_rows[0]["From"].lower()
    assert entries[0].to_hash == raw_rows[0]["To"].lower()


from tools import repo as repo_mod


@pytest.mark.skipif(
    not repo_mod.changelog_path(repo_mod.default_cache_dir()).exists(),
    reason="live ZZZ-Model-Hash data not cloned",
)
def test_buffer_coupled_fdc045fc_not_ingested_from_real_dataset():
    """fdc045fc is one of the buffer-coupled keys the legacy harvest drops
    wholesale; the exclusion must keep it out of the chains and out of
    entries. Without data/buffer_coupled_hashes.json it WOULD be ingested.
    """
    from tools.fixer import hash_is_outdated, load_fixer_data

    data = load_fixer_data(repo_mod.default_cache_dir(), include_pcdata=True)
    plain = load_fixer_data(repo_mod.default_cache_dir())

    assert "fdc045fc" not in data.chains
    assert [e.version_label for e in data.entries if e.from_hash == "fdc045fc"] == []
    assert not any(
        e.role == "pcdata" and e.from_hash == "fdc045fc" for e in data.entries
    )
    for hint in ("", "charabhair"):
        assert hash_is_outdated("fdc045fc", plain, hint) == hash_is_outdated(
            "fdc045fc", data, hint
        )


def test_buffer_coupled_from_hash_skipped_in_both_gap_fill_paths(tmp_path, monkeypatch):
    """Synthetic proof for both gap-fill paths with one excluded from-hash:
    a plain payload row never takes the chain-gap path and a remap payload
    row never fills ib_index_changes, in either gap-fill load."""
    from tools.fixer import load_fixer_data

    from_hash = "abcd1234"
    exclusion_path = tmp_path / "buffer_coupled_hashes.json"
    exclusion_path.write_text(json.dumps([from_hash]), encoding="utf-8")
    monkeypatch.setattr(
        "tools.pcdata.buffer_coupled_hashes_path", lambda: exclusion_path
    )
    payload = [
        {"From": from_hash, "To": "abcd5555", "Comment": "1 CharaC BodyA texcoord"},
        {
            "From": from_hash,
            "To": "abcd6666",
            "FromIndexes": "[0]",
            "ToIndexes": "[7]",
            "Comment": "2 CharaC BodyA ib",
        },
    ]
    pcdata_path = tmp_path / "PlayerCharacterData.json"
    pcdata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        "tools.fixer.legacy_chains_path", lambda: tmp_path / "legacy_chains.json"
    )
    monkeypatch.setattr(
        "tools.fixer.player_character_data_path", lambda: pcdata_path
    )

    data = load_fixer_data(
        make_repo(
            tmp_path,
            "版本 3.1 -> 3.11\n【角色丙CharaC】\nIB: eeee0001 -> eeee0002（身体）\n",
            subdir="repo-chain-gap",
        ),
        include_pcdata=True,
    )
    assert "abcd1234" not in data.chains
    assert not any(e.role == "pcdata" for e in data.entries)
    assert "abcd1234" not in data.ib_index_changes

    data = load_fixer_data(
        make_repo(
            tmp_path,
            "版本 3.1 -> 3.11\n【角色丙CharaC】\ntexcoord_vb: abcd1234 -> abcd9999\n",
            subdir="repo-remap-only",
        ),
        include_pcdata=True,
    )
    assert [e.role for e in data.chains["abcd1234"]] == ["texcoord_vb"]
    assert not any(e.role == "pcdata" for e in data.entries)
    assert "abcd1234" not in data.ib_index_changes
