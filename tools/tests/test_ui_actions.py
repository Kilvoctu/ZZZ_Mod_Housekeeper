"""Tests for module-level helpers in tools.ui.main_window:
`_scope_has_backups`, which answers whether the fix-backup store holds any
backup for a file or directory scope; `_enabled_view_includes`, which
answers whether a ModNode row survives the Show-enabled-only tree filter;
and `_stale_mod_refs`, which answers which mod references went stale after
a rescan (vanished mods plus dangling promoted/preset entries), fed by
`presets.mod_relative_paths`.

Headless-safe: importing the module pulls in PySide6 but creates no widgets
and needs no QApplication; only backup file names and store structure matter
to `_scope_has_backups` (file contents are never parsed), and
`_enabled_view_includes`, `_stale_mod_refs`, and `mod_relative_paths` run on
plain ModNode dataclasses.
"""

from pathlib import Path

from tools import presets
from tools.backups import store_folder_for_mod
from tools.mods import ModNode
from tools.ui import main_window


def add_mod_file(folder: Path, name: str) -> Path:
    """Placeholder live mod file; only the name matters to the helper."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\x00")
    return path


def add_backup(folder: Path, live_name: str) -> Path:
    """One new-scheme store backup of `live_name` inside the given folder."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{live_name} -- 2024-01-01 12.00.00.bak"
    path.write_bytes(b"\x00")
    return path


def make_node(kind, name, disabled=False, children=()):
    """Synthetic ModNode; the filter helper only reads kind, disabled, children."""
    return ModNode(
        path=Path(name), name=name, kind=kind, disabled=disabled, children=list(children)
    )


def test_scope_has_backups_without_store_is_false(tmp_path):
    """With no store directory at all, neither a directory scope nor a file
    scope finds any backup (the scoped file need not exist on disk either)."""
    mods = tmp_path / "mods"
    mod = mods / "mod"
    mod.mkdir(parents=True)
    store = tmp_path / "store"

    assert not main_window._scope_has_backups(mods, store, mod)
    assert not main_window._scope_has_backups(mods, store, mod / "char.ini")


def test_scope_has_backups_dir_scope_with_mirror_backup_is_true(tmp_path):
    """A directory scope counts once its store mirror folder holds anything:
    an empty mirror is False, one backup file inside flips it to True."""
    mods = tmp_path / "mods"
    mod = mods / "mod"
    add_mod_file(mod, "char.ini")
    store = tmp_path / "store"

    mirror = store_folder_for_mod(store, mods, mod)
    mirror.mkdir(parents=True)
    assert not main_window._scope_has_backups(mods, store, mod)

    add_backup(mirror, "char.ini")
    assert main_window._scope_has_backups(mods, store, mod)


def test_scope_has_backups_file_scope_matches_only_same_name(tmp_path):
    """A file scope checks chains for exactly that file: a backup of another
    name in the same canonical scan dir does not count, same names do."""
    mods = tmp_path / "mods"
    mod = mods / "mod"
    ini = add_mod_file(mod, "char.ini")
    store = tmp_path / "store"

    scan_dir = store_folder_for_mod(store, mods, ini.parent)
    add_backup(scan_dir, "other.ini")
    assert not main_window._scope_has_backups(mods, store, ini)

    add_backup(scan_dir, "char.ini")
    assert main_window._scope_has_backups(mods, store, ini)


def test_scope_has_backups_dir_scope_nested_backup_is_true(tmp_path):
    """The directory scope's non-empty mirror check is recursive: a backup
    buried in a nested store subfolder still counts for the mod dir scope."""
    mods = tmp_path / "mods"
    mod = mods / "mod"
    mod.mkdir(parents=True)
    store = tmp_path / "store"

    mirror = store_folder_for_mod(store, mods, mod)
    add_backup(mirror / "sub", "char.ini")
    assert main_window._scope_has_backups(mods, store, mod)


def test_enabled_view_excludes_disabled_mod():
    """Show enabled only hides a disabled mod row outright."""
    assert not main_window._enabled_view_includes(
        make_node("mod", "DISABLED_Anby", disabled=True)
    )


def test_enabled_view_includes_enabled_mod():
    """An enabled mod row always survives the filter."""
    assert main_window._enabled_view_includes(make_node("mod", "Anby"))


def test_enabled_view_includes_file():
    """File rows inside visible mods always show, disabled flag or not."""
    assert main_window._enabled_view_includes(
        make_node("file", "char.ini", disabled=True)
    )


def test_enabled_view_includes_subfolder_with_file_child():
    """A subfolder shows when it still holds a file child."""
    subfolder = make_node("subfolder", "body", children=[make_node("file", "m.ini")])
    assert main_window._enabled_view_includes(subfolder)


def test_enabled_view_excludes_category_with_only_disabled_mods():
    """A category whose only mods are disabled disappears from the view."""
    category = make_node(
        "category", "Pack", children=[make_node("mod", "DISABLED_Anby", disabled=True)]
    )
    assert not main_window._enabled_view_includes(category)


def test_enabled_view_includes_mixed_category():
    """A category with any enabled mod (beside disabled ones) keeps showing."""
    category = make_node(
        "category",
        "Pack",
        children=[
            make_node("mod", "DISABLED_Anby", disabled=True),
            make_node("mod", "Anby"),
        ],
    )
    assert main_window._enabled_view_includes(category)


def test_enabled_view_excludes_childless_category():
    """A childless (empty) category row has nothing visible and is hidden."""
    assert not main_window._enabled_view_includes(make_node("category", "Pack"))


def test_stale_mod_refs_vanished_mod():
    """A mod present in the previous scan but gone from the current one is stale."""
    assert main_window._stale_mod_refs(
        frozenset({"Cat/A", "Cat/Gone"}),
        frozenset({"Cat/A"}),
        {},
        {},
        set(),
    ) == ["Cat/Gone"]


def test_stale_mod_refs_dangling_promoted():
    """A promoted-image key no longer in the folder is stale even when the
    scan itself lost nothing."""
    assert main_window._stale_mod_refs(
        frozenset({"Cat/A"}),
        frozenset({"Cat/A"}),
        {"Cat/Dangling": "p.jpg"},
        {},
        set(),
    ) == ["Cat/Dangling"]


def test_stale_mod_refs_dangling_preset():
    """A preset entry pointing outside the current scan is stale."""
    assert main_window._stale_mod_refs(
        frozenset({"Cat/A"}),
        frozenset({"Cat/A"}),
        {},
        {"p1": ["Cat/Old"]},
        set(),
    ) == ["Cat/Old"]


def test_stale_mod_refs_dismissed_filtered():
    """A dangling ref the user already dismissed is not flagged again."""
    assert main_window._stale_mod_refs(
        frozenset({"Cat/A"}),
        frozenset({"Cat/A"}),
        {"Cat/X": "x.jpg"},
        {},
        {"Cat/X"},
    ) == []


def test_stale_mod_refs_union_sorted():
    """Vanished mods and dangling refs merge into one deduplicated, sorted
    list: Cat/A both vanishes and dangles but shows up once."""
    assert main_window._stale_mod_refs(
        frozenset({"Cat/A", "Cat/B"}),
        frozenset({"Cat/C"}),
        {"Cat/A": "a.jpg"},
        {},
        set(),
    ) == ["Cat/A", "Cat/B"]


def test_stale_mod_refs_self_caused_removal_not_flagged():
    """A mod the app itself deleted or renamed (guard set) never prompts."""
    assert main_window._stale_mod_refs(
        frozenset({"Cat/A", "Cat/Gone"}),
        frozenset({"Cat/A"}),
        {"Cat/Gone": "p.jpg"},
        {},
        {"Cat/Gone"},
    ) == []


def test_stale_mod_refs_present_refs_not_flagged():
    """Promoted and preset refs to mods still on disk stay silent."""
    assert main_window._stale_mod_refs(
        frozenset({"Cat/A"}),
        frozenset({"Cat/A"}),
        {"Cat/A": "a.jpg"},
        {"p1": ["Cat/A"]},
        set(),
    ) == []


def test_mod_relative_paths_canonical(tmp_path):
    """mod_relative_paths returns every mod's relative posix path with the
    DISABLED_ leaf prefix stripped: the disabled Cat/DISABLED_B counts as
    Cat/B, and the nested Pack/C keeps its category prefix. Only node paths
    matter; parents need not mirror them."""
    base = tmp_path / "mods"
    root = ModNode(
        path=base,
        name="mods",
        kind="category",
        children=[
            ModNode(
                path=base / "Cat" / "A", name="A", kind="mod"
            ),
            ModNode(
                path=base / "Cat" / "DISABLED_B",
                name="DISABLED_B",
                kind="mod",
                disabled=True,
            ),
            ModNode(
                path=base / "Pack",
                name="Pack",
                kind="category",
                children=[ModNode(path=base / "Pack" / "C", name="C", kind="mod")],
            ),
        ],
    )
    assert presets.mod_relative_paths(root, base) == {"Cat/A", "Cat/B", "Pack/C"}