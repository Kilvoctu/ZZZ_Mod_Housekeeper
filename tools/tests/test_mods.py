"""Tests for tools.mods: mod tree building and per-mod version analysis."""

from copy import deepcopy
from pathlib import Path

from PySide6.QtGui import QColor
import pytest

from tools import changelog, mods, repo
from tools.characters import CharacterDB, HashRef
from tools.fixer import (
    FixerData,
    collect_texture_override_hashes,
    load_fixer_data,
)
from tools.model import ChangeEntry, Character, Component
from tools.mods import (
    ModVersion,
    aggregate_updates,
    analyze_mods,
    analyze_scope,
    retarget_subtree_paths,
    set_mod_enabled,
)
from tools.structure import build_structure
from tools.tests._test_data import LEGACY_ENTRY
from tools.ui.main_window import hashes_text, updates_color, updates_text

BACKUP_PREFIXES = ("DISABLED_versionfix_", "DISABLED_BACKUP_")

EMPTY_DATA = FixerData(chains={}, ib_index_changes={}, db=CharacterDB(), entries=[])

REAL_MODS_DIR = Path(r"G:\randomDevStuff\Test")


def build_classify_tree(base: Path, with_root_ini: bool) -> Path:
    """Synthetic mods tree mirroring the real GymZhuYuan/Subfolder layout.

    with_root_ini toggles the loose root .ini: with it, the root acts as one
    mod itself and (mods.py's documented tradeoff) swallows every nested folder.
    """
    root = base / "mods"
    (root / "GymMod").mkdir(parents=True)
    (root / "GymMod" / "gym.ini").write_text(
        "[TextureOverrideGym]\nhash = aaaa0000\n", encoding="utf-8"
    )
    fashion = root / "Category" / "DISABLED_Fashion"
    (fashion / "body" / "0").mkdir(parents=True)
    (fashion / "fashion.ini").write_text("[TextureOverrideFashion]\n", encoding="utf-8")
    (fashion / "body" / "m.ini").write_text("[TextureOverrideM]\n", encoding="utf-8")
    (fashion / "body" / "0" / "a.ini").write_text(
        "[TextureOverrideA]\n", encoding="utf-8"
    )
    (fashion / "Assets").mkdir()
    loose = root / "Category" / "LoosePack"
    loose.mkdir(parents=True)
    (loose / "x.ini").write_text("[TextureOverrideX]\n", encoding="utf-8")
    (root / "DISABLED_versionfix_123-rogue.ini").write_text(
        "[TextureOverrideBackup]\n", encoding="utf-8"
    )
    backup_dir = root / "DISABLED_versionfix_9"
    backup_dir.mkdir()
    (backup_dir / "inside.ini").write_text(
        "[TextureOverrideBackupDir]\n", encoding="utf-8"
    )
    (root / "EmptyBranch").mkdir()
    if with_root_ini:
        (root / "rogue.ini").write_text("[TextureOverrideRogue]\n", encoding="utf-8")
    return root


def walk_nodes(node):
    yield node
    for child in node.children:
        yield from walk_nodes(child)


def by_name(tree, name):
    matches = [node for node in walk_nodes(tree) if node.name == name]
    assert len(matches) == 1, f"expected exactly one node named {name!r}, got {matches}"
    return matches[0]


def inventory(tree):
    """Every node as (kind, name, disabled), order-independent."""
    return sorted((node.kind, node.name, node.disabled) for node in walk_nodes(tree))


def parent_map(tree):
    parents = {tree.path: None}
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in node.children:
            parents[child.path] = node
            stack.append(child)
    return parents


def nearest_mod(node, parents):
    """Nearest ancestor-or-self acting as a mod (kind "mod", or a root that
    qualifies via direct .ini files or mod.json)."""
    current = node
    while current is not None:
        if current.kind == "mod" or (
            current.kind == "root" and current.version is not None
        ):
            return current
        current = parents[current.path]
    return None


def assert_backups_excluded(tree):
    """No node or included .ini file anywhere mentions either backup convention."""
    for node in walk_nodes(tree):
        assert not node.name.startswith(BACKUP_PREFIXES), node.name
        if node.kind == "file":
            for part in node.path.parts:
                assert not part.startswith(BACKUP_PREFIXES), node.path


def test_tree_classification(tmp_path):
    root = build_classify_tree(tmp_path, with_root_ini=False)
    tree, _summary = mods.analyze_mods(root, EMPTY_DATA)

    assert (tree.kind, tree.name, tree.disabled) == ("root", "mods", False)

    assert inventory(tree) == [
        ("category", "Category", False),
        ("file", "a.ini", False),
        ("file", "fashion.ini", False),
        ("file", "gym.ini", False),
        ("file", "m.ini", False),
        ("file", "x.ini", False),
        ("mod", "DISABLED_Fashion", True),
        ("mod", "GymMod", False),
        ("mod", "LoosePack", False),
        ("root", "mods", False),
        ("subfolder", "0", False),
        ("subfolder", "Assets", False),
        ("subfolder", "body", False),
    ]

    gym = by_name(tree, "GymMod")
    fashion = by_name(tree, "DISABLED_Fashion")
    loose = by_name(tree, "LoosePack")

    parents = parent_map(tree)
    assert nearest_mod(by_name(tree, "a.ini"), parents) is fashion
    assert nearest_mod(by_name(tree, "m.ini"), parents) is fashion
    assert nearest_mod(by_name(tree, "fashion.ini"), parents) is fashion
    assert nearest_mod(by_name(tree, "gym.ini"), parents) is gym
    assert nearest_mod(by_name(tree, "x.ini"), parents) is loose

    assert [child.name for child in tree.children] == ["Category", "GymMod"]
    assert [child.name for child in fashion.children] == [
        "Assets",
        "body",
        "fashion.ini",
    ]
    assert [child.name for child in by_name(tree, "body").children] == ["0", "m.ini"]

    assert_backups_excluded(tree)


def test_tree_root_acts_as_mod(tmp_path):
    root = build_classify_tree(tmp_path, with_root_ini=True)
    tree, _summary = mods.analyze_mods(root, EMPTY_DATA)

    assert tree.kind == "root"
    assert [child.name for child in tree.children] == [
        "Category",
        "EmptyBranch",
        "GymMod",
        "rogue.ini",
    ]
    assert tree.children[-1].kind == "file"

    for name in ("GymMod", "DISABLED_Fashion", "LoosePack", "EmptyBranch"):
        assert by_name(tree, name).kind == "subfolder"
    assert by_name(tree, "DISABLED_Fashion").disabled is True
    parents = parent_map(tree)
    assert nearest_mod(by_name(tree, "gym.ini"), parents) is tree
    assert nearest_mod(by_name(tree, "a.ini"), parents) is tree

    assert_backups_excluded(tree)


def test_mod_json_without_ini_is_pruned(tmp_path):
    root = tmp_path / "mods"
    category = root / "Category"
    dead = category / "DeadPack"
    dead.mkdir(parents=True)
    (dead / "mod.json").write_text("{}\n", encoding="utf-8")
    alive = category / "AliveMod"
    alive.mkdir()
    (alive / "alive.ini").write_text("[TextureOverrideA]\n", encoding="utf-8")
    tree, _summary = mods.analyze_mods(root, EMPTY_DATA)

    assert inventory(tree) == [
        ("category", "Category", False),
        ("file", "alive.ini", False),
        ("mod", "AliveMod", False),
        ("root", "mods", False),
    ]
    assert [node.name for node in walk_nodes(tree) if node.name == "DeadPack"] == []


def test_mod_json_pack_collapses_parts(tmp_path):
    root = tmp_path / "mods"
    pack = root / "Pack"
    (pack / "Part1").mkdir(parents=True)
    (pack / "Part2").mkdir()
    (pack / "mod.json").write_text("{}\n", encoding="utf-8")
    (pack / "Part1" / "part1.ini").write_text(
        "[TextureOverrideP1]\nhash = aaaa0000\n", encoding="utf-8"
    )
    (pack / "Part2" / "part2.ini").write_text(
        "[TextureOverrideP2]\nhash = cccc0000\n", encoding="utf-8"
    )

    data = version_data(LADDER_ENTRIES, {"cccc0000": [HashRef("Anby", "Body", "ib")]})
    tree, summary = mods.analyze_mods(root, data)

    assert inventory(tree) == [
        ("file", "part1.ini", False),
        ("file", "part2.ini", False),
        ("mod", "Pack", False),
        ("root", "mods", False),
        ("subfolder", "Part1", False),
        ("subfolder", "Part2", False),
    ]
    pack_node = by_name(tree, "Pack")
    parents = parent_map(tree)
    assert nearest_mod(by_name(tree, "part1.ini"), parents) is pack_node
    assert nearest_mod(by_name(tree, "part2.ini"), parents) is pack_node

    version = version_of(pack_node)
    assert (version.label, version.is_latest) == ("2.0", False)
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (1, 1, 0, 2)
    assert summary == mods.AnalysisSummary(
        mods=1, current=0, outdated=1, unknown=0, backups_skipped=0, files_scanned=2
    )


def test_mod_json_highest_ancestor_wins(tmp_path):
    root = tmp_path / "mods"
    upper = root / "UpperPack"
    (upper / "Part").mkdir(parents=True)
    (upper / "MOD.JSON").write_text("{}\n", encoding="utf-8")
    (upper / "Part" / "part.ini").write_text(
        "[TextureOverridePart]\nhash = aaaa0000\n", encoding="utf-8"
    )
    direct = root / "DirectPack"
    (direct / "Part2").mkdir(parents=True)
    (direct / "mod.json").write_text("{}\n", encoding="utf-8")
    (direct / "top.ini").write_text(
        "[TextureOverrideTop]\nhash = cccc0000\n", encoding="utf-8"
    )
    (direct / "Part2" / "part2.ini").write_text(
        "[TextureOverridePart2]\nhash = dddd0000\n", encoding="utf-8"
    )

    data = version_data(
        LADDER_ENTRIES,
        {
            "cccc0000": [HashRef("Anby", "Body", "blend_vb")],
            "dddd0000": [HashRef("Anby", "Body", "ib")],
        },
    )
    tree, summary = mods.analyze_mods(root, data)

    assert inventory(tree) == [
        ("file", "part.ini", False),
        ("file", "part2.ini", False),
        ("file", "top.ini", False),
        ("mod", "DirectPack", False),
        ("mod", "UpperPack", False),
        ("root", "mods", False),
        ("subfolder", "Part", False),
        ("subfolder", "Part2", False),
    ]

    upper_node = by_name(tree, "UpperPack")
    parents = parent_map(tree)
    assert nearest_mod(by_name(tree, "part.ini"), parents) is upper_node
    assert by_name(tree, "Part").kind == "subfolder"
    version = version_of(upper_node)
    assert (version.label, version.is_latest) == ("2.0", False)
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (0, 1, 0, 1)

    direct_node = by_name(tree, "DirectPack")
    assert nearest_mod(by_name(tree, "part2.ini"), parents) is direct_node
    assert by_name(tree, "Part2").kind == "subfolder"
    version = version_of(direct_node)
    assert (version.label, version.is_latest) == ("2.2", True)
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (2, 0, 0, 2)

    assert summary == mods.AnalysisSummary(
        mods=2, current=1, outdated=1, unknown=0, backups_skipped=0, files_scanned=3
    )


def test_root_mod_json_acts_as_one_mod(tmp_path):
    root = tmp_path / "mods"
    root.mkdir()
    (root / "mod.json").write_text("{}\n", encoding="utf-8")
    (root / "Pack").mkdir()
    (root / "Pack" / "p.ini").write_text(
        "[TextureOverrideP]\nhash = cccc0000\n", encoding="utf-8"
    )

    data = version_data(LADDER_ENTRIES, {"cccc0000": [HashRef("Anby", "Body", "ib")]})
    tree, summary = mods.analyze_mods(root, data)

    assert inventory(tree) == [
        ("file", "p.ini", False),
        ("root", "mods", False),
        ("subfolder", "Pack", False),
    ]
    parents = parent_map(tree)
    assert nearest_mod(by_name(tree, "p.ini"), parents) is tree
    version = tree.version
    assert version is not None
    assert (version.label, version.is_latest) == ("2.2", True)
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (1, 0, 0, 1)
    assert summary == mods.AnalysisSummary(
        mods=1, current=1, outdated=0, unknown=0, backups_skipped=0, files_scanned=1
    )


def test_disabled_backup_prefix_files_and_dirs_excluded(tmp_path):
    root = tmp_path / "mods"
    mod = root / "SomeMod"
    (mod / "DISABLED_BACKUP_1727259313").mkdir(parents=True)
    (mod / "original.ini").write_text(
        "[TextureOverrideO]\nhash = cccc0000\n", encoding="utf-8"
    )
    (mod / "DISABLED_BACKUP_1727259311.x.ini").write_text(
        "[TextureOverrideBackup]\n", encoding="utf-8"
    )
    (mod / "DISABLED_versionfix_1727259311.x.ini").write_text(
        "[TextureOverrideBackup]\n", encoding="utf-8"
    )
    (mod / "DISABLED_BACKUP_1727259313" / "nested.ini").write_text(
        "[TextureOverrideNested]\n", encoding="utf-8"
    )
    top_backup = root / "DISABLED_BACKUP_1727259312"
    top_backup.mkdir()
    (top_backup / "inner.ini").write_text("[TextureOverrideInner]\n", encoding="utf-8")
    alive = root / "Alive"
    alive.mkdir()
    (alive / "alive.ini").write_text(
        "[TextureOverrideA]\nhash = dddd0000\n", encoding="utf-8"
    )

    data = version_data(
        LADDER_ENTRIES,
        {
            "cccc0000": [HashRef("Anby", "Body", "blend_vb")],
            "dddd0000": [HashRef("Anby", "Body", "ib")],
        },
    )
    tree, summary = mods.analyze_mods(root, data)

    assert inventory(tree) == [
        ("file", "alive.ini", False),
        ("file", "original.ini", False),
        ("mod", "Alive", False),
        ("mod", "SomeMod", False),
        ("root", "mods", False),
    ]
    original = by_name(tree, "original.ini")
    original_version = version_of(original)
    assert (original_version.label, original_version.is_latest) == ("2.2", True)
    assert summary == mods.AnalysisSummary(
        mods=2, current=2, outdated=0, unknown=0, backups_skipped=4, files_scanned=2
    )
    assert_backups_excluded(tree)


def test_set_mod_enabled_disables_prepends_prefix(tmp_path):
    mod = tmp_path / "Cool Mod"
    mod.mkdir()
    (mod / "cool.ini").write_text("[TextureOverrideCool]\n", encoding="utf-8")

    result = set_mod_enabled(mod, False)

    assert result == tmp_path / "DISABLED_Cool Mod"
    assert result.is_dir()
    assert (result / "cool.ini").exists()
    assert not mod.exists()


def test_set_mod_enabled_enables_strips_prefix(tmp_path):
    mod = tmp_path / "DISABLED_Cool Mod"
    mod.mkdir()
    (mod / "cool.ini").write_text("[TextureOverrideCool]\n", encoding="utf-8")

    result = set_mod_enabled(mod, True)

    assert result == tmp_path / "Cool Mod"
    assert result.is_dir()
    assert (result / "cool.ini").exists()
    assert not mod.exists()


def test_set_mod_enabled_collision_raises(tmp_path):
    mod = tmp_path / "Cool Mod"
    disabled = tmp_path / "DISABLED_Cool Mod"
    mod.mkdir()
    disabled.mkdir()

    with pytest.raises(FileExistsError):
        set_mod_enabled(disabled, True)
    assert mod.is_dir() and disabled.is_dir()

    with pytest.raises(FileExistsError):
        set_mod_enabled(mod, False)
    assert mod.is_dir() and disabled.is_dir()


def test_set_mod_enabled_noop_when_already_in_state(tmp_path):
    mod = tmp_path / "Cool Mod"
    disabled = tmp_path / "DISABLED_Cool Mod"
    mod.mkdir()
    disabled.mkdir()

    result = set_mod_enabled(mod, True)
    assert result == mod
    assert mod.is_dir()
    result = set_mod_enabled(disabled, False)
    assert result == disabled
    assert disabled.is_dir()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "Cool Mod",
        "DISABLED_Cool Mod",
    ]


def test_collect_texture_override_hashes_section_filtering(tmp_path):
    path = tmp_path / "mod.ini"
    path.write_text(
        "hash = 00000000\n"
        "[ShaderOverrideX]\n"
        "hash = 11111111\n"
        "[TextureOverrideY]\n"
        "hash = 22222222\n"
        "hash = 33333333\n"
        "hash = 22222222\n"
        "[Constants]\n"
        "hash = 44444444\n"
        "[textureoverridez]\n"
        "hash = 55555555\n"
        "[TextureOverride]\n"
        "hash = 66666666\n",
        encoding="utf-8",
    )
    assert collect_texture_override_hashes(path) == [
        "22222222",
        "33333333",
        "55555555",
        "66666666",
    ]
    assert collect_texture_override_hashes(tmp_path / "missing.ini") == []


def ladder_entry(from_hash, to_hash, version_index, version_label):
    return ChangeEntry(
        from_hash=from_hash,
        to_hash=to_hash,
        version_index=version_index,
        role="blend_vb",
        version_label=version_label,
    )


LADDER_ENTRIES = [
    ladder_entry("aaaa0000", "bbbb0000", 1, "2.0 → 2.1"),
    ladder_entry("bbbb0000", "cccc0000", 2, "2.1 → 2.2"),
]


def version_data(entries, reverse: dict[str, list[HashRef]]):
    """Synthetic FixerData: chains from the entries, reverse index as given."""
    db = CharacterDB()
    db.reverse = dict(reverse)
    return FixerData(
        chains=changelog.build_chain_index(entries),
        ib_index_changes={},
        db=db,
        entries=entries,
    )


def classify(data, hashes, hints=None):
    ladder, latest_index = mods.version_ladder(data.entries)
    version = mods.version_for_hashes(hashes, data, ladder, latest_index, hints)
    assert version is not None
    return version


def version_of(node):
    """The analyzed ModVersion of a node, asserted present."""
    version = node.version
    assert version is not None
    return version


def test_version_classification_matrix():
    data = version_data(
        LADDER_ENTRIES,
        {
            "cccc0000": [HashRef("Anby", "Body", "blend_vb")],
            "dddd0000": [HashRef("Anby", "Body", "ib")],
        },
    )

    outdated_20 = classify(data, ["aaaa0000"])
    assert (outdated_20.label, outdated_20.is_latest) == ("2.0", False)
    assert outdated_20.breaks_label == "2.0 → 2.1"
    assert (
        outdated_20.current_count,
        outdated_20.outdated_count,
        outdated_20.unknown_count,
        outdated_20.total,
    ) == (0, 1, 0, 1)

    outdated_21 = classify(data, ["bbbb0000"])
    assert (outdated_21.label, outdated_21.is_latest) == ("2.1", False)
    assert outdated_21.breaks_label == "2.1 → 2.2"

    for hash_value in ("cccc0000", "dddd0000"):
        current = classify(data, [hash_value])
        assert (current.label, current.is_latest) == ("2.2", True)
        assert current.breaks_label is None
        assert (
            current.current_count,
            current.outdated_count,
            current.unknown_count,
            current.total,
        ) == (1, 0, 0, 1)

    unknown = classify(data, ["eeee0000"])
    assert (unknown.label, unknown.is_latest) == ("unknown", False)
    assert (
        unknown.current_count,
        unknown.outdated_count,
        unknown.unknown_count,
        unknown.total,
    ) == (0, 0, 1, 1)

    data.db.reverse["bbbb0000"] = [HashRef("Anby", "Body", "ib")]
    both = classify(data, ["bbbb0000"])
    assert (both.label, both.is_latest) == ("2.1", False)
    assert (
        both.current_count,
        both.outdated_count,
        both.unknown_count,
        both.total,
    ) == (0, 1, 0, 1)

    scope = classify(data, ["eeee0000", "ffff0000"])
    assert (scope.label, scope.is_latest) == ("unknown", False)
    assert (
        scope.current_count,
        scope.outdated_count,
        scope.unknown_count,
        scope.total,
    ) == (0, 0, 2, 2)

    mixed = classify(data, ["aaaa0000", "cccc0000"])
    assert (mixed.label, mixed.is_latest) == ("2.0", False)
    assert (
        mixed.current_count,
        mixed.outdated_count,
        mixed.unknown_count,
        mixed.total,
    ) == (1, 1, 0, 2)


def test_version_classification_without_ladder_entries():
    data = version_data(
        LADDER_ENTRIES, {"cccc0000": [HashRef("Anby", "Body", "blend_vb")]}
    )
    empty = FixerData(
        chains=data.chains, ib_index_changes={}, db=data.db, entries=[]
    )
    version = classify(empty, ["aaaa0000", "cccc0000", "eeee0000"])
    assert (version.label, version.is_latest, version.breaks_label) == (
        "unknown",
        False,
        None,
    )
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (0, 0, 3, 3)


def test_chain_target_without_table_row_counts_outdated():
    entry = ladder_entry("aaaa0000", "bbbb0000", 1, "2.0 → 2.1")
    data = version_data([entry], {"dddd0000": [HashRef("Anby", "Body", "ib")]})
    version = classify(data, ["bbbb0000"])
    assert (version.label, version.is_latest, version.breaks_label) == (
        "unknown",
        False,
        None,
    )
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (0, 1, 0, 1)


def test_chain_target_with_empty_table_stays_unknown():
    entry = ladder_entry("aaaa0000", "bbbb0000", 1, "2.0 → 2.1")
    data = version_data([entry], {})
    version = classify(data, ["bbbb0000"])
    assert (version.label, version.is_latest, version.breaks_label) == (
        "unknown",
        False,
        None,
    )
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (0, 0, 1, 1)


def test_round_trip_chain_target_in_table_counts_current():
    data = version_data(
        ROUND_TRIP_ENTRIES, {"dd86f5ae": [HashRef("Promeia", "Body", "ib")]}
    )
    version = classify(data, ["dd86f5ae"])
    assert (version.label, version.is_latest, version.breaks_label) == (
        "3.0",
        True,
        None,
    )
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (1, 0, 0, 1)


LEGACY_ENTRIES = [LEGACY_ENTRY]


def test_hint_gated_legacy_edge_fires():
    data = version_data(
        LEGACY_ENTRIES, {"cccc0000": [HashRef("ZhuYuan", "Body", "ib")]}
    )

    outdated = classify(data, ["aaaa0000"], {"aaaa0000": {"zhuyuanbodyadiffuse"}})
    assert (outdated.label, outdated.is_latest, outdated.breaks_label) == (
        "1.0",
        False,
        "1.0 -> 1.2",
    )
    assert (
        outdated.current_count,
        outdated.outdated_count,
        outdated.unknown_count,
        outdated.total,
    ) == (0, 1, 0, 1)

    unmatched = classify(data, ["aaaa0000"], {"aaaa0000": {"unrelatedchar"}})
    assert (unmatched.label, unmatched.is_latest, unmatched.breaks_label) == (
        "unknown",
        False,
        None,
    )
    assert (
        unmatched.current_count,
        unmatched.outdated_count,
        unmatched.unknown_count,
        unmatched.total,
    ) == (0, 1, 0, 1)

    hintless = classify(data, ["aaaa0000"])
    assert (hintless.label, hintless.is_latest, hintless.breaks_label) == (
        "unknown",
        False,
        None,
    )
    assert (
        hintless.current_count,
        hintless.outdated_count,
        hintless.unknown_count,
        hintless.total,
    ) == (0, 1, 0, 1)


def test_hint_one_matching_context_among_several_is_enough():
    data = version_data(
        LEGACY_ENTRIES, {"cccc0000": [HashRef("ZhuYuan", "Body", "ib")]}
    )

    outdated = classify(data, ["aaaa0000"], {"aaaa0000": {"unrelated", "zhuyuanbody"}})
    assert (outdated.label, outdated.is_latest, outdated.breaks_label) == (
        "1.0",
        False,
        "1.0 -> 1.2",
    )
    assert (
        outdated.current_count,
        outdated.outdated_count,
        outdated.unknown_count,
        outdated.total,
    ) == (0, 1, 0, 1)


def test_hint_mixed_scope_label_from_earliest_breaking_hash():
    data = version_data(
        LEGACY_ENTRIES, {"cccc0000": [HashRef("ZhuYuan", "Body", "ib")]}
    )

    mixed = classify(data, ["aaaa0000", "cccc0000"], {"aaaa0000": {"zhuyuanbody"}})
    assert (mixed.label, mixed.is_latest) == ("1.0", False)
    assert mixed.breaks_label == "1.0 -> 1.2"
    assert (
        mixed.current_count,
        mixed.outdated_count,
        mixed.unknown_count,
        mixed.total,
    ) == (1, 1, 0, 2)


def test_mixed_bucket_without_hint_labels_first_applied_step():
    real_row = ChangeEntry(
        from_hash="aaaa0000",
        to_hash="cccc0000",
        characters=["zhuyuan"],
        role="diffuse",
        version_label="1.2 -> 2.0",
        version_index=2,
    )
    data = version_data(
        [LEGACY_ENTRY, real_row],
        {"cccc0000": [HashRef("ZhuYuan", "Body", "ib")]},
    )

    version = classify(data, ["aaaa0000"])
    assert version.outdated_count == 1
    assert version.breaks_label == "1.2 -> 2.0"
    assert version.breaks_label != "1.0 -> 1.2"
    assert (version.label, version.is_latest) == ("1.2", False)
    assert (
        version.current_count,
        version.unknown_count,
        version.total,
    ) == (0, 0, 1)


def test_analyze_mods_end_to_end(tmp_path):
    root = tmp_path / "mods"
    (root / "Alpha").mkdir(parents=True)
    (root / "Alpha" / "a.ini").write_text(
        "[ShaderOverrideS]\nhash = 99999999\n[TextureOverrideA]\nhash = aaaa0000\n",
        encoding="utf-8",
    )
    (root / "Pack" / "deep").mkdir(parents=True)
    (root / "Pack" / "one.ini").write_text(
        "[TextureOverrideO]\nhash = cccc0000\n", encoding="utf-8"
    )
    (root / "Pack" / "deep" / "two.ini").write_text(
        "[TextureOverrideD]\nhash = dddd0000\nhash = eeee0000\n", encoding="utf-8"
    )
    (root / "Ghost").mkdir(parents=True)
    (root / "Ghost" / "g.ini").write_text(
        "[TextureOverrideG]\nhash = eeee0000\n", encoding="utf-8"
    )
    (root / "DISABLED_versionfix_7-a.ini").write_text(
        "[TextureOverrideBackup]\nhash = aaaa0000\n", encoding="utf-8"
    )
    backup_dir = root / "DISABLED_versionfix_8"
    backup_dir.mkdir()
    (backup_dir / "inside.ini").write_text(
        "[TextureOverrideBackupDir]\nhash = aaaa0000\n", encoding="utf-8"
    )

    data = version_data(
        LADDER_ENTRIES,
        {
            "cccc0000": [HashRef("Anby", "Body", "blend_vb")],
            "dddd0000": [HashRef("Anby", "Body", "ib")],
        },
    )
    tree, summary = mods.analyze_mods(root, data)

    alpha = by_name(tree, "Alpha")
    assert alpha.kind == "mod"
    a_file = by_name(tree, "a.ini")
    assert a_file.kind == "file" and a_file.version is not None
    assert a_file.version.label == "2.0"
    assert a_file.version.total == 1
    assert alpha.version == a_file.version

    pack = by_name(tree, "Pack")
    assert pack.kind == "mod"
    one = by_name(tree, "one.ini")
    two = by_name(tree, "two.ini")
    one_version = version_of(one)
    two_version = version_of(two)
    assert (one_version.label, one_version.is_latest) == ("2.2", True)
    assert (two_version.label, two_version.is_latest) == ("2.2", True)
    assert (two_version.current_count, two_version.unknown_count, two_version.total) == (
        1,
        1,
        2,
    )
    pack_version = version_of(pack)
    assert (pack_version.label, pack_version.is_latest) == ("2.2", True)
    assert (
        pack_version.current_count,
        pack_version.unknown_count,
        pack_version.total,
    ) == (2, 1, 3)
    assert by_name(tree, "deep").version is None

    ghost = by_name(tree, "Ghost")
    ghost_version = version_of(ghost)
    assert (ghost_version.label, ghost_version.is_latest) == ("unknown", False)
    assert ghost_version.unknown_count == 1

    assert summary == mods.AnalysisSummary(
        mods=3, current=1, outdated=1, unknown=1, backups_skipped=2, files_scanned=4
    )


def test_analyze_mods_root_acts_as_mod(tmp_path):
    root = tmp_path / "flat"
    (root / "Nested").mkdir(parents=True)
    (root / "top.ini").write_text(
        "[TextureOverrideT]\nhash = aaaa0000\n", encoding="utf-8"
    )
    (root / "Nested" / "n.ini").write_text(
        "[TextureOverrideN]\nhash = cccc0000\n", encoding="utf-8"
    )

    data = version_data(LADDER_ENTRIES, {"cccc0000": [HashRef("Anby", "Body", "ib")]})
    tree, summary = mods.analyze_mods(root, data)

    assert tree.kind == "root"
    assert tree.version is not None
    assert (tree.version.label, tree.version.is_latest) == ("2.0", False)
    assert (
        tree.version.current_count,
        tree.version.outdated_count,
        tree.version.total,
    ) == (1, 1, 2)

    top = by_name(tree, "top.ini")
    assert version_of(top).label == "2.0"
    nested_ini = by_name(tree, "n.ini")
    nested_ini_version = version_of(nested_ini)
    assert (nested_ini_version.label, nested_ini_version.is_latest) == ("2.2", True)
    assert by_name(tree, "Nested").version is None

    assert summary == mods.AnalysisSummary(
        mods=1, current=0, outdated=1, unknown=0, backups_skipped=0, files_scanned=2
    )


def subtree_versions(node):
    """(kind, name, deepcopy(version), variant) for node and its whole subtree."""
    return [
        (sub.kind, sub.name, deepcopy(sub.version), sub.variant)
        for sub in walk_nodes(node)
    ]


def build_scope_mods(base: Path) -> Path:
    """Two sibling mods: Target holds two loose inis plus one nested ini."""
    root = base / "mods"
    target = root / "Target"
    (target / "sub").mkdir(parents=True)
    (target / "a.ini").write_text(
        "[TextureOverrideA]\nhash = aaaa0000\n", encoding="utf-8"
    )
    (target / "c.ini").write_text(
        "[TextureOverrideC]\nhash = cccc0000\n", encoding="utf-8"
    )
    (target / "sub" / "b.ini").write_text(
        "[TextureOverrideB]\nhash = bbbb0000\n", encoding="utf-8"
    )
    sibling = root / "Sibling"
    sibling.mkdir()
    (sibling / "s.ini").write_text(
        "[TextureOverrideS]\nhash = dddd0000\n", encoding="utf-8"
    )
    return root


def test_analyze_scope_mod_matches_fresh_full_analysis(tmp_path):
    root = build_scope_mods(tmp_path)
    data = version_data(
        LADDER_ENTRIES,
        {
            "cccc0000": [HashRef("Anby", "Body", "ib")],
            "dddd0000": [HashRef("Anby", "Body", "blend_vb")],
        },
    )
    tree, _summary = mods.analyze_mods(root, data)
    target = by_name(tree, "Target")
    sibling = by_name(tree, "Sibling")
    pre_target = subtree_versions(target)
    pre_sibling = subtree_versions(sibling)
    pre_sibling_version = sibling.version

    (target.path / "a.ini").write_text(
        "[TextureOverrideA]\nhash = dddd0000\n", encoding="utf-8"
    )

    analyze_scope(target, data)

    target_version = version_of(target)
    assert (target_version.label, target_version.is_latest, target_version.breaks_label) == (
        "2.1",
        False,
        "2.1 → 2.2",
    )
    assert (
        target_version.current_count,
        target_version.outdated_count,
        target_version.unknown_count,
        target_version.total,
        target_version.structural_count,
    ) == (2, 1, 0, 3, 0)

    fresh_tree, _fresh_summary = mods.analyze_mods(root, data)
    fresh_target = by_name(fresh_tree, "Target")

    assert subtree_versions(target) == subtree_versions(fresh_target)
    assert target.variant == fresh_target.variant
    assert subtree_versions(target) != pre_target
    assert sibling.version is pre_sibling_version
    assert subtree_versions(sibling) == pre_sibling


def test_analyze_scope_rejects_root_node(tmp_path):
    root = build_scope_mods(tmp_path)
    tree, _summary = mods.analyze_mods(root, EMPTY_DATA)
    with pytest.raises(ValueError):
        analyze_scope(tree, EMPTY_DATA)


def test_analyze_scope_rejects_file_node(tmp_path):
    root = build_scope_mods(tmp_path)
    tree, _summary = mods.analyze_mods(root, EMPTY_DATA)
    with pytest.raises(ValueError):
        analyze_scope(by_name(tree, "a.ini"), EMPTY_DATA)


def test_analyze_scope_category_reanalyzes_nested_mods(tmp_path):
    root = tmp_path / "mods"
    category = root / "Category"
    (category / "ModA").mkdir(parents=True)
    (category / "ModA" / "a.ini").write_text(
        "[TextureOverrideA]\nhash = 204800aa\n", encoding="utf-8"
    )
    (category / "ModB").mkdir()
    (category / "ModB" / "b.ini").write_text(
        "[TextureOverrideB]\nhash = 102400bb\n", encoding="utf-8"
    )
    outside = root / "Outside"
    outside.mkdir()
    (outside / "o.ini").write_text(
        "[TextureOverrideO]\nhash = 204800aa\n", encoding="utf-8"
    )

    datasets = variant_datasets()
    tree, _summary = mods.analyze_mods(root, datasets)
    cat_node = by_name(tree, "Category")
    mod_a = by_name(tree, "ModA")
    outside_node = by_name(tree, "Outside")
    pre_outside = subtree_versions(outside_node)
    pre_outside_version = outside_node.version

    (mod_a.path / "a.ini").write_text(
        "[TextureOverrideA]\nhash = 102400bb\n", encoding="utf-8"
    )

    analyze_scope(cat_node, datasets)

    fresh_tree, _fresh_summary = mods.analyze_mods(root, datasets)
    fresh_cat = by_name(fresh_tree, "Category")

    assert cat_node.version is None
    assert (mod_a.variant, by_name(tree, "ModB").variant) == ("1024p", "1024p")
    assert version_of(mod_a).label == "2.2"
    assert subtree_versions(cat_node) == subtree_versions(fresh_cat)
    assert outside_node.version is pre_outside_version
    assert subtree_versions(outside_node) == pre_outside


def test_retarget_subtree_paths_rebases_whole_subtree(tmp_path):
    old_dir = tmp_path / "Cool Mod"
    root = mods.ModNode(
        path=old_dir,
        name="Cool Mod",
        kind="mod",
        children=[
            mods.ModNode(
                path=old_dir / "sub",
                name="sub",
                kind="subfolder",
                children=[mods.ModNode(path=old_dir / "sub" / "b.ini", name="b.ini", kind="file")],
            ),
            mods.ModNode(path=old_dir / "a.ini", name="a.ini", kind="file"),
        ],
    )
    new_dir = tmp_path / "DISABLED_Cool Mod"

    retarget_subtree_paths(root, new_dir)

    assert root.path == new_dir
    sub = root.children[0]
    assert sub.path == new_dir / "sub"
    assert sub.children[0].path == new_dir / "sub" / "b.ini"
    assert root.children[1].path == new_dir / "a.ini"
    assert [(child.name, child.kind) for child in root.children] == [
        ("sub", "subfolder"),
        ("a.ini", "file"),
    ]
    assert (root.name, root.kind) == ("Cool Mod", "mod")


def test_analyze_scope_structural_parity(tmp_path):
    db = CharacterDB()
    character = Character(
        name="示例Sample",
        components=[
            Component(
                name="Body-身体",
                fields={"draw_vb": "aa000001", "ib": "bb000001"},
                texture_hashes=[],
            ),
        ],
    )
    db.characters[character.name] = character
    structure = build_structure({"2048p": db, "1024p": db})
    datasets = {
        "2048p": FixerData(chains={}, ib_index_changes={}, db=db, entries=[]),
        "1024p": FixerData(chains={}, ib_index_changes={}, db=db, entries=[]),
    }
    root = tmp_path / "mods"
    mod = root / "SampleMod"
    mod.mkdir(parents=True)
    (mod / "Mod.ini").write_bytes(
        ("\r\n".join(["[TextureOverrideSampleBody]", "hash = aa000001"]) + "\r\n").encode("utf-8")
    )

    tree, _summary = mods.analyze_mods(root, datasets, structure=structure)
    mod_node = tree.children[0]
    assert version_of(mod_node).structural_count == 1

    analyze_scope(mod_node, datasets, structure=structure)

    fresh_tree, _fresh_summary = mods.analyze_mods(root, datasets, structure=structure)
    fresh_node = fresh_tree.children[0]
    assert version_of(mod_node).structural_count == 1
    assert version_of(mod_node.children[0]).structural_count == 1
    assert subtree_versions(mod_node) == subtree_versions(fresh_node)

    (mod_node.path / "Mod.ini").write_bytes(
        ("\r\n".join(["[TextureOverrideSomething]", "hash = ffffffff"]) + "\r\n").encode("utf-8")
    )
    analyze_scope(mod_node, datasets, structure=structure)
    tree3, _summary3 = mods.analyze_mods(root, datasets, structure=structure)

    assert version_of(mod_node).structural_count == 0
    assert subtree_versions(mod_node) == subtree_versions(tree3.children[0])


ROUND_TRIP_ENTRIES = [
    ladder_entry("dd86f5ae", "19ad87f6", 1, "2.7 → 2.8"),
    ladder_entry("19ad87f6", "dd86f5ae", 2, "2.8 → 3.0"),
]


def test_analyze_mods_round_trip_chain_counts_current(tmp_path):
    root = tmp_path / "mods"
    (root / "ModA").mkdir(parents=True)
    (root / "ModA" / "a.ini").write_text(
        "[TextureOverrideA]\nhash = dd86f5ae\n", encoding="utf-8"
    )
    (root / "ModB").mkdir()
    (root / "ModB" / "b.ini").write_text(
        "[TextureOverrideB]\nhash = 19ad87f6\n", encoding="utf-8"
    )

    data = version_data(
        ROUND_TRIP_ENTRIES, {"dd86f5ae": [HashRef("Promeia", "Body", "ib")]}
    )
    tree, summary = mods.analyze_mods(root, data)

    mod_a = by_name(tree, "ModA")
    version = version_of(mod_a)
    assert (version.label, version.is_latest, version.breaks_label) == ("3.0", True, None)
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (1, 0, 0, 1)

    mod_b = by_name(tree, "ModB")
    version = version_of(mod_b)
    assert (version.label, version.is_latest) == (
        "2.8",
        False,
    )
    assert version.breaks_label == "2.8 → 3.0"
    assert (
        version.current_count,
        version.outdated_count,
        version.unknown_count,
        version.total,
    ) == (0, 1, 0, 1)

    assert summary == mods.AnalysisSummary(
        mods=2, current=1, outdated=1, unknown=0, backups_skipped=0, files_scanned=2
    )


SPLINTER_PART_NAMES = (
    "5e717358",
    "6619364f_Body",
    "9821017e_HairEquip",
    "a63028ae_shoulderPads",
    "a7f00ae5",
    "a9188e3c",
    "db111545",
    "e5a92555",
    "f8e6c967",
    "fcac8411",
    "f1c241b7_CapFace",
)

LIVE_MOD_AGGREGATES = {
    "GymZhuYuan": ((25, 6, 1, 32), 1, False),
    "DISABLED_zhu_yan_416": ((54, 0, 1, 55), 11, True),
    "DISABLED_Fallen Angel": ((33, 0, 3, 36), 10, True),
    "Subfolder/DISABLED_Fallen Angel": ((33, 0, 3, 36), 10, True),
    "Subfolder/DISABLED_Zhu Yuan Fashionwear": ((16, 0, 1, 17), 5, True),
    "Promeia-ADO": ((3, 4, 0, 7), 1, False),
    "honeyorchiid_s_promeia_vanilla": ((21, 0, 0, 21), 2, True),
}

EXPECTED_LIVE_SUMMARY = mods.AnalysisSummary(
    mods=7, current=5, outdated=2, unknown=0, backups_skipped=0, files_scanned=40
)


@pytest.mark.skipif(
    not repo.default_cache_dir().exists() or not REAL_MODS_DIR.exists(),
    reason="live ZZZ-Model-Hash data or user mods folder not present",
)
def test_live_mods_smoke():
    data = load_fixer_data(repo.default_cache_dir())
    tree, summary = mods.analyze_mods(REAL_MODS_DIR, data)
    nodes = list(walk_nodes(tree))

    problems = []

    if summary != EXPECTED_LIVE_SUMMARY:
        problems.append(f"summary drifted: {summary}")

    for rel_path, (counts, ini_count, is_latest) in LIVE_MOD_AGGREGATES.items():
        matches = [
            node
            for node in nodes
            if node.kind == "mod"
            and node.path.relative_to(REAL_MODS_DIR).as_posix() == rel_path
        ]
        if len(matches) != 1:
            problems.append(
                f"expected exactly one {rel_path!r} mod node, got {len(matches)}"
            )
            continue
        node = matches[0]
        version = node.version
        if version is None:
            problems.append(f"{rel_path} has no version")
        elif (
            version.current_count,
            version.outdated_count,
            version.unknown_count,
            version.total,
        ) != counts or version.is_latest != is_latest:
            problems.append(f"{rel_path} version drifted: {version}")
        ini_nodes = [sub for sub in walk_nodes(node) if sub.kind == "file"]
        if len(ini_nodes) != ini_count:
            problems.append(
                f"{rel_path} included ini count drifted: {len(ini_nodes)} != {ini_count}"
            )
        if node.disabled != node.name.startswith("DISABLED"):
            problems.append(f"{rel_path} disabled flag drifted: {node.disabled}")

    zhu = [node for node in nodes if node.name == "DISABLED_zhu_yan_416"]
    if len(zhu) != 1 or zhu[0].kind != "mod" or not zhu[0].disabled:
        problems.append("DISABLED_zhu_yan_416 is not exactly one disabled mod node")
    else:
        dir_children = [child for child in zhu[0].children if child.kind != "file"]
        if len(dir_children) != 11:
            problems.append(
                f"DISABLED_zhu_yan_416 directory children drifted: "
                f"{len(dir_children)} != 11"
            )

    splinters = [
        node
        for node in nodes
        if node.kind == "mod" and node.name in SPLINTER_PART_NAMES
    ]
    if splinters:
        problems.append(
            f"splinter part mods reappeared: {sorted(node.name for node in splinters)}"
        )

    backup_nodes = [
        node for node in nodes if node.name.startswith("DISABLED_BACKUP_")
    ]
    if backup_nodes:
        problems.append(
            f"DISABLED_BACKUP_ nodes present: {[node.name for node in backup_nodes]}"
        )

    if problems:
        pytest.skip("live data drift: " + "; ".join(problems))


def variant_datasets():
    """2048p/1024p FixerData pair: per-variant exclusive hash + shared mesh."""
    shared = {"cccc0000": [HashRef("Anby", "Body", "blend_vb")]}
    return {
        "2048p": version_data(
            LADDER_ENTRIES,
            {"204800aa": [HashRef("Anby", "Body", "ib")], **shared},
        ),
        "1024p": version_data(
            LADDER_ENTRIES,
            {"102400bb": [HashRef("Anby", "Body", "ib")], **shared},
        ),
    }


def build_variant_mods(base: Path) -> Path:
    """Mods folder with one mod per detection outcome (A/B exclusive, C unknown)."""
    root = base / "mods"
    (root / "ModA").mkdir(parents=True)
    (root / "ModA" / "a.ini").write_text(
        "[TextureOverrideA]\nhash = 204800aa\n", encoding="utf-8"
    )
    (root / "ModB").mkdir()
    (root / "ModB" / "b.ini").write_text(
        "[TextureOverrideB]\nhash = 102400bb\n", encoding="utf-8"
    )
    (root / "ModC").mkdir()
    (root / "ModC" / "c.ini").write_text(
        "[TextureOverrideC]\nhash = deadbeef\n", encoding="utf-8"
    )
    return root


def test_analyze_mods_detects_variants_per_mod(tmp_path):
    root = build_variant_mods(tmp_path)
    tree, summary = mods.analyze_mods(root, variant_datasets())

    mod_a = by_name(tree, "ModA")
    mod_b = by_name(tree, "ModB")
    mod_c = by_name(tree, "ModC")
    assert (mod_a.variant, mod_b.variant, mod_c.variant) == ("2048p", "1024p", None)
    assert summary.variants == {"2048p": 1, "1024p": 1, "unknown": 1}
    mod_c_version = version_of(mod_c)
    assert (mod_c_version.label, mod_c_version.unknown_count, mod_c_version.total) == (
        "unknown",
        1,
        1,
    )


def test_analyze_mods_variant_inherited_by_files(tmp_path):
    root = tmp_path / "mods"
    (root / "ModB" / "deep").mkdir(parents=True)
    (root / "ModB" / "b.ini").write_text(
        "[TextureOverrideB]\nhash = 102400bb\n", encoding="utf-8"
    )
    (root / "ModB" / "deep" / "d.ini").write_text(
        "[TextureOverrideD]\nhash = cccc0000\n", encoding="utf-8"
    )
    tree, summary = mods.analyze_mods(root, variant_datasets())

    mod_b = by_name(tree, "ModB")
    deep = by_name(tree, "deep")
    b_file = by_name(tree, "b.ini")
    d_file = by_name(tree, "d.ini")
    assert (mod_b.variant, deep.variant, b_file.variant, d_file.variant) == (
        "1024p",
        "1024p",
        "1024p",
        "1024p",
    )
    b_file_version = version_of(b_file)
    d_file_version = version_of(d_file)
    assert (b_file_version.label, b_file_version.is_latest) == ("2.2", True)
    assert (d_file_version.label, d_file_version.is_latest) == ("2.2", True)
    assert summary.variants == {"1024p": 1}
    assert summary == mods.AnalysisSummary(
        mods=1, current=1, outdated=0, unknown=0, backups_skipped=0, files_scanned=2
    )


def test_analyze_mods_single_data_backward_compatible(tmp_path):
    root = build_variant_mods(tmp_path)
    tree, summary = mods.analyze_mods(root, variant_datasets()["2048p"])

    mod_a = by_name(tree, "ModA")
    mod_b = by_name(tree, "ModB")
    mod_c = by_name(tree, "ModC")
    assert (mod_a.variant, mod_b.variant, mod_c.variant) == ("2048p", None, None)
    assert summary.variants == {"2048p": 1, "unknown": 2}
    assert summary == mods.AnalysisSummary(
        mods=3, current=1, outdated=0, unknown=2, backups_skipped=0, files_scanned=3
    )


def make_node(name, kind, version=None, children=()):
    """Hand-built ModNode with an optional version (paths are opaque dummies)."""
    return mods.ModNode(
        path=Path(name), name=name, kind=kind, children=list(children), version=version
    )


def version_counts(outdated=0, current=0, unknown=0):
    """ModVersion carrying only the given hash counts (labels are dummies)."""
    return mods.ModVersion(
        current_count=current,
        outdated_count=outdated,
        unknown_count=unknown,
        total=outdated + current + unknown,
    )


def test_aggregate_updates_nested_outdated():
    category = make_node(
        "Category",
        "category",
        children=[
            make_node("Good", "mod", version=version_counts(current=2)),
            make_node("Stale", "mod", version=version_counts(outdated=1, current=1)),
        ],
    )
    assert mods.aggregate_updates(category) == "updates available"

    subfolder = make_node(
        "body",
        "subfolder",
        children=[make_node("deep.ini", "file", version=version_counts(outdated=1))],
    )
    nested = make_node(
        "Category",
        "category",
        children=[
            make_node("Good", "mod", version=version_counts(current=2)),
            subfolder,
        ],
    )
    assert mods.aggregate_updates(nested) == "updates available"


def test_aggregate_updates_nested_current_with_unknown():
    category = make_node(
        "Category",
        "category",
        children=[
            make_node("Ghost", "mod", version=version_counts(unknown=1)),
            make_node("Good", "mod", version=version_counts(current=1)),
        ],
    )
    assert mods.aggregate_updates(category) == "up to date"


def test_aggregate_updates_nothing_classifiable():
    category = make_node(
        "Category",
        "category",
        children=[
            make_node("GhostA", "mod", version=version_counts(unknown=2)),
            make_node("GhostB", "mod", version=version_counts(unknown=1)),
        ],
    )
    assert mods.aggregate_updates(category) == ""
    assert mods.aggregate_updates(make_node("Empty", "category")) == ""


def test_aggregate_updates_transitive_through_versionless_dirs():
    current = make_node(
        "Outer",
        "category",
        children=[
            make_node(
                "Inner",
                "category",
                children=[make_node("Good", "mod", version=version_counts(current=1))],
            )
        ],
    )
    assert mods.aggregate_updates(current) == "up to date"
    outdated = make_node(
        "Outer",
        "category",
        children=[
            make_node(
                "Inner",
                "category",
                children=[make_node("Stale", "mod", version=version_counts(outdated=1))],
            )
        ],
    )
    assert mods.aggregate_updates(outdated) == "updates available"


def test_aggregate_updates_structural_only_subtree():
    category = make_node(
        "Category",
        "category",
        children=[
            make_node("Good", "mod", version=version_counts(current=2)),
            make_node(
                "Structural",
                "mod",
                version=ModVersion(current_count=1, total=1, structural_count=2),
            ),
        ],
    )
    assert mods.aggregate_updates(category) == "structural"


def test_aggregate_updates_outdated_beats_structural():
    category = make_node(
        "Category",
        "category",
        children=[
            make_node(
                "Structural",
                "mod",
                version=ModVersion(current_count=1, total=1, structural_count=1),
            ),
            make_node("Stale", "mod", version=version_counts(outdated=1)),
        ],
    )
    assert mods.aggregate_updates(category) == "updates available"


def test_analyze_structural_breakage_counts(tmp_path):
    db = CharacterDB()
    character = Character(
        name="示例Sample",
        components=[
            Component(
                name="Body-身体",
                fields={"draw_vb": "aa000001", "ib": "bb000001"},
                texture_hashes=[],
            ),
        ],
    )
    db.characters[character.name] = character
    structure = build_structure({"2048p": db, "1024p": db})
    datasets = {
        "2048p": FixerData(chains={}, ib_index_changes={}, db=db, entries=[]),
        "1024p": FixerData(chains={}, ib_index_changes={}, db=db, entries=[]),
    }
    mod = tmp_path / "SampleMod"
    mod.mkdir()
    (mod / "Mod.ini").write_bytes(
        ("\r\n".join(["[TextureOverrideSampleBody]", "hash = aa000001"]) + "\r\n").encode("utf-8")
    )

    tree, summary = analyze_mods(tmp_path, datasets, structure=structure)
    mod_node = tree.children[0]
    assert (mod_node.kind, mod_node.name) == ("mod", "SampleMod")
    mod_node_version = version_of(mod_node)
    assert mod_node_version.structural_count == 1
    assert mod_node_version.outdated_count == 0
    assert aggregate_updates(tree) == "structural"
    file_node = mod_node.children[0]
    assert (file_node.kind, version_of(file_node).structural_count) == ("file", 1)
    assert summary.structural == 1

    tree2, summary2 = analyze_mods(tmp_path, datasets)
    assert version_of(tree2.children[0]).structural_count == 0
    assert summary2.structural == 0

    other = tmp_path / "UnknownMod"
    other.mkdir()
    (other / "Other.ini").write_bytes(
        ("\r\n".join(["[TextureOverrideSomething]", "hash = ffffffff"]) + "\r\n").encode("utf-8")
    )
    tree3, summary3 = analyze_mods(tmp_path, datasets, structure=structure)
    unknown_node = [child for child in tree3.children if child.name == "UnknownMod"][0]
    assert version_of(unknown_node).structural_count == 0
    assert summary3.structural == 1


def test_updates_text_fires_for_structural_only():
    version = ModVersion(current_count=2, total=2, structural_count=1)
    assert updates_text(version, as_mod=True) == "structural"
    assert updates_text(ModVersion(current_count=2, total=2), as_mod=True) == "up to date"
    assert updates_color(version, as_mod=True) == QColor("#808080")
    assert "1 structural" in hashes_text(version, as_mod=True)


def test_updates_text_outdated_beats_structural():
    version = ModVersion(current_count=1, outdated_count=2, total=3, structural_count=1)
    assert updates_text(version, as_mod=True) == "updates available"
    assert updates_color(version, as_mod=True) == QColor("#c47f00")


def test_updates_text_structural_file_row():
    version = ModVersion(current_count=1, total=1, structural_count=1)
    assert updates_text(version, as_mod=False) == "structural"
    assert updates_text(ModVersion(), as_mod=False) == ""
