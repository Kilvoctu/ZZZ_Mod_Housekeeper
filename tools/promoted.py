"""Which image represents each mod, stored in state.json.

"Set as preview image" in the gallery records <mod folder>: <image file name>;
hovering the mod shows that picture instead of its first preview.
"""

from collections.abc import Iterable, Mapping
from pathlib import Path

from .presets import _clean_disabled_leaf
from .state import load_state, save_state


def canonical_key(relative: str) -> str:
    """Canonical promoted key: DISABLED_-leaf-stripped relative posix path."""
    return _clean_disabled_leaf(relative.strip())


def load_promoted(root: Path | None = None) -> dict[str, str]:
    """Read {mod relative path: image relative name} from state.json; {} on missing, undecodable or non-dict JSON.

    Keys are canonicalized on read, healing pre-canonical entries that still carry a raw DISABLED_ leaf.
    """
    promoted: dict[str, str] = {}
    section = load_state(root)["promoted"]
    if isinstance(section, dict):
        for key, value in section.items():
            if isinstance(key, str) and isinstance(value, str):
                promoted[canonical_key(key)] = value
    return promoted


def save_promoted(promoted: Mapping[str, str], root: Path | None = None) -> None:
    """Store the promoted previews mapping, overwriting any previous content."""
    _write(dict(promoted), root)


def retarget_promoted_paths(
    swaps: Iterable[tuple[str, str]], root: Path | None = None
) -> int:
    """Rewrite stored keys for renamed folders; returns the changed count.

    Keys equal to an old path or under it swap that prefix for the new one; mod renames match exactly, category renames cover nested keys, and old paths also match their canonical DISABLED_-leaf-stripped form.
    """
    valid: list[tuple[str, str]] = []
    for old, new in swaps:
        stripped_old = canonical_key(old)
        stripped_new = new.strip()
        if stripped_old and stripped_old != stripped_new:
            valid.append((stripped_old, stripped_new))
    if not valid:
        return 0
    promoted = load_promoted(root)
    changed = 0
    rewritten: dict[str, str] = {}
    for key, value in promoted.items():
        replacement = key
        for old, new in valid:
            if key == old:
                replacement = new
                break
            if key.startswith(old + "/"):
                replacement = new + key[len(old) :]
                break
        if replacement != key:
            changed += 1
        rewritten[replacement] = value
    if changed:
        _write(rewritten, root)
    return changed


def remove_promoted_paths(paths: Iterable[str], root: Path | None = None) -> int:
    """Remove stored keys for deleted mods or categories; returns the removed count.

    A key is removed when it equals one of the given paths or under it; exact matches cover mod deletions, prefix matches cover nested keys in category deletions, and given paths also match their canonical DISABLED_-leaf-stripped form.
    """
    old_paths = [
        cleaned for cleaned in (canonical_key(path) for path in paths) if cleaned
    ]
    if not old_paths:
        return 0
    promoted = load_promoted(root)
    removed = 0
    kept: dict[str, str] = {}
    for key, value in promoted.items():
        if any(key == old or key.startswith(old + "/") for old in old_paths):
            removed += 1
        else:
            kept[key] = value
    if removed:
        _write(kept, root)
    return removed


def _write(promoted: dict[str, str], root: Path | None) -> None:
    """Merge the promoted entries into state.json and save it atomically."""
    state = load_state(root)
    state["promoted"] = promoted
    save_state(state, root)
