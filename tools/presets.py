"""Named snapshots of enabled mods, stored in state.json.

A preset maps a name to the relative posix paths of the mod folders it
considers enabled; the GUI later applies one by toggling those folders.
"""

from collections.abc import Iterable
from pathlib import Path

from .mods import ModNode
from .state import load_state, save_state

_DISABLED_PREFIX = "DISABLED_"


def load_presets(root: Path | None = None) -> dict[str, list[str]]:
    """Read {name: sorted enabled paths} from state.json; {} on missing, undecodable or non-dict JSON."""
    presets: dict[str, list[str]] = {}
    for name, value in load_state(root)["presets"].items():
        if isinstance(name, str) and isinstance(value, list):
            presets[name] = sorted(entry for entry in value if isinstance(entry, str))
    return presets


def save_preset(
    name: str, enabled_relative: Iterable[str], root: Path | None = None
) -> None:
    """Store name.strip() -> sorted de-duplicated enabled paths, overwriting."""
    stripped = name.strip()
    if not stripped:
        raise ValueError(f"preset name is blank: {name!r}")
    presets = load_presets(root)
    presets[stripped] = sorted({path for path in enabled_relative})
    _write(presets, root)


def delete_preset(name: str, root: Path | None = None) -> bool:
    """Remove one preset; True when it existed."""
    stripped = name.strip()
    presets = load_presets(root)
    if stripped not in presets:
        return False
    del presets[stripped]
    _write(presets, root)
    return True


def rename_preset(old: str, new: str, root: Path | None = None) -> bool:
    """Rename one preset; False when old is missing, new is blank or taken."""
    stripped_old = old.strip()
    stripped_new = new.strip()
    if not stripped_new:
        return False
    presets = load_presets(root)
    if stripped_old not in presets or stripped_new in presets:
        return False
    presets[stripped_new] = presets.pop(stripped_old)
    _write(presets, root)
    return True


def preset_names(root: Path | None = None) -> list[str]:
    """Sorted preset names."""
    return sorted(load_presets(root))


def _clean_disabled_leaf(relative: str) -> str:
    """Strip a leading DISABLED_ prefix from the last path component."""
    slash = relative.rfind("/")
    if slash >= 0:
        head, leaf = relative[: slash + 1], relative[slash + 1 :]
        if leaf.startswith(_DISABLED_PREFIX):
            return head + leaf[len(_DISABLED_PREFIX) :]
        return relative
    if relative.startswith(_DISABLED_PREFIX):
        return relative[len(_DISABLED_PREFIX) :]
    return relative


def _node_relative(node: ModNode, root: Path) -> str:
    """Relative posix path of a mod node with its DISABLED_ leaf prefix stripped."""
    try:
        relative = node.path.relative_to(root).as_posix()
    except ValueError:
        relative = node.path.as_posix()
    return _clean_disabled_leaf(relative)


def preset_changes(
    tree: ModNode, root: Path, enabled_relative: frozenset[str]
) -> list[tuple[ModNode, bool]]:
    """(node, desired) pairs for mod nodes the preset toggles, depth-first.

    Paths outside root fall back to their absolute posix form.
    """
    changes: list[tuple[ModNode, bool]] = []
    for node in _walk(tree):
        if node.kind != "mod":
            continue
        relative = _node_relative(node, root)
        desired = relative in enabled_relative
        if desired != (not node.disabled):
            changes.append((node, desired))
    return changes


def missing_preset_paths(
    tree: ModNode, root: Path, enabled_relative: frozenset[str]
) -> list[str]:
    """Sorted preset paths no mod node in the tree matches."""
    present: set[str] = set()
    for node in _walk(tree):
        if node.kind != "mod":
            continue
        present.add(_node_relative(node, root))
    return sorted(enabled_relative - present)


def retarget_preset_paths(
    swaps: Iterable[tuple[str, str]], root: Path | None = None
) -> int:
    """Rewrite stored entries for renamed folders; returns the changed count.

    Entries equal to an old path or under it swap that prefix for the new one; mod renames match exactly, category renames cover nested entries.
    """
    valid: list[tuple[str, str]] = []
    for old, new in swaps:
        stripped_old = old.strip()
        stripped_new = new.strip()
        if stripped_old and stripped_old != stripped_new:
            valid.append((stripped_old, stripped_new))
    if not valid:
        return 0
    presets = load_presets(root)
    changed = 0
    for name, entries in presets.items():
        rewritten: list[str] = []
        dirty = False
        for entry in entries:
            replacement = entry
            for old, new in valid:
                if entry == old:
                    replacement = new
                    break
                if entry.startswith(old + "/"):
                    replacement = new + entry[len(old) :]
                    break
            if replacement != entry:
                changed += 1
                dirty = True
            rewritten.append(replacement)
        if dirty:
            presets[name] = sorted(set(rewritten))
    if changed:
        _write(presets, root)
    return changed


def remove_preset_paths(paths: Iterable[str], root: Path | None = None) -> int:
    """Remove stored entries for deleted mods or categories; returns the removed count.

    A stored entry is removed when it equals one of the given paths or under it; exact matches cover mod deletions, prefix matches cover nested entries in category deletions. Presets that lose their last entry through this removal are deleted; presets that were already empty are kept.
    """
    old_paths = [path.strip() for path in paths if path.strip()]
    if not old_paths:
        return 0
    presets = load_presets(root)
    removed = 0
    emptied: list[str] = []
    for name, entries in presets.items():
        kept: list[str] = []
        for entry in entries:
            if any(entry == old or entry.startswith(old + "/") for old in old_paths):
                removed += 1
            else:
                kept.append(entry)
        if len(kept) != len(entries):
            if kept:
                presets[name] = kept
            else:
                emptied.append(name)
    for name in emptied:
        del presets[name]
    if removed:
        _write(presets, root)
    return removed


def enabled_relative_paths(tree: ModNode, root: Path) -> frozenset[str]:
    """Enabled mod relative posix paths, depth-first, matching preset storage."""
    enabled: set[str] = set()
    for node in _walk(tree):
        if node.kind != "mod":
            continue
        relative = _node_relative(node, root)
        if not node.disabled:
            enabled.add(relative)
    return frozenset(enabled)


def mod_relative_paths(tree: ModNode, root: Path) -> frozenset[str]:
    """Canonical relative posix paths of every mod node, enabled or disabled."""
    return frozenset(_node_relative(node, root) for node in _walk(tree) if node.kind == "mod")


def _walk(node: ModNode) -> Iterable[ModNode]:
    """Depth-first walk: the node then its children in order."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _write(presets: dict[str, list[str]], root: Path | None) -> None:
    """Merge the presets into state.json and save it atomically."""
    state = load_state(root)
    state["presets"] = presets
    save_state(state, root)
