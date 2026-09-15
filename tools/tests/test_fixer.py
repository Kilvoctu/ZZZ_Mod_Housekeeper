"""Tests for tools.fixer: scanning, applying, reverting, encodings and guards."""

import os
import re
import shutil
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

import pytest

from tools import backups, changelog, fixer, repo
from tools.backups import (
    _canonical_dirs,
    _resolve_live_path,
    backup_path_for,
    collect_backup_chains,
    collect_backup_chains_for,
    default_backups_dir,
    delete_store_folder,
    folder_key,
    move_file,
    parse_store_backup_name,
    prune_empty_store_folders,
    retarget_store_folder,
    store_folder_for_mod,
    store_folder_to_open,
    store_root,
)
from tools.characters import CharacterDB, HashRef
from tools.fixer import (
    INDEX_WARNING_KIND,
    FilePlan,
    FixerData,
    apply_plan,
    collect_texture_override_hashes,
    collect_texture_override_hints,
    hash_is_outdated,
    hint_matches,
    known_hashes,
    load_fixer_data,
    resolve_hash_chain,
    revert_backups,
    scan_files,
    scan_folder,
    walk_index_value,
)
from tools.legacy import merge_entries
from tools.model import ChangeEntry, Character, Component, FixSuggestion
from tools.structure import latin_suffix
from tools.tests._test_data import SNAPSHOT_CHANGELOG, make_repo

SAMPLE_CHANGELOG = """===============================================================================
  版本 3.1 -> 3.11
===============================================================================
【Sample】
IB: a23aa8a3 -> 38daef11（身体）
  draw_vb: 01b35c45 -> d0bf0e87
  texcoord_vb: f6474154 -> 08ddaed3
  blend_vb: 8c0622d7 -> 018ea72c
  object_indexes: [0, 42759] -> [0, 42963]
  object_classifications: ['A', 'B']"""

SAMPLE_INI = (
    "; Sample\r\n"
    "\r\n"
    "; 注释：示例 -------------------------\r\n"
    "\r\n"
    "[Constants]\r\n"
    "global $active\r\n"
    "global $first_run = 1\r\n"
    "\r\n"
    "[TextureOverrideSampleTopBlend]\r\n"
    "hash = 8c0622d7\r\n"
    "$active = 1\r\n"
    "handling = skip\r\n"
    "if DRAW_TYPE == 2 || DRAW_TYPE == 4\r\n"
    "\tvb1 = ResourceSampleTopTexcoord\r\n"
    "\tvb2 = ResourceSampleTopBlend\r\n"
    "\tchecktextureoverride = ib\r\n"
    "endif\r\n"
    "\r\n"
    "[TextureOverrideSampleTopTexcoord]\r\n"
    "hash = f6474154\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleTopPosition\r\n"
    "vb1 = ResourceSampleTopTexcoord\r\n"
    "drawindexed = 91462, 0, 0\r\n"
    "\r\n"
    "[TextureOverrideSampleTopVertexLimitRaise]\r\n"
    "hash = 01b35c45\r\n"
    "override_vertex_count = 91462\r\n"
    "override_byte_stride = 40\r\n"
    "\r\n"
    "[TextureOverrideSampleTopA]\r\n"
    "hash = a23aa8a3\r\n"
    "match_first_index = 0\r\n"
    "match_index_count = 42759\r\n"
    "handling = skip\r\n"
    "ib = ResourceSampleTopAIB\r\n"
    "run = CommandListBodyADiffuse\r\n"
    "\r\n"
    "[TextureOverrideSampleTopB]\r\n"
    "hash = a23aa8a3\r\n"
    "match_first_index = 42759\r\n"
    "match_index_count = 288\r\n"
    "handling = skip\r\n"
    "ib = ResourceSampleTopBIB\r\n"
    "run = CommandList\\ZZMI\\SetTextures\r\n"
    "\r\n"
    "[ResourceSampleTopBlend]\r\n"
    "type = Buffer\r\n"
    "stride = 32\r\n"
    "filename = SampleTopBlend.buf\r\n"
    "\r\n"
    "[TextureOverrideSampleCollarPosition]\r\n"
    "hash = AAAA0001\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleCollarPosition\r\n"
    "drawindexed = 360, 0, 0\r\n"
    "\r\n"
    "[TextureOverrideSampleCharmPosition]\r\n"
    "hash = DDDD0004\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleCharmPosition\r\n"
    "drawindexed = 288, 0, 0\r\n"
    "\r\n"
    "[TextureOverrideSampleBadgePosition]\r\n"
    "hash = EEEE0005\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleBadgePosition\r\n"
    "drawindexed = 132, 0, 0\r\n"
)

SAMPLE_FIXED = (
    "; Sample\r\n"
    "\r\n"
    "; 注释：示例 -------------------------\r\n"
    "\r\n"
    "[Constants]\r\n"
    "global $active\r\n"
    "global $first_run = 1\r\n"
    "\r\n"
    "[TextureOverrideSampleTopBlend]\r\n"
    "hash = 018ea72c\r\n"
    "$active = 1\r\n"
    "handling = skip\r\n"
    "if DRAW_TYPE == 2 || DRAW_TYPE == 4\r\n"
    "\tvb1 = ResourceSampleTopTexcoord\r\n"
    "\tvb2 = ResourceSampleTopBlend\r\n"
    "\tchecktextureoverride = ib\r\n"
    "endif\r\n"
    "\r\n"
    "[TextureOverrideSampleTopTexcoord]\r\n"
    "hash = 08ddaed3\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleTopPosition\r\n"
    "vb1 = ResourceSampleTopTexcoord\r\n"
    "drawindexed = 91462, 0, 0\r\n"
    "\r\n"
    "[TextureOverrideSampleTopVertexLimitRaise]\r\n"
    "hash = d0bf0e87\r\n"
    "override_vertex_count = 91462\r\n"
    "override_byte_stride = 40\r\n"
    "\r\n"
    "[TextureOverrideSampleTopA]\r\n"
    "hash = 38daef11\r\n"
    "match_first_index = 0\r\n"
    "match_index_count = 42963\r\n"
    "handling = skip\r\n"
    "ib = ResourceSampleTopAIB\r\n"
    "run = CommandListBodyADiffuse\r\n"
    "\r\n"
    "[TextureOverrideSampleTopB]\r\n"
    "hash = 38daef11\r\n"
    "match_first_index = 42963\r\n"
    "match_index_count = 288\r\n"
    "handling = skip\r\n"
    "ib = ResourceSampleTopBIB\r\n"
    "run = CommandList\\ZZMI\\SetTextures\r\n"
    "\r\n"
    "[ResourceSampleTopBlend]\r\n"
    "type = Buffer\r\n"
    "stride = 32\r\n"
    "filename = SampleTopBlend.buf\r\n"
    "\r\n"
    "[TextureOverrideSampleCollarPosition]\r\n"
    "hash = cccc0003\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleCollarPosition\r\n"
    "drawindexed = 360, 0, 0\r\n"
    "\r\n"
    "[TextureOverrideSampleCharmPosition]\r\n"
    "hash = cccc0003\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleCharmPosition\r\n"
    "drawindexed = 288, 0, 0\r\n"
    "\r\n"
    "[TextureOverrideSampleBadgePosition]\r\n"
    "hash = EEEE0005\r\n"
    "handling = skip\r\n"
    "vb0 = ResourceSampleBadgePosition\r\n"
    "drawindexed = 132, 0, 0\r\n"
)

EXPECTED_SAMPLE = [
    ("hash", 10, "8c0622d7", "018ea72c", "3.1 -> 3.11"),
    ("hash", 20, "f6474154", "08ddaed3", "3.1 -> 3.11"),
    ("hash", 27, "01b35c45", "d0bf0e87", "3.1 -> 3.11"),
    ("hash", 32, "a23aa8a3", "38daef11", "3.1 -> 3.11"),
    ("index_warning", 32, "a23aa8a3", "a23aa8a3", ""),
    ("match_index_count", 34, "42759", "42963", ""),
    ("hash", 40, "a23aa8a3", "38daef11", "3.1 -> 3.11"),
    ("match_first_index", 41, "42759", "42963", ""),
    ("hash", 53, "aaaa0001", "cccc0003", "1.1 -> 1.2, 1.2 -> 2.5"),
    ("hash", 59, "dddd0004", "cccc0003", "1.0 -> 1.2"),
]


def build_data(entries):
    """Build FixerData from parsed changelog entries, mirroring load_fixer_data
    (scan does not use the character db, so an empty CharacterDB is fine).
    Passing entries keeps known_hashes classification consistent with load_fixer_data."""
    chains = changelog.build_chain_index(entries)
    ib_index_changes: dict[str, list[ChangeEntry]] = {}
    for item in entries:
        if (
            item.role == "object_indexes"
            and item.from_hash is not None
            and item.from_indexes is not None
            and item.to_indexes is not None
        ):
            ib_index_changes.setdefault(item.from_hash.lower(), []).append(item)
    for bucket in ib_index_changes.values():
        bucket.sort(key=lambda change: change.version_index)
    return FixerData(chains=chains, ib_index_changes=ib_index_changes, db=CharacterDB(), entries=entries)


def entry(from_hash, to_hash, characters, version_index=1, role="ib"):
    return ChangeEntry(
        from_hash=from_hash,
        to_hash=to_hash,
        version_index=version_index,
        characters=characters,
        role=role,
    )


def index_entry(
    from_hash, to_hash, version_index, from_indexes, to_indexes, characters, role="object_indexes"
):
    return ChangeEntry(
        from_hash=from_hash,
        to_hash=to_hash,
        version_index=version_index,
        characters=characters,
        role=role,
        from_indexes=from_indexes,
        to_indexes=to_indexes,
    )


def rel_path(plan_path, root):
    return Path(plan_path).relative_to(root).as_posix()


def quiet(_message):
    pass


@pytest.fixture(scope="module")
def snapshot_data():
    """FixerData built from the frozen changelog snapshot (never the live repo)."""
    return build_data(changelog.parse_changelog(SNAPSHOT_CHANGELOG))


SAMPLE_LEGACY_EDGES = (
    ("dddd0004", "cccc0003", "1.0 -> 1.2"),
    ("aaaa0001", "bbbb0002", "1.1 -> 1.2"),
    ("bbbb0002", "cccc0003", "1.2 -> 2.5"),
)


@pytest.fixture(scope="module")
def sample_data():
    """FixerData built from the in-line sample changelog (the sample's chains)."""
    legacy = [
        ChangeEntry(
            from_hash=from_hash,
            to_hash=to_hash,
            version_index=index,
            characters=["sample"],
            role="legacy",
            version_label=label,
        )
        for index, (from_hash, to_hash, label) in enumerate(SAMPLE_LEGACY_EDGES, start=1)
    ]
    return build_data(merge_entries(legacy, changelog.parse_changelog(SAMPLE_CHANGELOG)))


def write_sample_ini(tmp_path, name="SampleMod.ini"):
    """Write SAMPLE_INI byte-exactly (no newline translation) and return the path."""
    dst = tmp_path / name
    dst.write_text(SAMPLE_INI, encoding="utf-8", newline="")
    return dst


def sample_ini_hashes():
    """Unique lowercased hash values carried by SAMPLE_INI, in file order."""
    return [
        "8c0622d7",
        "f6474154",
        "01b35c45",
        "a23aa8a3",
        "aaaa0001",
        "dddd0004",
        "eeee0005",
    ]


def test_sample_apply_exact_bytes(tmp_path, sample_data):
    dst = write_sample_ini(tmp_path)
    store = tmp_path / "store"
    (plan,) = scan_folder(tmp_path, sample_data)
    logs: list[str] = []
    assert apply_plan(
        plan, sample_data, store_dir=store, mods_dir=tmp_path, log=logs.append
    ) is True
    key_dir = store_root(store, tmp_path)
    stored = list(key_dir.glob("SampleMod.ini -- *.bak"))
    assert len(stored) == 1
    assert re.fullmatch(
        r"SampleMod\.ini -- \d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2}\.bak", stored[0].name
    )
    assert stored[0].read_bytes() == SAMPLE_INI.encode("utf-8")
    assert list(tmp_path.glob("SampleMod.ini -- *.bak")) == []
    assert dst.read_bytes() == SAMPLE_FIXED.encode("utf-8")
    expected = [
        (
            "warning: IB a23aa8a3 -> 38daef11: the mod ships custom .ib/.buf buffers; "
            "a structurally changed mesh needs re-dumped binaries - ini fixes alone "
            "will not render correctly"
        ),
        *(
            f"{old} to {new} [{labels}]"
            for kind, _line_no, old, new, labels in EXPECTED_SAMPLE
            if kind == "hash"
        ),
    ]
    assert logs[:-1] == expected
    assert logs[-1].startswith("fixed ")


def test_sample_apply_changes_only_planned_lines(tmp_path, sample_data):
    dst = write_sample_ini(tmp_path)
    store = tmp_path / "store"
    (plan,) = scan_folder(tmp_path, sample_data)
    planned = sorted({suggestion.line_no for suggestion in plan.suggestions})
    assert planned == sorted({row[1] for row in EXPECTED_SAMPLE})
    assert apply_plan(
        plan, sample_data, store_dir=store, mods_dir=tmp_path, log=quiet
    ) is True
    old_rows = SAMPLE_INI.splitlines()
    new_rows = dst.read_bytes().decode("utf-8").splitlines()
    assert len(new_rows) == len(old_rows)
    changed = [
        line_no
        for line_no, (old_row, new_row) in enumerate(zip(old_rows, new_rows), start=1)
        if old_row != new_row
    ]
    assert changed == planned
    for line_no, (old_row, new_row) in enumerate(zip(old_rows, new_rows), start=1):
        if line_no not in planned:
            assert new_row == old_row


def test_sample_scan_clears_after_apply(tmp_path, sample_data):
    dst = write_sample_ini(tmp_path)
    store = tmp_path / "store"
    (plan,) = scan_folder(tmp_path, sample_data)
    assert apply_plan(
        plan, sample_data, store_dir=store, mods_dir=tmp_path, log=quiet
    ) is True
    assert dst.read_bytes() == SAMPLE_FIXED.encode("utf-8")
    assert scan_folder(tmp_path, sample_data) == []
    assert (
        apply_plan(
            FilePlan(path=str(dst)),
            sample_data,
            store_dir=store,
            mods_dir=tmp_path,
            log=quiet,
        )
        is False
    )
    assert dst.read_bytes() == SAMPLE_FIXED.encode("utf-8")


def test_sample_legacy_classification(tmp_path, sample_data):
    dst = write_sample_ini(tmp_path)
    hints_map = collect_texture_override_hints(dst)
    collar_hint = min(hints_map["aaaa0001"])
    charm_hint = min(hints_map["dddd0004"])
    known = known_hashes(sample_data)
    status: dict[str, str] = {}
    for h in sorted(set(sample_ini_hashes()) | known):
        own = hints_map.get(h, set())
        contexts = [
            resolve_hash_chain(h, hint, sample_data)
            for hint in sorted(own or {collar_hint})
        ]
        if any(resolved is None or resolved for resolved in contexts):
            status[h] = "outdated"
        elif h in known:
            status[h] = "current"
        else:
            status[h] = "unknown"
    assert status == {
        "8c0622d7": "outdated",
        "f6474154": "outdated",
        "01b35c45": "outdated",
        "a23aa8a3": "outdated",
        "aaaa0001": "outdated",
        "dddd0004": "outdated",
        "bbbb0002": "outdated",
        "cccc0003": "current",
        "018ea72c": "current",
        "08ddaed3": "current",
        "d0bf0e87": "current",
        "38daef11": "current",
        "eeee0005": "unknown",
    }
    collar_steps = resolve_hash_chain("aaaa0001", collar_hint, sample_data)
    assert collar_steps is not None
    assert [(s.role, s.version_label, s.to_hash) for s in collar_steps] == [
        ("legacy", "1.1 -> 1.2", "bbbb0002"),
        ("legacy", "1.2 -> 2.5", "cccc0003"),
    ]
    charm_steps = resolve_hash_chain("dddd0004", charm_hint, sample_data)
    assert charm_steps is not None
    assert [(s.role, s.version_label, s.to_hash) for s in charm_steps] == [
        ("legacy", "1.0 -> 1.2", "cccc0003"),
    ]
    for gated in ("aaaa0001", "dddd0004", "bbbb0002"):
        assert resolve_hash_chain(gated, "", sample_data) == []
    assert hash_is_outdated("aaaa0001", sample_data, collar_hint) is True
    assert hash_is_outdated("dddd0004", sample_data, charm_hint) is True
    assert hash_is_outdated("cccc0003", sample_data, collar_hint) is False
    (plan,) = scan_folder(tmp_path, sample_data)
    assert [
        (s.kind, s.line_no, s.old, s.new, s.labels) for s in plan.suggestions
    ] == EXPECTED_SAMPLE
    assert not any(s.line_no == 65 for s in plan.suggestions)
    by_line = {s.line_no: s for s in plan.suggestions}
    assert by_line[53].reason == (
        "changelog 1.1 -> 1.2, 1.2 -> 2.5: aaaa0001 -> bbbb0002 -> cccc0003"
    )
    assert by_line[59].reason == "changelog 1.0 -> 1.2: dddd0004 -> cccc0003"
    assert by_line[53].new == by_line[59].new == "cccc0003"
    assert SAMPLE_FIXED.splitlines()[64] == "hash = EEEE0005"


def test_apply_plan_backup_false_skips_store(tmp_path, sample_data):
    dst = write_sample_ini(tmp_path)
    store = tmp_path / "store"
    (plan,) = scan_folder(tmp_path, sample_data)
    assert apply_plan(
        plan, sample_data, store_dir=store, mods_dir=tmp_path, log=quiet, backup=False
    ) is True
    assert list(store_root(store, tmp_path).glob("*.bak")) == []
    assert not store_root(store, tmp_path).exists()
    fixed_text, _encoding = fixer._decode_ini(dst.read_bytes())
    reference_text, _reference_encoding = fixer._decode_ini(
        SAMPLE_FIXED.encode("utf-8")
    )
    assert fixed_text.lstrip("\ufeff") == reference_text.lstrip("\ufeff")


def test_idempotent_rescan(tmp_path, snapshot_data):
    dst = write_sample_ini(tmp_path)
    store = tmp_path / "store"
    (plan,) = scan_folder(tmp_path, snapshot_data)
    apply_plan(plan, snapshot_data, store_dir=store, mods_dir=tmp_path, log=quiet)
    assert scan_folder(tmp_path, snapshot_data) == []
    assert (
        apply_plan(
            FilePlan(path=str(dst)),
            snapshot_data,
            store_dir=store,
            mods_dir=tmp_path,
            log=quiet,
        )
        is False
    )
    assert (
        len(list(store_root(store, tmp_path).glob("SampleMod.ini -- *.bak"))) == 1
    )


def test_apply_plan_rollback_restores_original(tmp_path, snapshot_data, monkeypatch):
    dst = write_sample_ini(tmp_path)
    store = tmp_path / "store"
    (plan,) = scan_folder(tmp_path, snapshot_data)
    original = dst.read_bytes()

    def broken_write(_self, _data):
        raise OSError("simulated write failure")

    monkeypatch.setattr(Path, "write_bytes", broken_write)
    with pytest.raises(OSError):
        apply_plan(
            plan, snapshot_data, store_dir=store, mods_dir=tmp_path, log=quiet
        )
    monkeypatch.undo()
    assert dst.read_bytes() == original
    assert list(store_root(store, tmp_path).glob("DISABLED_versionfix_*")) == []


S1 = "state one\n"
S2 = "state two\n"
S3 = "state three\n"


def store_layout(tmp_path):
    """(mods dir, store dir, the mods folder's store folder) for chain tests."""
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    mods.mkdir()
    return mods, store, store_root(store, mods)


def test_collect_backup_chains_single(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    live = mods / "a.ini"
    live.write_text(S2, encoding="utf-8")
    backup = key_dir / "DISABLED_versionfix_1000-a.ini"
    backup.parent.mkdir(parents=True)
    backup.write_text(S1, encoding="utf-8")
    (key_dir / "DISABLED_BACKUP_1727259311.9821017e.ini").write_text(
        "foreign", encoding="utf-8"
    )
    nested = key_dir / "DISABLED_versionfix_1"
    nested.mkdir()
    (nested / "plain.ini").write_text("plain", encoding="utf-8")

    chains = collect_backup_chains(mods, store)

    assert list(chains) == [live]
    chain = chains[live]
    assert chain.live == live
    assert chain.backups == [(1000, backup)]


def test_collect_backup_chains_deep(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    key_dir.mkdir(parents=True)
    live = mods / "b.ini"
    live.write_text(S3, encoding="utf-8")
    b2000 = key_dir / "DISABLED_versionfix_2000-b.ini"
    b2000.write_text(S2, encoding="utf-8")
    b1000 = key_dir / "DISABLED_versionfix_1000-b.ini"
    b1000.write_text(S1, encoding="utf-8")

    chains = collect_backup_chains(mods, store)

    assert list(chains) == [live]
    chain = chains[live]
    assert chain.live == live
    assert chain.backups == [(1000, b1000), (2000, b2000)]


def test_collect_backup_chains_mirrors_mods_tree(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    live = mods / "pack" / "part.ini"
    live.parent.mkdir(parents=True)
    live.write_text(S3, encoding="utf-8")
    b1000 = key_dir / "pack" / "DISABLED_versionfix_1000-part.ini"
    b1000.parent.mkdir(parents=True)
    b1000.write_text(S1, encoding="utf-8")

    chains = collect_backup_chains(mods, store)

    assert list(chains) == [live]
    assert chains[live].backups == [(1000, b1000)]


def test_collect_backup_chains_orphan(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    backup = key_dir / "DISABLED_versionfix_3000-gone.ini"
    backup.parent.mkdir(parents=True)
    backup.write_text(S1, encoding="utf-8")

    chains = collect_backup_chains(mods, store)

    assert list(chains) == [mods / "gone.ini"]
    chain = chains[mods / "gone.ini"]
    assert chain.live == mods / "gone.ini"
    assert not chain.live.exists()
    assert chain.backups == [(3000, backup)]


def test_collect_backup_chains_empty(tmp_path):
    mods, store, _key_dir = store_layout(tmp_path)
    assert collect_backup_chains(mods, store) == {}
    store_root(store, mods).mkdir(parents=True)
    assert collect_backup_chains(mods, store) == {}


def test_collect_backup_chains_subtree(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    for sub in ("subtree_a", "subtree_b"):
        live = mods / sub / "pack" / "part.ini"
        live.parent.mkdir(parents=True)
        live.write_text(S3, encoding="utf-8")
        b1000 = key_dir / sub / "pack" / "DISABLED_versionfix_1000-part.ini"
        b1000.parent.mkdir(parents=True)
        b1000.write_text(S1, encoding="utf-8")

    both = collect_backup_chains(mods, store)
    assert list(both) == [
        mods / "subtree_a" / "pack" / "part.ini",
        mods / "subtree_b" / "pack" / "part.ini",
    ]

    only_a = collect_backup_chains(mods, store, subtree=Path("subtree_a"))
    live_a = mods / "subtree_a" / "pack" / "part.ini"
    assert list(only_a) == [live_a]
    assert only_a[live_a].backups == [
        (1000, key_dir / "subtree_a" / "pack" / "DISABLED_versionfix_1000-part.ini")
    ]

    assert collect_backup_chains(mods, store, subtree=Path("subtree_c")) == {}


def test_revert_backups_one_level(tmp_path):
    mods, _store, key_dir = store_layout(tmp_path)
    key_dir.mkdir(parents=True)
    live = mods / "b.ini"
    live.write_text(S3, encoding="utf-8")
    b2000 = key_dir / "DISABLED_versionfix_2000-b.ini"
    b2000.write_text(S2, encoding="utf-8")
    b1000 = key_dir / "DISABLED_versionfix_1000-b.ini"
    b1000.write_text(S1, encoding="utf-8")

    assert revert_backups([(live, b2000)], log=quiet) == 1

    assert live.read_text(encoding="utf-8") == S2
    assert not b2000.exists()
    assert b1000.read_text(encoding="utf-8") == S1


def test_revert_backups_deep_original(tmp_path):
    mods, _store, key_dir = store_layout(tmp_path)
    key_dir.mkdir(parents=True)
    live = mods / "b.ini"
    live.write_text(S3, encoding="utf-8")
    b2000 = key_dir / "DISABLED_versionfix_2000-b.ini"
    b2000.write_text(S2, encoding="utf-8")
    b1000 = key_dir / "DISABLED_versionfix_1000-b.ini"
    b1000.write_text(S1, encoding="utf-8")

    assert revert_backups([(live, b1000)], log=quiet) == 1

    assert live.read_text(encoding="utf-8") == S1
    assert b2000.read_text(encoding="utf-8") == S2
    assert not b1000.exists()


def test_revert_backups_orphan_restore(tmp_path):
    mods, _store, key_dir = store_layout(tmp_path)
    backup = key_dir / "DISABLED_versionfix_3000-gone.ini"
    backup.parent.mkdir(parents=True)
    backup.write_text(S1, encoding="utf-8")
    live = mods / "gone.ini"

    assert revert_backups([(live, backup)], log=quiet) == 1

    assert live.read_text(encoding="utf-8") == S1
    assert not backup.exists()


def test_revert_backups_multiple_choices(tmp_path):
    mods, _store, key_dir = store_layout(tmp_path)
    key_dir.mkdir(parents=True)
    live_a = mods / "a.ini"
    live_a.write_text(S3, encoding="utf-8")
    bkp_a = key_dir / "DISABLED_versionfix_1000-a.ini"
    bkp_a.write_text(S1, encoding="utf-8")
    live_b = mods / "b.ini"
    live_b.write_text(S2, encoding="utf-8")
    bkp_b = key_dir / "DISABLED_versionfix_2000-b.ini"
    bkp_b.write_text(S1, encoding="utf-8")

    assert (
        revert_backups([(live_a, bkp_a), (live_b, bkp_b)], log=quiet) == 2
    )

    assert live_a.read_text(encoding="utf-8") == S1
    assert live_b.read_text(encoding="utf-8") == S1
    assert not bkp_a.exists()
    assert not bkp_b.exists()


@pytest.mark.parametrize("encoding", ["gbk", "utf-8-sig", "utf-8"])
def test_encoding_preserved(tmp_path, snapshot_data, encoding):
    text = (
        "; mod ini\r\n"
        "; 中文注释：示例\r\n"
        "[TextureOverrideSampleTopBlend]\r\n"
        "hash = 8c0622d7\r\n"
    )
    src = tmp_path / "Mod.ini"
    src.write_bytes(text.encode(encoding))
    (plan,) = scan_folder(tmp_path, snapshot_data)
    assert (
        apply_plan(
            plan,
            snapshot_data,
            store_dir=tmp_path / "store",
            mods_dir=tmp_path,
            log=quiet,
        )
        is True
    )

    new_bytes = src.read_bytes()
    if encoding == "utf-8-sig":
        assert new_bytes.startswith(b"\xef\xbb\xbf")
    else:
        assert not new_bytes.startswith(b"\xef\xbb\xbf")
    new_text = new_bytes.decode(encoding)
    assert "018ea72c" in new_text
    assert "8c0622d7" not in new_text
    assert "中文注释：示例" in new_text
    assert "\r\n" in new_text


def test_scan_folder_always_scans_disabled_content(tmp_path, snapshot_data):
    fixable = "[TextureOverrideSampleTopBlend]\nhash = 8c0622d7\n"
    (tmp_path / "a.ini").write_text(fixable, encoding="utf-8")
    (tmp_path / "DISABLED_b.ini").write_text(fixable, encoding="utf-8")
    disabled_dir = tmp_path / "DISABLED_dir"
    disabled_dir.mkdir()
    (disabled_dir / "c.ini").write_text(fixable, encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "e.ini").write_text(fixable, encoding="utf-8")
    nested_disabled = sub / "DISABLED_nested"
    nested_disabled.mkdir()
    (nested_disabled / "f.ini").write_text(fixable, encoding="utf-8")

    plans = scan_folder(tmp_path, snapshot_data)
    assert {rel_path(plan.path, tmp_path) for plan in plans} == {
        "a.ini",
        "DISABLED_b.ini",
        "DISABLED_dir/c.ini",
        "sub/e.ini",
        "sub/DISABLED_nested/f.ini",
    }


def test_scan_folder_skips_all_backup_conventions(tmp_path, snapshot_data):
    fixable = "[TextureOverrideSampleTopBlend]\nhash = 8c0622d7\n"
    stale = tmp_path / "part.ini"
    stale.write_text(fixable, encoding="utf-8")
    (tmp_path / "DISABLED_versionfix_123456-part.ini").write_text(
        fixable, encoding="utf-8"
    )
    backup_dir = tmp_path / "sub" / "DISABLED_versionfix_123"
    backup_dir.mkdir(parents=True)
    (backup_dir / "d.ini").write_text(fixable, encoding="utf-8")
    (tmp_path / "DISABLED_BACKUP_1727259311.x.ini").write_text(
        fixable, encoding="utf-8"
    )
    foreign_dir = tmp_path / "DISABLED_BACKUP_1727259313"
    foreign_dir.mkdir()
    (foreign_dir / "nested.ini").write_text(fixable, encoding="utf-8")

    plans = scan_folder(tmp_path, snapshot_data)
    assert [Path(plan.path) for plan in plans] == [stale]


def test_legacy_in_folder_backups_invisible(tmp_path, snapshot_data):
    fixable = "[TextureOverrideSampleTopBlend]\nhash = 8c0622d7\n"
    live = tmp_path / "legacy.ini"
    live.write_text(fixable, encoding="utf-8")
    legacy = tmp_path / "DISABLED_versionfix_1000-legacy.ini"
    legacy.write_text(fixable, encoding="utf-8")

    plans = scan_folder(tmp_path, snapshot_data)
    assert [Path(plan.path) for plan in plans] == [live]

    store = tmp_path / "store"
    assert collect_backup_chains(tmp_path, store) == {}
    assert collect_backup_chains_for([live], tmp_path, store) == {}


def test_scan_files_stale_file(tmp_path, snapshot_data):
    stale_file = tmp_path / "stale.ini"
    stale_file.write_text(
        "[TextureOverrideSampleTopBlend]\nhash = 8c0622d7\n", encoding="utf-8"
    )
    clean_file = tmp_path / "clean.ini"
    clean_file.write_text(
        "[TextureOverrideSomething]\nhash = deadbeef\n", encoding="utf-8"
    )

    plans = scan_files([stale_file, clean_file], snapshot_data)

    assert [Path(plan.path) for plan in plans] == [stale_file]
    assert len(plans[0].suggestions) == 1
    suggestion = plans[0].suggestions[0]
    assert (suggestion.kind, suggestion.old, suggestion.new) == (
        "hash",
        "8c0622d7",
        "018ea72c",
    )


def test_scan_files_preserves_input_order(tmp_path, snapshot_data):
    stale = "[TextureOverrideSampleTopBlend]\nhash = 8c0622d7\n"
    first = tmp_path / "m_stale.ini"
    first.write_text(stale, encoding="utf-8")
    second = tmp_path / "z_stale.ini"
    second.write_text(stale, encoding="utf-8")

    plans = scan_files([second, first], snapshot_data)

    assert [Path(plan.path) for plan in plans] == [second, first]


def test_scan_files_skips_undecodable(tmp_path, snapshot_data):
    bad_file = tmp_path / "broken.ini"
    bad_file.write_bytes(b"\xff\xfe\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    good_file = tmp_path / "stale.ini"
    good_file.write_text(
        "[TextureOverrideSampleTopBlend]\nhash = 8c0622d7\n", encoding="utf-8"
    )

    plans = scan_files([bad_file, good_file], snapshot_data)

    assert [Path(plan.path) for plan in plans] == [good_file]


def test_scan_files_explicitly_includes_disabled_named_file(tmp_path, snapshot_data):
    disabled_file = tmp_path / "DISABLED_part.ini"
    disabled_file.write_text(
        "[TextureOverrideSampleTopBlend]\nhash = 8c0622d7\n", encoding="utf-8"
    )

    plans = scan_files([disabled_file], snapshot_data)

    assert [Path(plan.path) for plan in plans] == [disabled_file]
    assert len(plans[0].suggestions) == 1


def test_collect_backup_chains_for_file(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    live = mods / "part.ini"
    live.write_text(S3, encoding="utf-8")
    b2000 = key_dir / "DISABLED_versionfix_2000-part.ini"
    b2000.parent.mkdir(parents=True)
    b2000.write_text(S2, encoding="utf-8")
    b1000 = key_dir / "DISABLED_versionfix_1000-part.ini"
    b1000.write_text(S1, encoding="utf-8")
    foreign = key_dir / "DISABLED_BACKUP_1727259311.9821017e.ini"
    foreign.write_text("foreign", encoding="utf-8")

    chains = collect_backup_chains_for([live], mods, store)

    assert list(chains) == [live]
    chain = chains[live]
    assert chain.live == live
    assert chain.backups == [(1000, b1000), (2000, b2000)]


def test_collect_backup_chains_for_nested_path(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    live = mods / "pack" / "part.ini"
    live.parent.mkdir(parents=True)
    live.write_text(S3, encoding="utf-8")
    b1000 = key_dir / "pack" / "DISABLED_versionfix_1000-part.ini"
    b1000.parent.mkdir(parents=True)
    b1000.write_text(S1, encoding="utf-8")
    (b1000.parent / "DISABLED_versionfix_1000-other.ini").write_text(
        "other", encoding="utf-8"
    )

    chains = collect_backup_chains_for([live], mods, store)

    assert list(chains) == [live]
    assert chains[live].backups == [(1000, b1000)]


def test_collect_backup_chains_for_no_backups(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    clean = mods / "clean.ini"
    clean.write_text("live", encoding="utf-8")
    key_dir.mkdir(parents=True)
    (key_dir / "DISABLED_versionfix_1000-other.ini").write_text(
        "other", encoding="utf-8"
    )

    assert collect_backup_chains_for([clean], mods, store) == {}


def test_collect_backup_chains_for_dedupes_paths(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    live = mods / "part.ini"
    live.write_text(S3, encoding="utf-8")
    b1000 = key_dir / "DISABLED_versionfix_1000-part.ini"
    b1000.parent.mkdir(parents=True)
    b1000.write_text(S1, encoding="utf-8")

    chains = collect_backup_chains_for([live, Path(live)], mods, store)

    assert list(chains) == [live]
    assert len(chains) == 1


def test_collect_backup_chains_for_outside_mods_dir_raises(tmp_path):
    mods, store, _key_dir = store_layout(tmp_path)
    outside = tmp_path / "elsewhere" / "part.ini"
    with pytest.raises(ValueError):
        collect_backup_chains_for([outside], mods, store)


def test_scan_files_empty_paths(snapshot_data):
    assert scan_files([], snapshot_data) == []


def test_folder_key_deterministic_and_distinct(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    key = folder_key(mods)
    assert folder_key(mods) == key
    other = tmp_path / "My Mods!"
    other.mkdir()
    assert folder_key(other) != key
    same_name = tmp_path / "nested" / "mods"
    same_name.mkdir(parents=True)
    assert folder_key(same_name) != key


def test_folder_key_sanitizes_name():
    key = folder_key(Path("irrelevant") / "My Mods!")
    assert re.fullmatch(r"My_Mods_-[0-9a-f]{8}", key)
    assert re.fullmatch(r"[A-Za-z0-9_-]+-[0-9a-f]{8}", folder_key(Path(".")))
    assert re.fullmatch(r"mods-[0-9a-f]{8}", folder_key(Path("")))


def test_default_backups_dir():
    store_dir = default_backups_dir()
    assert store_dir.name == "backups"
    assert store_dir.parent == repo.default_cache_dir().parent.parent


def test_folder_key_relative_inside_app_root(tmp_path, monkeypatch):
    """Mods folders inside the app root key on the relative path."""
    root = tmp_path / "app"
    (root / "mods").mkdir(parents=True)
    monkeypatch.setattr("tools.backups.project_root", lambda: root)
    original = folder_key(root / "mods")
    relocated = tmp_path / "moved"
    (relocated / "mods").mkdir(parents=True)
    monkeypatch.setattr("tools.backups.project_root", lambda: relocated)
    assert folder_key(relocated / "mods") == original


def test_folder_key_absolute_outside_app_root(tmp_path, monkeypatch):
    """Mods folders outside the app root keep the absolute-path key."""
    mods = tmp_path / "mods"
    mods.mkdir()
    monkeypatch.setattr("tools.backups.project_root", lambda: tmp_path / "app")
    expected = f"mods-{sha1(str(mods.resolve()).encode('utf-8')).hexdigest()[:8]}"
    assert folder_key(mods) == expected


def test_migrate_store_key_adopts_legacy(tmp_path, monkeypatch):
    """A legacy absolute-key store is renamed under the relative key."""
    root = tmp_path / "app"
    mods = root / "mods"
    mods.mkdir(parents=True)
    monkeypatch.setattr("tools.backups.project_root", lambda: root)
    store = tmp_path / "backups"
    legacy = store / f"mods-{sha1(str(mods.resolve()).encode('utf-8')).hexdigest()[:8]}"
    legacy.mkdir(parents=True)
    (legacy / "keep.bak").write_text("x", encoding="utf-8")
    result = store_root(store, mods)
    assert result == store / folder_key(mods)
    assert result != legacy
    assert (result / "keep.bak").read_text(encoding="utf-8") == "x"
    assert not legacy.exists()


def test_migrate_store_key_no_ops(tmp_path, monkeypatch):
    """Migration keeps the current store and leaves the legacy store alone."""
    root = tmp_path / "app"
    mods = root / "mods"
    mods.mkdir(parents=True)
    monkeypatch.setattr("tools.backups.project_root", lambda: root)
    store = tmp_path / "backups"
    current = store / folder_key(mods)
    current.mkdir(parents=True)
    (current / "fresh.bak").write_text("y", encoding="utf-8")
    legacy = store / f"mods-{sha1(str(mods.resolve()).encode('utf-8')).hexdigest()[:8]}"
    legacy.mkdir(parents=True)
    (legacy / "old.bak").write_text("x", encoding="utf-8")
    assert store_root(store, mods) == current
    assert (current / "fresh.bak").read_text(encoding="utf-8") == "y"
    assert (legacy / "old.bak").read_text(encoding="utf-8") == "x"


def test_backup_path_for_layout(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "pack" / "SampleMod.ini"
    dst = backup_path_for(store, mods, live, 1234)
    local = datetime.fromtimestamp(1234 / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    assert dst == store_root(store, mods) / "pack" / f"SampleMod.ini -- {local}.bak"


def test_backup_path_for_outside_mods_dir_raises(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    outside = tmp_path / "elsewhere" / "a.ini"
    with pytest.raises(ValueError):
        backup_path_for(tmp_path / "store", mods, outside, 1000)


def test_backup_path_for_new_scheme(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    live = mods / "CharaA.ini"
    stamp = 1788950506000
    dst = backup_path_for(store, mods, live, stamp)
    local = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    assert dst == store_root(store, mods) / f"CharaA.ini -- {local}.bak"
    assert parse_store_backup_name(dst.name) == (stamp, "CharaA.ini")


def test_backup_path_for_collision_suffix(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    live = mods / "CharaA.ini"
    stamp = 1788950506000
    first = backup_path_for(store, mods, live, stamp)
    first.parent.mkdir(parents=True)
    first.write_text("taken", encoding="utf-8")

    second = backup_path_for(store, mods, live, stamp)

    local = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    assert second.name == f"CharaA.ini -- {local} (2).bak"
    assert second != first


def test_collect_backup_chains_parses_both_schemes(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    key_dir.mkdir(parents=True)
    live = mods / "part.ini"
    live.write_text(S3, encoding="utf-8")
    legacy = key_dir / "DISABLED_versionfix_123-part.ini"
    legacy.write_text(S1, encoding="utf-8")
    stamp = 1788950506000
    local = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    modern = key_dir / f"part.ini -- {local}.bak"
    modern.write_text(S2, encoding="utf-8")

    chains = collect_backup_chains(mods, store)

    assert list(chains) == [live]
    chain = chains[live]
    assert chain.live == live
    assert chain.backups == [(123, legacy), (stamp, modern)]

    assert collect_backup_chains_for([live], mods, store)[live].backups == chain.backups


def test_collect_backup_chains_same_second_dup_order(tmp_path):
    mods, store, key_dir = store_layout(tmp_path)
    live = mods / "a.ini"
    live.write_text(S3, encoding="utf-8")
    plain = key_dir / "a.ini -- 2026-09-09 17.34.49.bak"
    plain.parent.mkdir(parents=True)
    plain.write_text(S1, encoding="utf-8")
    dup2 = key_dir / "a.ini -- 2026-09-09 17.34.49 (2).bak"
    dup2.write_text(S2, encoding="utf-8")
    parsed_plain = parse_store_backup_name(plain.name)
    assert parsed_plain is not None
    stamp = parsed_plain[0]
    parsed_dup = parse_store_backup_name(dup2.name)
    assert parsed_dup is not None
    assert stamp == parsed_dup[0]
    assert str(dup2) < str(plain)

    expected = [(stamp, plain), (stamp, dup2)]
    assert collect_backup_chains(mods, store)[live].backups == expected
    assert collect_backup_chains_for([live], mods, store)[live].backups == expected


def test_canonical_dirs_strips_disabled_prefix():
    assert _canonical_dirs(("DISABLED_modA", "sub", "DISABLED_deep")) == (
        "modA",
        "sub",
        "deep",
    )
    assert _canonical_dirs(()) == ()
    assert _canonical_dirs(("pack", "nested")) == ("pack", "nested")


def test_backup_path_for_strips_disabled_dirs(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "DISABLED_modA" / "CharaA.ini"
    stamp = 1788950506000
    dst = backup_path_for(store, mods, live, stamp)
    local = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    assert dst == store_root(store, mods) / "modA" / f"CharaA.ini -- {local}.bak"
    assert parse_store_backup_name(dst.name) == (stamp, "CharaA.ini")


def test_backup_path_for_both_states_same_store_path(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    stamp = 1788950506000
    disabled = backup_path_for(store, mods, mods / "DISABLED_modA" / "CharaA.ini", stamp)
    enabled = backup_path_for(store, mods, mods / "modA" / "CharaA.ini", stamp)
    assert disabled == enabled


def test_store_folder_for_mod_canonicalizes_disabled_dirs(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    folder = store_folder_for_mod(store, mods, mods / "DISABLED_modA")
    assert folder == store_root(store, mods) / "modA"
    nested = store_folder_for_mod(store, mods, mods / "pack" / "DISABLED_modA")
    assert nested == store_root(store, mods) / "pack" / "modA"


def test_store_folder_for_mod_outside_mods_dir_raises(tmp_path):
    with pytest.raises(ValueError):
        store_folder_for_mod(
            tmp_path / "store", tmp_path / "mods", tmp_path / "elsewhere"
        )


def test_retarget_store_folder_moves_mirror_folder(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    backup = (
        store_folder_for_mod(store, mods, mods / "Foo")
        / "a.ini -- 2026-01-01 00.00.00.bak"
    )
    backup.parent.mkdir(parents=True)
    backup.write_text(S1, encoding="utf-8")

    assert retarget_store_folder(store, mods, Path("Foo"), Path("Bar")) is True

    moved = store_folder_for_mod(store, mods, mods / "Bar") / backup.name
    assert moved.read_text(encoding="utf-8") == S1
    assert not store_folder_for_mod(store, mods, mods / "Foo").exists()


def test_retarget_store_folder_canonicalizes_disabled_rels(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    mirror = store_folder_for_mod(store, mods, mods / "Foo")
    mirror.mkdir(parents=True)
    (mirror / "a.ini -- 2026-01-01 00.00.00.bak").write_text(S1, encoding="utf-8")

    assert retarget_store_folder(
        store, mods, Path("DISABLED_Foo"), Path("DISABLED_Bar")
    ) is True

    moved = store_folder_for_mod(store, mods, mods / "Bar")
    assert (moved / "a.ini -- 2026-01-01 00.00.00.bak").read_text(
        encoding="utf-8"
    ) == S1
    assert not mirror.exists()


def test_retarget_store_folder_moves_whole_category_subtree(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    nested = store_folder_for_mod(store, mods, mods / "Cat") / "Sub"
    nested.mkdir(parents=True)
    (nested / "x.bak").write_text(S1, encoding="utf-8")

    assert retarget_store_folder(store, mods, Path("Cat"), Path("Pets")) is True

    moved = store_folder_for_mod(store, mods, mods / "Pets") / "Sub" / "x.bak"
    assert moved.read_text(encoding="utf-8") == S1
    assert not store_folder_for_mod(store, mods, mods / "Cat").exists()


def test_retarget_store_folder_missing_source_or_taken_target(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"

    assert retarget_store_folder(store, mods, Path("Foo"), Path("Bar")) is False

    source = store_folder_for_mod(store, mods, mods / "Foo")
    source.mkdir(parents=True)
    (source / "keep.bak").write_text(S1, encoding="utf-8")
    target = store_folder_for_mod(store, mods, mods / "Bar")
    target.mkdir(parents=True)
    (target / "other.bak").write_text(S2, encoding="utf-8")

    assert retarget_store_folder(store, mods, Path("Foo"), Path("Bar")) is False

    assert (source / "keep.bak").read_text(encoding="utf-8") == S1
    assert (target / "other.bak").read_text(encoding="utf-8") == S2


def test_delete_store_folder_removes_mirror_with_baks(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    mirror = store_folder_for_mod(store, mods, mods / "Foo")
    mirror.mkdir(parents=True)
    (mirror / "a.ini -- 2026-01-01 00.00.00.bak").write_text(S1, encoding="utf-8")

    assert delete_store_folder(store, mods, Path("Foo")) is True

    assert not store_folder_for_mod(store, mods, mods / "Foo").exists()


def test_delete_store_folder_canonicalizes_disabled_rel(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    mirror = store_folder_for_mod(store, mods, mods / "Foo")
    mirror.mkdir(parents=True)
    (mirror / "a.ini -- 2026-01-01 00.00.00.bak").write_text(S1, encoding="utf-8")

    assert delete_store_folder(store, mods, Path("DISABLED_Foo")) is True

    assert not mirror.exists()


def test_delete_store_folder_removes_whole_category_subtree(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    nested = store_folder_for_mod(store, mods, mods / "Cat") / "Sub"
    nested.mkdir(parents=True)
    (nested / "x.bak").write_text(S1, encoding="utf-8")

    assert delete_store_folder(store, mods, Path("Cat")) is True

    assert not store_folder_for_mod(store, mods, mods / "Cat").exists()


def test_delete_store_folder_absent_mirror_writes_nothing(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"

    assert delete_store_folder(store, mods, Path("Ghost")) is False

    assert not store.exists()


def test_prune_empty_store_folders_removes_empty_mirrors(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    root = store_root(store, mods)
    for name in ("CharaA", "CharaB"):
        (root / "Cat" / "ModA" / "Gen" / name).mkdir(parents=True)
    backup = root / "Cat" / "ModB" / "char.ini -- 2026-01-01 00.00.00.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text(S1, encoding="utf-8")

    assert prune_empty_store_folders(store, mods) == 4

    assert not (root / "Cat" / "ModA").exists()
    assert not (root / "Cat" / "ModA" / "Gen" / "CharaA").exists()
    assert not (root / "Cat" / "ModA" / "Gen" / "CharaB").exists()
    assert (root / "Cat").is_dir()
    assert (root / "Cat" / "ModB").is_dir()
    assert backup.read_text(encoding="utf-8") == S1
    assert root.is_dir()


def test_prune_empty_store_folders_without_store_is_zero(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"

    assert prune_empty_store_folders(store, mods) == 0

    assert not store.exists()


def test_prune_empty_store_folders_keeps_store_root(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    root = store_root(store, mods)
    (root / "Cat" / "ModA" / "Gen").mkdir(parents=True)

    assert prune_empty_store_folders(store, mods) == 3

    assert not (root / "Cat").exists()
    assert root.is_dir()


def test_store_folder_to_open_prefers_nearest_existing(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    target = store_root(tmp_path / "s1", mods) / "modA"
    target.mkdir(parents=True)
    assert (
        store_folder_to_open(tmp_path / "s1", mods, mods / "DISABLED_modA") == target
    )
    root = store_root(tmp_path / "s2", mods)
    root.mkdir(parents=True)
    assert store_folder_to_open(tmp_path / "s2", mods, mods / "DISABLED_modA") == root
    store = tmp_path / "s3"
    store.mkdir()
    assert store_folder_to_open(store, mods, mods / "DISABLED_modA") == store
    assert store_folder_to_open(tmp_path / "s4", mods, mods / "DISABLED_modA") == mods


def test_resolve_live_path_canonical_exists_wins(tmp_path):
    mods = tmp_path / "mods"
    canonical = mods / "modA" / "x.ini"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(S3, encoding="utf-8")
    disabled = mods / "DISABLED_modA" / "x.ini"
    disabled.parent.mkdir()
    disabled.write_text(S2, encoding="utf-8")

    assert _resolve_live_path(mods, ("modA",), "x.ini") == canonical


def test_resolve_live_path_disabled_variant_found(tmp_path):
    mods = tmp_path / "mods"
    disabled = mods / "DISABLED_modA" / "x.ini"
    disabled.parent.mkdir(parents=True)
    disabled.write_text(S2, encoding="utf-8")

    assert _resolve_live_path(mods, ("modA",), "x.ini") == disabled


def test_resolve_live_path_missing_falls_back_to_canonical(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    resolved = _resolve_live_path(mods, ("modA",), "x.ini")
    assert resolved == mods / "modA" / "x.ini"
    assert not resolved.exists()


def test_collect_backup_chains_subtree_matches_toggled_name(tmp_path):
    mods, store, _key_dir = store_layout(tmp_path)
    stamp = 1788950506000
    backup = backup_path_for(store, mods, mods / "DISABLED_modA" / "CharaA.ini", stamp)
    backup.parent.mkdir(parents=True)
    backup.write_text(S1, encoding="utf-8")

    disabled_live = mods / "DISABLED_modA" / "CharaA.ini"
    disabled_live.parent.mkdir(parents=True)
    disabled_live.write_text(S2, encoding="utf-8")
    toggled = collect_backup_chains(mods, store, subtree=mods / "DISABLED_modA")
    assert list(toggled) == [disabled_live]
    assert toggled[disabled_live].live == disabled_live
    assert toggled[disabled_live].backups == [(stamp, backup)]

    disabled_live.unlink()
    disabled_live.parent.rmdir()
    enabled_live = mods / "modA" / "CharaA.ini"
    enabled_live.parent.mkdir(parents=True)
    enabled_live.write_text(S3, encoding="utf-8")
    enabled = collect_backup_chains(mods, store, subtree=mods / "modA")
    assert list(enabled) == [enabled_live]
    assert enabled[enabled_live].backups == [(stamp, backup)]


def test_collect_backup_chains_for_matches_across_toggle(tmp_path):
    mods, store, _key_dir = store_layout(tmp_path)
    stamp = 1788950506000
    backup = backup_path_for(store, mods, mods / "DISABLED_modA" / "CharaA.ini", stamp)
    backup.parent.mkdir(parents=True)
    backup.write_text(S1, encoding="utf-8")
    disabled_live = mods / "DISABLED_modA" / "CharaA.ini"
    disabled_live.parent.mkdir(parents=True)
    disabled_live.write_text(S2, encoding="utf-8")

    chains = collect_backup_chains_for([disabled_live], mods, store)

    assert list(chains) == [disabled_live]
    chain = chains[disabled_live]
    assert chain.live == disabled_live
    assert chain.backups == [(stamp, backup)]

    disabled_live.unlink()
    disabled_live.parent.rmdir()
    enabled_live = mods / "modA" / "CharaA.ini"
    enabled_live.parent.mkdir(parents=True)
    enabled_live.write_text(S3, encoding="utf-8")
    enabled = collect_backup_chains_for([enabled_live], mods, store)
    assert list(enabled) == [enabled_live]
    assert enabled[enabled_live].backups == [(stamp, backup)]


def test_two_state_chain_merges_and_orders(tmp_path, monkeypatch):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "modA" / "CharaA.ini"
    stale = "[TextureOverrideCharaA]\nhash = 8c0622d7\n"
    live.parent.mkdir(parents=True)
    live.write_text(stale, encoding="utf-8")
    original = live.read_bytes()
    stamp_a = 1788950506000
    stamp_b = 1788950606000
    stamps = iter([stamp_a, stamp_b])
    monkeypatch.setattr(fixer.time, "time", lambda: next(stamps) / 1000)
    data = build_data(
        [
            entry("8c0622d7", "018ea72c", ["CharaA"]),
            entry("f6474154", "08ddaed3", ["CharaA"]),
        ]
    )
    key_dir = store_root(store, mods)
    local_a = datetime.fromtimestamp(stamp_a / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    local_b = datetime.fromtimestamp(stamp_b / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H.%M.%S")
    first_backup = key_dir / "modA" / f"CharaA.ini -- {local_a}.bak"
    second_backup = key_dir / "modA" / f"CharaA.ini -- {local_b}.bak"

    (plan,) = scan_folder(mods, data)
    assert apply_plan(plan, data, store_dir=store, mods_dir=mods, log=quiet) is True
    assert first_backup.read_bytes() == original

    shutil.move(str(mods / "modA"), str(mods / "DISABLED_modA"))
    disabled_live = mods / "DISABLED_modA" / "CharaA.ini"
    disabled_live.write_text(
        "[TextureOverrideCharaA]\nhash = f6474154\n", encoding="utf-8"
    )
    pre_second_fix = disabled_live.read_bytes()

    (plan,) = scan_folder(mods, data)
    assert apply_plan(plan, data, store_dir=store, mods_dir=mods, log=quiet) is True

    assert first_backup.exists() and second_backup.exists()
    assert len(list(key_dir.rglob("*.bak"))) == 2
    assert not (key_dir / "DISABLED_modA").exists()

    chains = collect_backup_chains(mods, store, subtree=mods / "DISABLED_modA")

    assert list(chains) == [disabled_live]
    chain = chains[disabled_live]
    assert chain.backups == [(stamp_a, first_backup), (stamp_b, second_backup)]

    assert revert_backups([(disabled_live, chain.backups[-1][1])], log=quiet) == 1

    assert disabled_live.read_bytes() == pre_second_fix
    assert not second_backup.exists()
    assert first_backup.read_bytes() == original


def test_canonical_dirs_empty_dirs_key_at_store_root(tmp_path):
    mods = tmp_path / "mods"
    live = mods / "x.ini"
    live.parent.mkdir(parents=True)
    live.write_text(S3, encoding="utf-8")

    assert _canonical_dirs(()) == ()
    assert _resolve_live_path(mods, (), "x.ini") == live


def test_move_file_same_volume(tmp_path):
    src = tmp_path / "src.ini"
    src.write_text(S2, encoding="utf-8")
    dst = tmp_path / "dst.ini"
    move_file(src, dst)
    assert dst.read_text(encoding="utf-8") == S2
    assert not src.exists()


def test_move_file_fallback_cross_volume(tmp_path, monkeypatch):
    src = tmp_path / "src.ini"
    src.write_text(S2, encoding="utf-8")
    dst = tmp_path / "missing" / "nested" / "dst.ini"

    def cross_volume_error(*_args, **_kwargs):
        raise OSError("cross-volume move simulated")

    monkeypatch.setattr(backups.os, "replace", cross_volume_error)
    move_file(src, dst)

    assert dst.read_text(encoding="utf-8") == S2
    assert not src.exists()


def test_ambiguity_guard(tmp_path):
    (tmp_path / "mod.ini").write_text(
        "[TextureOverrideSomething]\nhash = aaaaaa11\n", encoding="utf-8"
    )
    conflicting = FixerData(
        chains={
            "aaaaaa11": [
                entry("aaaaaa11", "bbbbbb22", ["xxxfoo"]),
                entry("aaaaaa11", "cccccc33", ["yyybar"]),
            ]
        },
        ib_index_changes={},
        db=CharacterDB(),
    )
    assert scan_folder(tmp_path, conflicting) == []

    resolved = FixerData(
        chains={
            "aaaaaa11": [
                entry("aaaaaa11", "bbbbbb22", ["xxxfoo"]),
                entry("aaaaaa11", "bbbbbb22", ["yyybar"]),
            ]
        },
        ib_index_changes={},
        db=CharacterDB(),
    )
    plans = scan_folder(tmp_path, resolved)
    assert len(plans) == 1
    assert [
        (s.kind, s.line_no, s.old, s.new) for s in plans[0].suggestions
    ] == [("hash", 2, "aaaaaa11", "bbbbbb22")]


def test_hint_disambiguation(tmp_path):
    (tmp_path / "mod.ini").write_text(
        "[TextureOverrideSamplefooTopA]\n"
        "hash = aaaaaa11\n"
        "[TextureOverrideBellebarBodyA]\n"
        "hash = aaaaaa11\n"
        "[TextureOverrideUnrelatedPlace]\n"
        "hash = aaaaaa11\n",
        encoding="utf-8",
    )
    data = FixerData(
        chains={
            "aaaaaa11": [
                entry("aaaaaa11", "bbbbbb22", ["samplefoo"]),
                entry("aaaaaa11", "cccccc33", ["bellebar"]),
            ]
        },
        ib_index_changes={},
        db=CharacterDB(),
    )
    assert latin_suffix("samplefoo") == "samplefoo"
    assert hint_matches(["samplefoo"], "samplefootopa")
    assert hint_matches(["bellebar"], "bellebarbodya")
    assert not hint_matches(["samplefoo"], "unrelatedplace")

    plans = scan_folder(tmp_path, data)
    assert len(plans) == 1
    got = [
        (s.section, s.kind, s.line_no, s.old, s.new) for s in plans[0].suggestions
    ]
    assert got == [
        ("TextureOverrideSamplefooTopA", "hash", 2, "aaaaaa11", "bbbbbb22"),
        ("TextureOverrideBellebarBodyA", "hash", 4, "aaaaaa11", "cccccc33"),
    ]


def round_trip_data():
    """FixerData for a round-trip chain: dd86f5ae -> 19ad87f6 (2.7 -> 2.8),
    then back 19ad87f6 -> dd86f5ae (2.8 -> 3.0) — the hash is reused by the
    later version, so dd86f5ae content is already in its latest state."""
    return build_data(
        [
            entry("dd86f5ae", "19ad87f6", ["CharaA"], version_index=1),
            entry("19ad87f6", "dd86f5ae", ["CharaA"], version_index=2),
        ]
    )


def test_resolve_hash_chain_round_trip_returns_no_steps():
    data = round_trip_data()
    assert resolve_hash_chain("dd86f5ae", "", data) == []
    steps = resolve_hash_chain("19ad87f6", "", data)
    assert steps is not None
    assert [step.to_hash for step in steps] == ["dd86f5ae"]
    assert resolve_hash_chain("deadbeef", "", data) == []


def test_hash_is_outdated_round_trip_false():
    data = round_trip_data()
    assert not hash_is_outdated("dd86f5ae", data)
    assert hash_is_outdated("19ad87f6", data)
    assert not hash_is_outdated("deadbeef", data)
    ambiguous = build_data(
        [
            entry("aaaaaa11", "bbbbbb22", ["xxxfoo"], version_index=1),
            entry("aaaaaa11", "cccccc33", ["yyybar"], version_index=1),
        ]
    )
    assert resolve_hash_chain("aaaaaa11", "", ambiguous) is None
    assert hash_is_outdated("aaaaaa11", ambiguous)


def test_scan_text_round_trip_no_suggestion():
    data = round_trip_data()
    text = "[TextureOverrideCharaA]\nhash = dd86f5ae\n"
    assert fixer._scan_text(text, data) == []
    text = "[TextureOverrideCharaA]\nhash = 19ad87f6\n"
    assert [
        (s.kind, s.line_no, s.old, s.new) for s in fixer._scan_text(text, data)
    ] == [("hash", 2, "19ad87f6", "dd86f5ae")]


def test_apply_plan_round_trip_writes_nothing_no_backup(tmp_path):
    data = round_trip_data()
    live = tmp_path / "CharaA.ini"
    content = "[TextureOverrideCharaA]\nhash = dd86f5ae\n"
    live.write_text(content, encoding="utf-8")
    store = tmp_path / "store"
    logs: list[str] = []

    assert (
        apply_plan(
            FilePlan(path=str(live)),
            data,
            store_dir=store,
            mods_dir=tmp_path,
            log=logs.append,
        )
        is False
    )

    assert live.read_text(encoding="utf-8") == content
    assert not store.exists()
    assert any("no changes needed" in message for message in logs)


def test_apply_plan_drops_noop_suggestions(tmp_path, monkeypatch):
    live = tmp_path / "CharaA.ini"
    content = "[TextureOverrideCharaA]\nhash = dd86f5ae\n"
    live.write_text(content, encoding="utf-8")
    store = tmp_path / "store"
    noop = FixSuggestion(
        file=str(live),
        section="TextureOverrideCharaA",
        line_no=2,
        kind="hash",
        old="dd86f5ae",
        new="DD86F5AE",
        reason="hand-built stale plan: self-replacement round-trip",
    )

    def fake_details(_path, _data, _structure=None):
        return content, "utf-8", [noop]

    monkeypatch.setattr(fixer, "_scan_file_details", fake_details)
    logs: list[str] = []

    assert (
        apply_plan(
            FilePlan(path=str(live), suggestions=[noop]),
            round_trip_data(),
            store_dir=store,
            mods_dir=tmp_path,
            log=logs.append,
        )
        is False
    )

    assert live.read_text(encoding="utf-8") == content
    assert not store.exists()
    assert any("no changes needed" in message for message in logs)


def legacy_mixed_data():
    """FixerData mixing harvested legacy edges with a real-changelog edge.

    aaaaaa11 carries a legacy edge (belletemple-attributed) and a real
    changelog edge (samplefoo-attributed); dddddd44 is a legacy-only bucket.
    """
    return build_data(
        [
            entry("aaaaaa11", "bbbbbb22", ["belletemple"], version_index=1, role="legacy"),
            entry("aaaaaa11", "cccccc33", ["samplefoo"], version_index=2, role="ib"),
            entry("dddddd44", "eeeeee55", ["belletemple"], version_index=1, role="legacy"),
        ]
    )


def test_legacy_edge_fires_on_matching_hint():
    data = legacy_mixed_data()
    steps = resolve_hash_chain("aaaaaa11", "belletemplebody", data)
    assert steps is not None
    assert [(s.to_hash, s.role) for s in steps] == [("bbbbbb22", "legacy")]
    only = resolve_hash_chain("dddddd44", "belletemplehair", data)
    assert only is not None
    assert [(s.to_hash, s.role) for s in only] == [("eeeeee55", "legacy")]


def test_legacy_edge_never_fires_via_fallback():
    data = legacy_mixed_data()
    assert resolve_hash_chain("dddddd44", "", data) == []
    assert resolve_hash_chain("dddddd44", "unrelatedname", data) == []
    assert not hash_is_outdated("dddddd44", data)
    steps = resolve_hash_chain("aaaaaa11", "", data)
    assert steps is not None
    assert [(s.to_hash, s.role) for s in steps] == [("cccccc33", "ib")]


def test_real_edge_still_fires_via_fallback():
    data = build_data([entry("aaaaaa11", "cccccc33", ["samplefoo"])])
    steps = resolve_hash_chain("aaaaaa11", "", data)
    assert steps is not None
    assert [(s.to_hash, s.role) for s in steps] == [("cccccc33", "ib")]
    assert hash_is_outdated("aaaaaa11", data)


def hint_gated_legacy_data():
    """FixerData with one hint-gated legacy edge plus an unrelated current hash.

    aaaa0000 -> bbbb0000 is a legacy-only bucket (role "legacy") firing only
    through a matching hint; dddd0000 lives only in the character-DB reverse index.
    """
    legacy = ChangeEntry(
        from_hash="aaaa0000",
        to_hash="bbbb0000",
        characters=["charab"],
        role="legacy",
        version_label="1.0 -> 1.2",
        version_index=1,
    )
    db = CharacterDB()
    db.reverse = {"dddd0000": []}
    return FixerData(
        chains=changelog.build_chain_index([legacy]),
        ib_index_changes={},
        db=db,
        entries=[legacy],
    )


def test_hash_is_outdated_respects_hint():
    data = hint_gated_legacy_data()
    assert hash_is_outdated("aaaa0000", data) is False
    assert hash_is_outdated("aaaa0000", data, "charabbodyadiffuse") is True
    assert hash_is_outdated("aaaa0000", data, "otherchar") is False
    assert hash_is_outdated("deadbeef", data) is False
    assert hash_is_outdated("deadbeef", data, "charabbodyadiffuse") is False
    assert hash_is_outdated("dddd0000", data) is False
    assert hash_is_outdated("dddd0000", data, "charabbodyadiffuse") is False


def test_walk_index_two_steps(tmp_path):
    data = FixerData(
        chains={
            "11111111": [entry("11111111", "22222222", ["xxxfoo"], version_index=1)],
            "22222222": [entry("22222222", "33333333", ["xxxfoo"], version_index=2)],
        },
        ib_index_changes={
            "11111111": [
                index_entry("11111111", "22222222", 1, [0, 100], [0, 200], ["xxxfoo"])
            ],
            "22222222": [
                index_entry("22222222", "33333333", 2, [0, 200], [0, 350], ["xxxfoo"])
            ],
        },
        db=CharacterDB(),
    )
    value, steps = walk_index_value(100, "11111111", data)
    assert value == 350
    assert [step.to_hash for step in steps] == ["22222222", "33333333"]
    assert walk_index_value(999, "11111111", data) == (999, [])

    (tmp_path / "mod.ini").write_text(
        "[TextureOverrideSomething]\n"
        "hash = 11111111\n"
        "match_first_index = 100\n"
        "match_index_count = 100\n",
        encoding="utf-8",
    )
    plans = scan_folder(tmp_path, data)
    assert len(plans) == 1
    assert [
        (s.kind, s.line_no, s.old, s.new) for s in plans[0].suggestions
    ] == [
        ("hash", 2, "11111111", "33333333"),
        ("match_first_index", 3, "100", "350"),
        ("match_index_count", 4, "100", "350"),
    ]


def test_collect_texture_override_hints(tmp_path):
    path = tmp_path / "mod.ini"
    path.write_text(
        "hash = bbbb1111\n"
        "[TextureOverrideA]\nhash = aaaa0000\n"
        "[TextureOverrideB]\nhash = aaaa0000\n"
        "[ShaderOverrideX]\nhash = aaaa0000\n",
        encoding="utf-8",
    )

    assert collect_texture_override_hints(path) == {"aaaa0000": {"a", "b"}}
    assert collect_texture_override_hashes(path) == ["aaaa0000"]


@pytest.mark.skipif(
    not repo.changelog_path(repo.default_cache_dir()).exists(),
    reason="live ZZZ-Model-Hash data not cloned",
)
def test_live_data_smoke():
    repo_dir = repo.default_cache_dir()
    data = load_fixer_data(repo_dir)
    assert data.chains
    assert data.db.characters
    entries = changelog.parse_changelog_file(repo.changelog_path(repo_dir))
    assert len(entries) >= 200


def make_variant_data(reverse: dict[str, list[HashRef]]):
    """Minimal FixerData whose knowledge is exactly the given reverse index."""
    db = CharacterDB()
    db.reverse = dict(reverse)
    return FixerData(chains={}, ib_index_changes={}, db=db)


def test_known_hashes_unions_sources():
    chain_entry = entry("dddd0000", "EEEE0000", ["xxxfoo"])
    data = FixerData(
        chains={"dddd0000": [chain_entry]},
        ib_index_changes={},
        db=make_variant_data({"aaaa0000": []}).db,
        entries=[chain_entry],
    )
    assert fixer.known_hashes(data) == {"aaaa0000", "dddd0000", "eeee0000"}


def test_detect_variant_exclusive_matches():
    datasets = {
        "2048p": make_variant_data({"204800aa": [], "cccc0000": []}),
        "1024p": make_variant_data({"102400bb": [], "cccc0000": []}),
    }
    assert fixer.detect_variant(["204800aa", "cccc0000"], datasets) == "2048p"
    assert fixer.detect_variant(["102400bb", "cccc0000"], datasets) == "1024p"


def test_detect_variant_shared_and_unknown_contribute_nothing():
    datasets = {
        "2048p": make_variant_data({"204800aa": [], "cccc0000": []}),
        "1024p": make_variant_data({"102400bb": [], "cccc0000": []}),
    }
    assert fixer.detect_variant(["cccc0000", "deadbeef"], datasets) is None
    assert fixer.detect_variant([], datasets) is None


def test_detect_variant_case_insensitive():
    datasets = {
        "2048p": make_variant_data({"204800aa": [], "cccc0000": []}),
        "1024p": make_variant_data({"102400bb": [], "cccc0000": []}),
    }
    assert fixer.detect_variant(["204800AA", "CCCC0000"], datasets) == "2048p"
    assert fixer.detect_variant(["102400BB"], datasets) == "1024p"


def test_detect_variant_tie_breaks_on_total_known():
    datasets = {
        "A": make_variant_data({"aaaa0001": [], "bbbb0000": []}),
        "B": make_variant_data({"aaaa0002": [], "bbbb0000": [], "cccc0000": []}),
        "C": make_variant_data({"cccc0000": []}),
    }
    hashes = ["aaaa0001", "aaaa0002", "bbbb0000", "cccc0000"]
    assert fixer.detect_variant(hashes, datasets) == "B"


def static_paths(monkeypatch, tmp_path, legacy=None, pcdata=None):
    """Patch tools.fixer's two static dataset lookups onto temp paths.

    legacy/pcdata: None keeps the temp path nonexistent (the dataset is
    absent); a payload dict is written to the temp path as JSON.
    """
    import json

    legacy_path = tmp_path / "legacy_chains.json"
    pcdata_path = tmp_path / "PlayerCharacterData.json"
    if legacy is not None:
        legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    if pcdata is not None:
        pcdata_path.write_text(
            json.dumps(pcdata, ensure_ascii=False), encoding="utf-8-sig"
        )
    monkeypatch.setattr("tools.fixer.legacy_chains_path", lambda: legacy_path)
    monkeypatch.setattr(
        "tools.fixer.player_character_data_path", lambda: pcdata_path
    )
    return legacy_path, pcdata_path


def test_pcdata_gap_fill_only_extends_unknown_chains(tmp_path, monkeypatch):
    """A pcdata row whose from-hash is unknown to changelog+legacy fills the
    chain gap ("importer #N") and stays strictly hint-gated like legacy."""
    changelog_text = (
        "版本 3.1 -> 3.11\n"
        "【角色丙CharaC】\n"
        "IB: aaaa0003 -> aaaa0004（身体）\n"
    )
    repo_dir = make_repo(tmp_path, changelog_text, subdir="repo")
    static_paths(
        monkeypatch,
        tmp_path,
        pcdata=[{"From": "aaaa0001", "To": "aaaa0002", "Comment": "1 CharaC BodyA texcoord"}],
    )

    data = load_fixer_data(repo_dir, include_pcdata=True)

    assert [e.role for e in data.entries] == ["pcdata", "ib"]
    assert [e.version_label for e in data.entries] == ["importer #1", "3.1 -> 3.11"]
    steps = resolve_hash_chain("aaaa0001", "charactopblend", data)
    assert steps is not None
    assert [(s.to_hash, s.version_label) for s in steps] == [
        ("aaaa0002", "importer #1")
    ]
    assert resolve_hash_chain("aaaa0001", "", data) == []
    assert hash_is_outdated("aaaa0001", data, "charactopblend") is True
    assert hash_is_outdated("aaaa0001", data) is False


def test_pcdata_does_not_touch_known_chains(tmp_path, monkeypatch):
    """A pcdata row whose from-hash already has a chain bucket is dropped
    entirely: the changelog edge keeps resolving and no pcdata row lands in
    the entries list."""
    changelog_text = (
        "版本 3.1 -> 3.11\n"
        "【角色丙CharaC/角色丁CharaD】\n"
        "texcoord_vb: bbbb0001 -> bbbb0002\n"
    )
    repo_dir = make_repo(tmp_path, changelog_text, subdir="repo")
    static_paths(
        monkeypatch,
        tmp_path,
        pcdata=[{"From": "bbbb0001", "To": "bbbb0003", "Comment": "1 CharaC BodyA texcoord"}],
    )

    data = load_fixer_data(repo_dir, include_pcdata=True)

    assert [e.role for e in data.entries] == ["texcoord_vb"]
    steps = resolve_hash_chain("bbbb0001", "charactopblend", data)
    assert steps is not None
    assert [(s.to_hash, s.role) for s in steps] == [("bbbb0002", "texcoord_vb")]


def test_pcdata_ping_pong_intermediate_rescues_to_legacy_target(tmp_path, monkeypatch):
    """A pcdata ping-pong pair around a legacy-known hash: the ordinal-1 row
    into the legacy hash fills the gap and the walk continues onto the legacy
    edge; the ordinal-2 row out of the legacy hash is dropped."""
    changelog_text = (
        "版本 3.1 -> 3.11\n"
        "【角色丙CharaC】\n"
        "draw_vb: aaaa0003 -> aaaa0004\n"
    )
    repo_dir = make_repo(tmp_path, changelog_text, subdir="repo")
    static_paths(
        monkeypatch,
        tmp_path,
        legacy=[
            {
                "from": "cccc0001",
                "to": "cccc0002",
                "characters": ["charae"],
                "label": "1.0 -> 1.1",
            }
        ],
        pcdata=[
            {"From": "dddd0001", "To": "cccc0001", "Comment": "1 CharaE BodyA draw"},
            {"From": "cccc0001", "To": "dddd0001", "Comment": "2 CharaE BodyA draw"},
        ],
    )

    data = load_fixer_data(repo_dir, include_pcdata=True)

    assert [e.version_index for e in data.entries] == [1, 3, 4]
    steps = resolve_hash_chain("dddd0001", "charaebody", data)
    assert steps is not None
    assert [(s.to_hash, s.version_label, s.role) for s in steps] == [
        ("cccc0001", "importer #1", "pcdata"),
        ("cccc0002", "1.0 -> 1.1", "legacy"),
    ]
    steps = resolve_hash_chain("cccc0001", "charaebody", data)
    assert steps is not None
    assert [(s.to_hash, s.role) for s in steps] == [("cccc0002", "legacy")]
    assert resolve_hash_chain("dddd0001", "", data) == []
    assert resolve_hash_chain("cccc0001", "", data) == []


def test_pcdata_ib_remap_fill_and_counts_walk(tmp_path, monkeypatch):
    """pcdata ib remap arrays fill ib_index_changes for a hash whose remap
    data is missing, without touching chains/entries; counts arrays win over
    indexes arrays for kind="counts"."""
    changelog_text = (
        "版本 3.1 -> 3.11\n"
        "【角色丙CharaC】\n"
        "IB: eeee0001 -> eeee0002（身体）\n"
        "  object_indexes: [0, 100] -> [0, 120]\n"
        "  texcoord_vb: ffff0001 -> ffff0003\n"
    )
    repo_dir = make_repo(tmp_path, changelog_text, subdir="repo")
    static_paths(
        monkeypatch,
        tmp_path,
        pcdata=[
            {
                "From": "ffff0001",
                "To": "ffff0002",
                "FromIndexes": "[0]",
                "ToIndexes": "[500]",
                "FromIndexCounts": "[200]",
                "ToIndexCounts": "[60]",
                "Comment": "3 CharaC BodyA ib",
            }
        ],
    )

    data = load_fixer_data(repo_dir, include_pcdata=True)

    assert [e.role for e in data.ib_index_changes["ffff0001"]] == ["pcdata"]
    assert not any(e.role == "pcdata" for e in data.entries)
    assert [e.role for e in data.chains["ffff0001"]] == ["texcoord_vb"]

    value, steps = walk_index_value(0, "ffff0001", data)
    assert (value, [s.to_hash for s in steps]) == (500, ["ffff0002"])
    assert walk_index_value(0, "ffff0001", data, kind="counts") == (0, [])
    value, steps = walk_index_value(200, "ffff0001", data, kind="counts")
    assert (value, [s.to_hash for s in steps]) == (60, ["ffff0002"])
    assert walk_index_value(200, "ffff0001", data, kind="indexes") == (200, [])

    value, steps = walk_index_value(0, "eeee0001", data, kind="counts")
    assert (value, [s.to_hash for s in steps]) == (0, ["eeee0002"])
    assert walk_index_value(100, "eeee0001", data, kind="counts")[0] == 120


def test_pcdata_absent_file_means_unchanged_behavior(tmp_path, monkeypatch):
    """Without a PlayerCharacterData.json the pcdata feature is inert: the
    load is exactly the plain changelog parse (the pre-feature result) and
    include_pcdata=True changes nothing."""
    repo_dir = tmp_path / "repo"
    (repo_dir / repo.CHARACTERS_DIR_NAME).mkdir(parents=True)
    (repo_dir / repo.CHANGELOG_NAME).write_text(SNAPSHOT_CHANGELOG, encoding="utf-8")
    static_paths(monkeypatch, tmp_path)

    base = load_fixer_data(repo_dir)
    with_pc = load_fixer_data(repo_dir, include_pcdata=True)

    parsed = changelog.parse_changelog_file(repo.changelog_path(repo_dir))
    assert not any(e.role == "pcdata" for e in base.entries)
    assert base.entries == parsed
    assert base.chains == changelog.build_chain_index(parsed)
    assert base.ib_index_changes
    assert with_pc.entries == base.entries
    assert with_pc.chains == base.chains
    assert with_pc.ib_index_changes == base.ib_index_changes


def empty_patches_dir(monkeypatch, tmp_path):
    """Point tools.patches.user_patches_dir at an empty temp data folder."""
    patches_dir = tmp_path / "data"
    patches_dir.mkdir()
    monkeypatch.setattr("tools.patches.user_patches_dir", lambda: patches_dir)
    return patches_dir


def test_load_fixer_data_none_repo_all_sources_missing(tmp_path, monkeypatch):
    """repo_dir=None with every optional source absent yields an all-empty
    dataset: no chains, entries, patches or characters (never raises)."""
    static_paths(monkeypatch, tmp_path)
    empty_patches_dir(monkeypatch, tmp_path)

    data = load_fixer_data(None)

    assert isinstance(data, FixerData)
    assert data.chains == {}
    assert data.entries == []
    assert data.ib_index_changes == {}
    assert data.user_patches == {}
    assert isinstance(data.db, CharacterDB)
    assert data.db.characters == {}
    assert data.db.reverse == {}


def test_load_fixer_data_none_repo_user_patches_still_apply(tmp_path, monkeypatch):
    """A data/*.txt patch survives the repo-less load and resolves as the
    authoritative chain step for its from-hash."""
    static_paths(monkeypatch, tmp_path)
    patches_dir = empty_patches_dir(monkeypatch, tmp_path)
    (patches_dir / "patches.txt").write_text(
        "deadbeef to feedface\n", encoding="utf-8"
    )

    data = load_fixer_data(None)

    assert set(data.user_patches) == {"deadbeef"}
    patch = data.user_patches["deadbeef"]
    assert (patch.role, patch.from_hash, patch.to_hash) == (
        "user",
        "deadbeef",
        "feedface",
    )
    assert resolve_hash_chain("deadbeef", "", data) == [patch]


def test_load_fixer_data_none_repo_pcdata_gap_fill_applies(tmp_path, monkeypatch):
    """pcdata rows gap-fill chains and ib_index_changes even without any repo
    clone, while the character DB stays empty."""
    static_paths(
        monkeypatch,
        tmp_path,
        pcdata=[
            {
                "From": "ffff0001",
                "To": "ffff0002",
                "FromIndexes": "[0]",
                "ToIndexes": "[500]",
                "Comment": "3 CharaC BodyA ib",
            }
        ],
    )
    empty_patches_dir(monkeypatch, tmp_path)

    data = load_fixer_data(None, include_pcdata=True)

    assert [(e.role, e.version_label) for e in data.entries] == [
        ("pcdata", "importer #3")
    ]
    assert [e.role for e in data.ib_index_changes["ffff0001"]] == ["pcdata"]
    value, steps = walk_index_value(0, "ffff0001", data)
    assert (value, [s.to_hash for s in steps]) == (500, ["ffff0002"])
    assert isinstance(data.db, CharacterDB)
    assert data.db.characters == {}
    assert data.db.reverse == {}


def belle_component(object_indexes=(0, 45060, 45324, 46932), classifications=("A", "B", "C", "D")):
    """The belle Body component referenced by the remap tests' reverse index."""
    return Component(
        name="Body",
        object_indexes=None if object_indexes is None else list(object_indexes),
        object_classifications=None if classifications is None else list(classifications),
    )


def remap_db(components):
    """CharacterDB whose reverse index maps the belle IB hash to its Body refs."""
    db = CharacterDB()
    db.characters["belle"] = Character(name="belle", components=list(components))
    db.reverse = {"619c5c94": [HashRef(character="belle", component="Body", role="ib")]}
    return db


def remap_data(db):
    """FixerData with the 43ed3c22 -> 619c5c94 chain plus the given character db."""
    data = build_data([entry("43ed3c22", "619c5c94", ["belle"])])
    data.db = db
    return data


def write_remap_ini(tmp_path, anchor, values, chars="ABCD"):
    """One BelleMod.ini with a TextureOverrideTestBody<letter> section per value."""
    text = "".join(
        f"[TextureOverrideTestBody{char}]\nhash = {anchor}\nmatch_first_index = {value}\n\n"
        for value, char in zip(values, chars)
    )
    dst = tmp_path / "BelleMod.ini"
    dst.write_text(text, encoding="utf-8", newline="")
    return dst



def test_index_remap_letter_pairing(tmp_path):
    data = remap_data(remap_db([belle_component()]))
    dst = write_remap_ini(tmp_path, "43ed3c22", [0, 46212, 46476, 48084])
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 2, "43ed3c22", "619c5c94"),
        ("hash", 6, "43ed3c22", "619c5c94"),
        ("hash", 10, "43ed3c22", "619c5c94"),
        ("hash", 14, "43ed3c22", "619c5c94"),
        ("match_first_index", 7, "46212", "45060"),
        ("match_first_index", 11, "46476", "45324"),
        ("match_first_index", 15, "48084", "46932"),
    ]
    remaps = [s for s in plan.suggestions if s.kind == "match_first_index"]
    assert all(s.reason.startswith("character table") for s in remaps)
    assert remaps[0].reason == (
        "character table belle Body: object_indexes [0, 45060, 45324, 46932] (position 1)"
    )


def test_index_remap_apply_plan_rewrites_lines(tmp_path):
    data = remap_data(remap_db([belle_component()]))
    dst = write_remap_ini(tmp_path, "43ed3c22", [0, 46212, 46476, 48084])
    store = tmp_path / "store"
    (plan,) = scan_files([dst], data)
    assert apply_plan(
        plan, data, store_dir=store, mods_dir=tmp_path, log=quiet, backup=False
    ) is True

    assert dst.read_text(encoding="utf-8") == "".join(
        f"[TextureOverrideTestBody{char}]\nhash = 619c5c94\nmatch_first_index = {value}\n\n"
        for value, char in zip([0, 45060, 45324, 46932], "ABCD")
    )
    assert scan_files([dst], data) == []


def test_index_remap_positional_without_classifications(tmp_path):
    data = remap_data(remap_db([belle_component(classifications=None)]))
    dst = write_remap_ini(tmp_path, "43ed3c22", [0, 46212, 45324, 46932])
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 2, "43ed3c22", "619c5c94"),
        ("hash", 6, "43ed3c22", "619c5c94"),
        ("hash", 10, "43ed3c22", "619c5c94"),
        ("hash", 14, "43ed3c22", "619c5c94"),
        ("match_first_index", 7, "46212", "45060"),
    ]


def test_index_remap_fires_on_already_current_anchor(tmp_path):
    data = FixerData(chains={}, ib_index_changes={}, db=remap_db([belle_component()]))
    dst = write_remap_ini(tmp_path, "619c5c94", [0, 46212, 46476, 48084])
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("match_first_index", 7, "46212", "45060"),
        ("match_first_index", 11, "46476", "45324"),
        ("match_first_index", 15, "48084", "46932"),
    ]


def test_index_warning_logged_and_never_applied(tmp_path):
    data = remap_data(remap_db([belle_component(object_indexes=None)]))
    dst = write_remap_ini(tmp_path, "43ed3c22", [46212], chars="A")
    original = dst.read_bytes()
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 2, "43ed3c22", "619c5c94"),
        (INDEX_WARNING_KIND, 3, "46212", "46212"),
    ]
    warning = plan.suggestions[1]
    logs: list[str] = []
    assert (
        apply_plan(
            FilePlan(path=str(dst)),
            data,
            store_dir=tmp_path / "store",
            mods_dir=tmp_path,
            log=logs.append,
            suggestions=[warning],
        )
        is False
    )
    assert any(m.startswith("warning: IB 43ed3c22 -> 619c5c94") for m in logs)
    assert dst.read_bytes() == original


def test_index_warning_on_ambiguous_counts(tmp_path):
    data = remap_data(remap_db([belle_component()]))
    dst = write_remap_ini(tmp_path, "43ed3c22", [0, 46212, 46476], chars="ABC")
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 2, "43ed3c22", "619c5c94"),
        ("hash", 6, "43ed3c22", "619c5c94"),
        ("hash", 10, "43ed3c22", "619c5c94"),
        (INDEX_WARNING_KIND, 3, "0", "0"),
        (INDEX_WARNING_KIND, 7, "46212", "46212"),
        (INDEX_WARNING_KIND, 11, "46476", "46476"),
    ]


def test_index_remap_dedupes_against_scan_text(tmp_path):
    db = remap_db([belle_component(object_indexes=(0, 45060), classifications=("A", "B"))])
    data = FixerData(
        chains={"43ed3c22": [entry("43ed3c22", "619c5c94", ["belle"])]},
        ib_index_changes={
            "43ed3c22": [
                index_entry(
                    "43ed3c22", "619c5c94", 1, [0, 46212], [0, 99999], ["belle"], role="pcdata"
                )
            ]
        },
        db=db,
    )
    dst = write_remap_ini(tmp_path, "43ed3c22", [0, 46212], chars="AB")
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 2, "43ed3c22", "619c5c94"),
        ("hash", 6, "43ed3c22", "619c5c94"),
        ("match_first_index", 7, "46212", "99999"),
    ]


def redump_db():
    """CharacterDB adding role-"ib" and role-"blend" refs for the redump targets."""
    db = remap_db([belle_component()])
    db.reverse["a7683988"] = [HashRef(character="belle", component="Hair", role="ib")]
    db.reverse["0a00d846"] = [HashRef(character="belle", component="Shirt", role="blend")]
    return db


def redump_data():
    """FixerData with the ib and blend chains plus the redump reverse index."""
    data = build_data(
        [
            entry("43ed3c22", "619c5c94", ["belle"]),
            entry("ea055cac", "a7683988", ["belle"]),
            entry("0139f7e8", "0a00d846", ["belle"], role="blend"),
        ]
    )
    data.db = redump_db()
    return data


def write_custom_buffers_ini(tmp_path, with_filename=True):
    """One BelleMod.ini with a custom .ib Resource plus five hash-only sections."""
    lines = ["[ResourceBelleSummerSkinBodyA]", "type = Buffer", "stride = 4"]
    if with_filename:
        lines.append("filename = BelleSummerSkinBodyA.ib")
    lines += [
        "",
        "[TextureOverrideBelleBodyIB]",
        "hash = 43ed3c22",
        "",
        "[TextureOverrideBelleBodyA]",
        "hash = 43ed3c22",
        "",
        "[TextureOverrideBelleHairIB]",
        "hash = ea055cac",
        "",
        "[TextureOverrideBelleHairA]",
        "hash = ea055cac",
        "",
        "[TextureOverrideBelleShirtBlend]",
        "hash = 0139f7e8",
        "",
    ]
    dst = tmp_path / "BelleMod.ini"
    dst.write_text("\n".join(lines), encoding="utf-8", newline="")
    return dst


def test_redump_warning_for_ib_renames_with_custom_buffers(tmp_path):
    data = redump_data()
    dst = write_custom_buffers_ini(tmp_path)
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 7, "43ed3c22", "619c5c94"),
        (INDEX_WARNING_KIND, 7, "43ed3c22", "43ed3c22"),
        ("hash", 10, "43ed3c22", "619c5c94"),
        ("hash", 13, "ea055cac", "a7683988"),
        (INDEX_WARNING_KIND, 13, "ea055cac", "ea055cac"),
        ("hash", 16, "ea055cac", "a7683988"),
        ("hash", 19, "0139f7e8", "0a00d846"),
    ]
    assert [s.reason for s in plan.suggestions if s.kind == INDEX_WARNING_KIND] == [
        (
            "IB 43ed3c22 -> 619c5c94: the mod ships custom .ib/.buf buffers; "
            "a structurally changed mesh needs re-dumped binaries - ini fixes alone "
            "will not render correctly"
        ),
        (
            "IB ea055cac -> a7683988: the mod ships custom .ib/.buf buffers; "
            "a structurally changed mesh needs re-dumped binaries - ini fixes alone "
            "will not render correctly"
        ),
    ]


def test_no_redump_warning_without_custom_buffers(tmp_path):
    data = redump_data()
    dst = write_custom_buffers_ini(tmp_path, with_filename=False)
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 6, "43ed3c22", "619c5c94"),
        ("hash", 9, "43ed3c22", "619c5c94"),
        ("hash", 12, "ea055cac", "a7683988"),
        ("hash", 15, "ea055cac", "a7683988"),
        ("hash", 18, "0139f7e8", "0a00d846"),
    ]


def test_redump_warning_deduped_per_rename(tmp_path):
    data = remap_data(remap_db([belle_component()]))
    text = (
        "[ResourceBelleSummerSkinBodyA]\n"
        "filename = BelleSummerSkinBodyA.ib\n\n"
        + "".join(
            f"[TextureOverrideTestBody{char}]\nhash = 43ed3c22\n\n" for char in "ABC"
        )
    )
    dst = tmp_path / "BelleMod.ini"
    dst.write_text(text, encoding="utf-8", newline="")
    (plan,) = scan_files([dst], data)

    assert [(s.kind, s.line_no, s.old, s.new) for s in plan.suggestions] == [
        ("hash", 5, "43ed3c22", "619c5c94"),
        (INDEX_WARNING_KIND, 5, "43ed3c22", "43ed3c22"),
        ("hash", 8, "43ed3c22", "619c5c94"),
        ("hash", 11, "43ed3c22", "619c5c94"),
    ]


def _face_data(face_hashes: frozenset[str] | None = None) -> FixerData:
    return FixerData(
        chains={
            "aaaaaaaa": [
                ChangeEntry(
                    role="texcoord",
                    from_hash="aaaaaaaa",
                    to_hash="bbbbbbbb",
                    version_index=1,
                )
            ]
        },
        ib_index_changes={},
        db=CharacterDB(),
        entries=[
            ChangeEntry(
                role="texcoord",
                from_hash="aaaaaaaa",
                to_hash="bbbbbbbb",
                version_index=1,
            )
        ],
        face_texcoord_hashes=face_hashes or frozenset({"aaaaaaaa", "bbbbbbbb"}),
    )


def test_scan_flags_face_texcoord_rename_with_buffer_warning(tmp_path):
    path = tmp_path / "m.ini"
    path.write_text(
        "[TextureOverrideSampleFaceTexcoord]\r\n"
        "hash = aaaaaaaa\r\n"
        "vb1 = ResourceSampleFaceTexcoord\r\n"
        "\r\n"
        "[ResourceSampleFaceTexcoord]\r\n"
        "type = Buffer\r\n"
        "stride = 36\r\n"
        "filename = FaceTexcoord.buf\r\n",
        encoding="utf-8",
    )
    (plan,) = scan_files([path], _face_data())
    suggestions = plan.suggestions
    hash_fixes = [s for s in suggestions if s.kind == "hash"]
    warnings = [s for s in suggestions if s.kind == INDEX_WARNING_KIND]
    assert [(s.old, s.new) for s in hash_fixes] == [("aaaaaaaa", "bbbbbbbb")]
    assert len(warnings) == 1
    assert "face texcoord" in warnings[0].reason


def test_scan_repoints_untracked_face_texcoord_to_table_current(tmp_path):
    db = CharacterDB()
    character = Character(name="NicoleTest")
    character.components.append(
        Component(name="Face-脸", fields={"texcoord": "cccc0003"})
    )
    db.characters["NicoleTest"] = character
    db.reverse["dddd0004"] = [HashRef("NicoleTest", "Face-脸", "ib")]
    data = FixerData(chains={}, ib_index_changes={}, db=db)
    path = tmp_path / "m.ini"
    path.write_text(
        "[TextureOverrideNicoleTestFaceIB]\r\n"
        "hash = dddd0004\r\n"
        "handling = skip\r\n"
        "\r\n"
        "[TextureOverrideNicoleTestFaceTexcoord]\r\n"
        "hash = eeee0005\r\n"
        "vb1 = ResourceFaceTexcoord\r\n",
        encoding="utf-8",
    )
    (plan,) = scan_files([path], data)
    assert [(s.kind, s.old, s.new) for s in plan.suggestions] == [
        ("hash", "eeee0005", "cccc0003")
    ]
    assert "untracked" in plan.suggestions[0].reason


def test_scan_repoint_ignores_eyebrow_sections(tmp_path):
    db = CharacterDB()
    character = Character(name="NicoleTest")
    character.components.append(
        Component(name="Face-脸", fields={"texcoord": "cccc0003"})
    )
    db.characters["NicoleTest"] = character
    db.reverse["dddd0004"] = [HashRef("NicoleTest", "Face-脸", "ib")]
    data = FixerData(chains={}, ib_index_changes={}, db=db)
    path = tmp_path / "m.ini"
    path.write_text(
        "[TextureOverrideNicoleTestFaceIB]\r\n"
        "hash = dddd0004\r\n"
        "handling = skip\r\n"
        "\r\n"
        "[TextureOverrideNicoleTestEyebrowTexcoord]\r\n"
        "hash = eeee0005\r\n"
        "vb1 = ResourceEyebrowTexcoord\r\n",
        encoding="utf-8",
    )
    assert scan_files([path], data) == []


def test_cached_ini_parse_memoizes_until_rewrite(tmp_path, monkeypatch):
    """A memo hit skips the file read; a same-size rewrite with a bumped mtime misses."""
    fixer._INI_PARSE_MEMO.clear()
    path = tmp_path / "m.ini"
    original = "[TextureOverrideA]\nhash = aaaa0000\n"
    path.write_text(original, encoding="utf-8", newline="\n")
    reads = []
    real_read = fixer.read_ini_text

    def spy_read(read_path):
        reads.append(Path(read_path))
        return real_read(read_path)

    monkeypatch.setattr(fixer, "read_ini_text", spy_read)

    text, hints, sections = fixer.cached_ini_parse(path)
    assert text == original
    assert hints == {"aaaa0000": {"a"}}
    assert [section.name for section in sections] == ["TextureOverrideA"]
    assert reads == [path]

    assert fixer.cached_ini_parse(path) == (text, hints, sections)
    assert reads == [path]  # memo hit: no second read

    rewritten = b"[TextureOverrideB]\nhash = bbbb1111\n"
    assert len(rewritten) == len(original.encode("utf-8"))
    path.write_bytes(rewritten)
    os.utime(path, (1_000_000_000, 1_000_000_000))  # deterministic mtime bump

    text2, hints2, _sections2 = fixer.cached_ini_parse(path)
    assert reads == [path, path]  # mtime key changed: re-read despite same size
    assert text2 == rewritten.decode("utf-8")
    assert hints2 == {"bbbb1111": {"b"}}


def test_cached_ini_parse_evicts_oldest_past_cap(tmp_path, monkeypatch):
    """Inserting past _INI_PARSE_MEMO_CAP evicts the least-recently-used entry."""
    fixer._INI_PARSE_MEMO.clear()
    monkeypatch.setattr(fixer, "_INI_PARSE_MEMO_CAP", 2)
    paths = []
    for index, name in enumerate(("a.ini", "b.ini", "c.ini")):
        path = tmp_path / name
        path.write_text(
            f"[TextureOverrideX{index}]\nhash = aaaa000{index}\n", encoding="utf-8"
        )
        fixer.cached_ini_parse(path)
        paths.append(path)

    assert len(fixer._INI_PARSE_MEMO) == 2
    memo_paths = {key[0] for key in fixer._INI_PARSE_MEMO}
    assert str(paths[0]) not in memo_paths
    assert {str(paths[1]), str(paths[2])} <= memo_paths


def test_cached_ini_parse_missing_or_directory_is_none(tmp_path):
    """Missing paths and directory paths return None and are never memoized."""
    fixer._INI_PARSE_MEMO.clear()
    missing = tmp_path / "missing.ini"

    assert fixer.cached_ini_parse(missing) is None
    assert fixer.cached_ini_parse(tmp_path) is None
    assert fixer._INI_PARSE_MEMO == {}


def test_cached_ini_parse_retries_after_failed_read(tmp_path, monkeypatch):
    """A transient OSError read is not memoized: the next call parses fresh."""
    fixer._INI_PARSE_MEMO.clear()
    path = tmp_path / "m.ini"
    content = "[TextureOverrideA]\nhash = aaaa0000\n"
    path.write_text(content, encoding="utf-8", newline="\n")
    real_read = fixer.read_ini_text
    failed = {"once": False}

    def flaky_read(_path):
        if not failed["once"]:
            failed["once"] = True
            raise OSError("transient read failure")
        return real_read(_path)

    monkeypatch.setattr(fixer, "read_ini_text", flaky_read)

    assert fixer.cached_ini_parse(path) is None
    assert fixer._INI_PARSE_MEMO == {}

    text, hints, _sections = fixer.cached_ini_parse(path)
    assert text == content
    assert hints == {"aaaa0000": {"a"}}


def test_cached_ini_parse_undecodable_bytes_raise(tmp_path):
    """Undecodable bytes raise ValueError (matching read_ini_text) and skip the memo."""
    fixer._INI_PARSE_MEMO.clear()
    path = tmp_path / "broken.ini"
    path.write_bytes(b"\xff\xfe\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")

    with pytest.raises(ValueError):
        fixer.cached_ini_parse(path)
    assert fixer._INI_PARSE_MEMO == {}
