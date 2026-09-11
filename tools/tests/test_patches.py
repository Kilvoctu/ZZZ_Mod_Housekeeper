"""Tests for tools.patches and the user-patch integration in tools.fixer."""

from pathlib import Path

from tools import changelog, mods
from tools.characters import CharacterDB
from tools.fixer import (
    FixerData,
    hash_is_outdated,
    known_hashes,
    load_fixer_data,
    resolve_hash_chain,
    scan_files,
)
from tools.model import ChangeEntry
from tools.patches import load_user_patches, parse_user_patches
from tools.tests._test_data import make_repo

PARSE_TEXT = (
    "b9f0d595 to 1132301e\n"
    "aabbccdd -> 11223344\n"
    "eeefff01 → 44556607\n"
    "# comment\n"
    "\n"
    " uppercase AA11BB22 to CC33DD44 \n"
    "not a patch line\n"
    "b9f0d595x to 1132301e\n"
    "aabbccdd -> 99887766 -> 55667788\n"
    " AA11BB22 to CC33DD44 \n"
    "aabbccdd -> 99887766\n"
)


def entry(
    from_hash, to_hash, characters, version_label="", version_index=1, role="ib"
):
    return ChangeEntry(
        from_hash=from_hash,
        to_hash=to_hash,
        version_index=version_index,
        characters=characters,
        role=role,
        version_label=version_label,
    )


def build_data(entries):
    """FixerData from parsed changelog entries, mirroring load_fixer_data."""
    return FixerData(
        chains=changelog.build_chain_index(entries),
        ib_index_changes={},
        db=CharacterDB(),
        entries=entries,
    )


def patched_data():
    """FixerData whose b9f0d595 chain is overridden by an authoritative user patch."""
    data = build_data(
        [entry("b9f0d595", "99999999", ["belle"], version_label="2.0 -> 2.1")]
    )
    data.user_patches = parse_user_patches("b9f0d595 to 1132301e")
    data.user_patches["b9f0d595"].version_index = 62
    return data


def version_for(data, hashes):
    """ModVersion for hashes, aggregating the way tools.mods analysis does."""
    ladder, latest_index = mods.version_ladder(data.entries)
    version = mods.version_for_hashes(hashes, data, ladder, latest_index)
    assert version is not None
    return version


def test_parse_user_patches_accepts_all_separators_and_comments():
    patches = parse_user_patches(PARSE_TEXT)
    assert set(patches) == {"b9f0d595", "aabbccdd", "eeefff01", "aa11bb22"}
    assert patches["b9f0d595"].to_hash == "1132301e"
    assert patches["aabbccdd"].to_hash == "99887766"
    assert patches["eeefff01"].to_hash == "44556607"
    assert patches["aa11bb22"].to_hash == "cc33dd44"
    for patch in patches.values():
        assert patch.role == "user"
        assert patch.version_label == ""


def test_load_user_patches_reads_top_level_txt_only(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "a.txt").write_text("11110000 to 22220000\n", encoding="utf-8")
    (data_dir / "z.txt").write_text(
        "11110000 to 33330000\n44440000 to 55550000\n", encoding="utf-8"
    )
    sub = data_dir / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("66660000 to 77770000\n", encoding="utf-8")
    (data_dir / "notes.md").write_text("88880000 to 99990000\n", encoding="utf-8")
    monkeypatch.setattr("tools.patches.user_patches_dir", lambda: data_dir)

    patches = load_user_patches()

    assert set(patches) == {"11110000", "44440000"}
    assert patches["11110000"].to_hash == "33330000"
    assert patches["44440000"].to_hash == "55550000"


def test_load_user_patches_tolerates_missing_dir(monkeypatch):
    monkeypatch.setattr("tools.patches.user_patches_dir", lambda: Path("no-such-dir"))
    assert load_user_patches() == {}


def test_user_patch_resolves_authoritatively():
    data = patched_data()
    patch = data.user_patches["b9f0d595"]
    steps = resolve_hash_chain("b9f0d595", "", data)
    assert steps is not None
    assert steps == [patch]
    assert hash_is_outdated("1132301e", data) is False
    assert {"b9f0d595", "1132301e"} <= known_hashes(data)


def test_scan_suggests_user_patch_rename(tmp_path):
    data = patched_data()
    dst = tmp_path / "TriggerMod.ini"
    dst.write_text(
        "[TextureOverrideBody]\nhash = b9f0d595\n", encoding="utf-8", newline=""
    )

    (plan,) = scan_files([dst], data)

    assert len(plan.suggestions) == 1
    suggestion = plan.suggestions[0]
    assert (suggestion.kind, suggestion.line_no, suggestion.old, suggestion.new) == (
        "hash",
        2,
        "b9f0d595",
        "1132301e",
    )
    assert suggestion.reason == "user patch: b9f0d595 -> 1132301e"
    assert suggestion.labels == ""


def test_analysis_counts_patched_hash_outdated():
    version = version_for(patched_data(), ["b9f0d595"])
    assert version.outdated_count == 1
    assert version.is_latest is False
    assert version.breaks_label is None


def test_load_fixer_data_attaches_patches(tmp_path, monkeypatch):
    repo_dir = make_repo(tmp_path)
    missing = tmp_path / "no-such-static-dataset.json"
    monkeypatch.setattr("tools.fixer.legacy_chains_path", lambda: missing)
    monkeypatch.setattr("tools.fixer.player_character_data_path", lambda: missing)
    monkeypatch.setattr("tools.fixer.load_user_patches", lambda: {})
    base = load_fixer_data(repo_dir)
    patch = ChangeEntry(from_hash="b9f0d595", to_hash="1132301e", role="user")
    monkeypatch.setattr(
        "tools.fixer.load_user_patches", lambda: {"b9f0d595": patch}
    )

    data = load_fixer_data(repo_dir)

    assert set(data.user_patches) == {"b9f0d595"}
    assert data.user_patches["b9f0d595"].version_index == (
        max(item.version_index for item in data.entries) + 2
    )
    assert len(data.chains) == len(base.chains)
