"""Tests for tools.presets on tmp_path JSONs (never the real project root)."""

from pathlib import Path

import pytest

from tools import presets
from tools.mods import ModNode


def build_tree(base: Path) -> tuple[ModNode, ModNode, ModNode]:
    """Hand-built tree: category with toggling mods, subfolder with a file, steady mod."""
    fashion = ModNode(
        path=base / "Cat" / "Fashion", name="Fashion", kind="mod", disabled=False
    )
    disabled_old = ModNode(
        path=base / "Cat" / "DISABLED_Old",
        name="DISABLED_Old",
        kind="mod",
        disabled=True,
    )
    assets = ModNode(
        path=base / "Cat" / "Assets",
        name="Assets",
        kind="subfolder",
        children=[
            ModNode(path=base / "Cat" / "Assets" / "a.ini", name="a.ini", kind="file")
        ],
    )
    cat = ModNode(
        path=base / "Cat",
        name="Cat",
        kind="category",
        children=[fashion, disabled_old, assets],
    )
    stays = ModNode(path=base / "Stays", name="Stays", kind="mod", disabled=False)
    root = ModNode(path=base, name="mods", kind="root", children=[cat, stays])
    return root, fashion, disabled_old


def test_save_and_load_round_trip(tmp_path):
    presets.save_preset(
        "Casual",
        ["Cat/Fashion", "Stays", "Cat/DISABLED_Old", "Cat/Fashion"],
        tmp_path,
    )
    assert presets.load_presets(tmp_path) == {
        "Casual": ["Cat/DISABLED_Old", "Cat/Fashion", "Stays"]
    }
    assert presets.preset_names(tmp_path) == ["Casual"]
    assert presets.presets_path(tmp_path).read_bytes().endswith(b"\n")


def test_save_preset_overwrites_existing_name(tmp_path):
    presets.save_preset("Casual", ["Cat/Fashion"], tmp_path)
    presets.save_preset("Casual", ["Stays"], tmp_path)
    assert presets.load_presets(tmp_path) == {"Casual": ["Stays"]}
    assert presets.preset_names(tmp_path) == ["Casual"]


def test_save_preset_blank_name_raises_and_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        presets.save_preset("   ", ["Cat/Fashion"], tmp_path)
    with pytest.raises(ValueError):
        presets.save_preset("", ["Cat/Fashion"], tmp_path)
    assert not presets.presets_path(tmp_path).exists()


def test_delete_preset_true_then_false(tmp_path):
    presets.save_preset("Casual", ["Cat/Fashion"], tmp_path)
    assert presets.delete_preset("Casual", tmp_path)
    assert presets.load_presets(tmp_path) == {}
    assert presets.presets_path(tmp_path).exists()
    assert not presets.delete_preset("Casual", tmp_path)
    assert not presets.delete_preset("   ", tmp_path)


def test_rename_preset_success_and_existing_target(tmp_path):
    presets.save_preset("Casual", ["Cat/Fashion"], tmp_path)
    assert presets.rename_preset("Casual", "Pro", tmp_path)
    assert presets.load_presets(tmp_path) == {"Pro": ["Cat/Fashion"]}
    assert presets.preset_names(tmp_path) == ["Pro"]
    presets.save_preset("Other", ["Stays"], tmp_path)
    assert not presets.rename_preset("Pro", "Other", tmp_path)
    assert presets.load_presets(tmp_path) == {
        "Pro": ["Cat/Fashion"],
        "Other": ["Stays"],
    }


def test_preset_names_sorted(tmp_path):
    presets.save_preset("Beta", ["Cat/Fashion"], tmp_path)
    presets.save_preset("Alpha", ["Stays"], tmp_path)
    presets.save_preset("Gamma", [], tmp_path)
    assert presets.preset_names(tmp_path) == ["Alpha", "Beta", "Gamma"]


def test_load_presets_returns_empty_on_bad_files(tmp_path):
    assert presets.load_presets(tmp_path) == {}
    presets.presets_path(tmp_path).write_text("", encoding="utf-8")
    assert presets.load_presets(tmp_path) == {}
    presets.presets_path(tmp_path).write_text("{oops", encoding="utf-8")
    assert presets.load_presets(tmp_path) == {}
    presets.presets_path(tmp_path).write_text("[1, 2]", encoding="utf-8")
    assert presets.load_presets(tmp_path) == {}


def test_load_presets_drops_non_string_entries_and_non_list_values(tmp_path):
    presets.presets_path(tmp_path).write_text(
        '{"Casual": ["Cat/Fashion", 1, ["nested"], "Stays"], "Broken": "no"}',
        encoding="utf-8",
    )
    assert presets.load_presets(tmp_path) == {"Casual": ["Cat/Fashion", "Stays"]}


def test_preset_changes_dfs_order(tmp_path):
    root, fashion, disabled_old = build_tree(tmp_path)
    enabled = frozenset({"Cat/Old", "Stays"})
    assert presets.preset_changes(root, tmp_path, enabled) == [
        (fashion, False),
        (disabled_old, True),
    ]


def test_missing_preset_paths(tmp_path):
    root, _, _ = build_tree(tmp_path)
    enabled = frozenset({"Cat/Fashion", "Stays", "Ghost/Removed", "Aaa"})
    assert presets.missing_preset_paths(root, tmp_path, enabled) == [
        "Aaa",
        "Ghost/Removed",
    ]


def test_enabled_relative_paths(tmp_path):
    root, _, _ = build_tree(tmp_path)
    outside = ModNode(
        path=tmp_path.parent / "Elsewhere" / "Out",
        name="Out",
        kind="mod",
        disabled=False,
    )
    root.children.append(outside)
    result = presets.enabled_relative_paths(root, tmp_path)
    assert isinstance(result, frozenset)
    assert result == frozenset(
        {
            "Cat/Fashion",
            "Stays",
            (tmp_path.parent / "Elsewhere" / "Out").as_posix(),
        }
    )


def test_clean_preset_enables_prefixed_mod(tmp_path):
    root, _, disabled_old = build_tree(tmp_path)
    enabled = frozenset({"Cat/Old", "Cat/Fashion", "Stays"})
    assert presets.preset_changes(root, tmp_path, enabled) == [(disabled_old, True)]


def test_prefixed_mod_not_missing_with_clean_name(tmp_path):
    root, _, _ = build_tree(tmp_path)
    enabled = frozenset({"Cat/Old", "Stays"})
    assert presets.missing_preset_paths(root, tmp_path, enabled) == []


def test_swap_between_clean_presets(tmp_path):
    base = tmp_path
    offset = base / "Claret"
    shorts = ModNode(
        path=offset / "DISABLED_Claret - Shorts & Subtle Edit",
        name="DISABLED_Claret - Shorts & Subtle Edit",
        kind="mod",
        disabled=True,
    )
    nsfw = ModNode(
        path=offset / "DISABLED_Claret Original model NSFW",
        name="DISABLED_Claret Original model NSFW",
        kind="mod",
        disabled=True,
    )
    claret = ModNode(
        path=offset, name="Claret", kind="category", children=[shorts, nsfw]
    )
    root = ModNode(path=base, name="mods", kind="root", children=[claret])
    preset1 = frozenset({"Claret/Claret - Shorts & Subtle Edit"})
    preset2 = frozenset({"Claret/Claret Original model NSFW"})
    assert presets.preset_changes(root, base, preset1) == [(shorts, True)]
    assert presets.preset_changes(root, base, preset2) == [(nsfw, True)]
    shorts_on = ModNode(
        path=offset / "Claret - Shorts & Subtle Edit",
        name="Claret - Shorts & Subtle Edit",
        kind="mod",
        disabled=False,
    )
    root2 = ModNode(
        path=base, name="mods", kind="root",
        children=[ModNode(path=offset, name="Claret", kind="category", children=[shorts_on, nsfw])],
    )
    assert presets.preset_changes(root2, base, preset2) == [
        (shorts_on, False),
        (nsfw, True),
    ]
    assert presets.preset_changes(root2, base, preset1) == []


def test_retarget_preset_paths_rewrites_exact_entry(tmp_path):
    presets.save_preset("x", ["Old", "Cat/Old2"], tmp_path)

    assert presets.retarget_preset_paths([("Old", "New")], tmp_path) == 1

    assert presets.load_presets(tmp_path) == {"x": ["Cat/Old2", "New"]}


def test_retarget_preset_paths_rewrites_category_prefix(tmp_path):
    presets.save_preset("x", ["Cat/Old", "Cat/A/B", "Other"], tmp_path)

    assert presets.retarget_preset_paths([("Cat", "Pets")], tmp_path) == 2

    assert presets.load_presets(tmp_path) == {"x": ["Other", "Pets/A/B", "Pets/Old"]}


def test_retarget_preset_paths_keeps_nested_and_root_prefixes_isolated(tmp_path):
    presets.save_preset("x", ["A/Cat", "A/Cat/Mod", "Cat/Mod"], tmp_path)

    assert presets.retarget_preset_paths([("A/Cat", "A/Pets")], tmp_path) == 2
    assert presets.load_presets(tmp_path) == {"x": ["A/Pets", "A/Pets/Mod", "Cat/Mod"]}

    assert presets.retarget_preset_paths([("Cat", "Dogs")], tmp_path) == 1
    assert presets.load_presets(tmp_path) == {"x": ["A/Pets", "A/Pets/Mod", "Dogs/Mod"]}


def test_retarget_preset_paths_no_match_leaves_file_untouched(tmp_path):
    presets.save_preset("x", ["Cat/Old"], tmp_path)
    raw = presets.presets_path(tmp_path).read_bytes()

    assert presets.retarget_preset_paths([("Ghost", "New")], tmp_path) == 0

    assert presets.presets_path(tmp_path).read_bytes() == raw


def test_retarget_preset_paths_skips_blank_and_identical_swaps(tmp_path):
    presets.save_preset("x", ["Old"], tmp_path)
    raw = presets.presets_path(tmp_path).read_bytes()

    assert presets.retarget_preset_paths([("", "New")], tmp_path) == 0
    assert presets.retarget_preset_paths([("   ", "New")], tmp_path) == 0
    assert presets.retarget_preset_paths([("Old", "Old")], tmp_path) == 0
    assert presets.presets_path(tmp_path).read_bytes() == raw


def test_retarget_preset_paths_missing_file_writes_nothing(tmp_path):
    assert presets.retarget_preset_paths([("Old", "New")], tmp_path) == 0
    assert not presets.presets_path(tmp_path).exists()


def test_remove_preset_paths_exact_entry(tmp_path):
    presets.save_preset("x", ["Old", "Cat/Old2"], tmp_path)

    assert presets.remove_preset_paths(["Old"], tmp_path) == 1

    assert presets.load_presets(tmp_path) == {"x": ["Cat/Old2"]}


def test_remove_preset_paths_category_prefix(tmp_path):
    presets.save_preset("x", ["Cat/Old", "Cat/A/B", "Other"], tmp_path)

    assert presets.remove_preset_paths(["Cat"], tmp_path) == 2

    assert presets.load_presets(tmp_path) == {"x": ["Other"]}


def test_remove_preset_paths_no_match_leaves_file_untouched(tmp_path):
    presets.save_preset("x", ["Cat/Old"], tmp_path)
    raw = presets.presets_path(tmp_path).read_bytes()

    assert presets.remove_preset_paths(["Ghost"], tmp_path) == 0

    assert presets.presets_path(tmp_path).read_bytes() == raw


def test_remove_preset_paths_skips_blank_paths(tmp_path):
    presets.save_preset("x", ["Cat/Old"], tmp_path)
    raw = presets.presets_path(tmp_path).read_bytes()

    assert presets.remove_preset_paths(["", "  "], tmp_path) == 0

    assert presets.presets_path(tmp_path).read_bytes() == raw


def test_remove_preset_paths_missing_file_writes_nothing(tmp_path):
    assert presets.remove_preset_paths(["Old"], tmp_path) == 0

    assert not presets.presets_path(tmp_path).exists()


def test_remove_preset_paths_prunes_emptied_preset(tmp_path):
    presets.save_preset("solo", ["Old"], tmp_path)

    assert presets.remove_preset_paths(["Old"], tmp_path) == 1

    assert presets.load_presets(tmp_path) == {}
    assert "solo" not in presets.presets_path(tmp_path).read_text(encoding="utf-8")


def test_remove_preset_paths_keeps_preexisting_empty_preset(tmp_path):
    presets.save_preset("empty", [], tmp_path)
    presets.save_preset("x", ["Other"], tmp_path)
    raw = presets.presets_path(tmp_path).read_bytes()

    assert presets.remove_preset_paths(["Old"], tmp_path) == 0

    assert presets.load_presets(tmp_path) == {"empty": [], "x": ["Other"]}
    assert presets.presets_path(tmp_path).read_bytes() == raw