"""Classify mod folders and analyze each mod's game-version status.

A mod root is the highest directory directly holding an included .ini or a
``mod.json``; backups are excluded and hashes are classified as the fixer
resolves them. A mod root may be promoted to a wrapper parent that holds
only loose files (e.g. a readme) and that single nested mod root.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .backups import is_backup_name, is_backup_path
from .bufferbinds import BufferBind, collect_buffer_binds
from .changelog import ARROW_SPLIT_RE
from .dumpdata import DumpLayout
from .fixer import (
    FixerData,
    detect_variant,
    known_hashes,
    parse_ini_facts,
    read_ini_text,
    resolve_hash_chain,
    structural_fix_count_sections,
)
from .model import ChangeEntry
from .repo import DEFAULT_VARIANT
from .structure import StructureData

_DISABLED_PREFIX = "DISABLED_"
_CONTAINER_DIRS = frozenset({"resources"})
_PART_DIRS = frozenset({"body", "face", "hair", "legs", "torso", "head"})
BROKEN_BUFFERS_SIGNAL = "buffers"
CURRENT_SIGNAL = "up to date"
OLD_HASH_SIGNAL = "old hash"
SECTIONS_SIGNAL = "sections"


@dataclass
class ModVersion:
    """Game-version status of one mod's or one .ini file's override hashes."""

    label: str = "unknown"
    is_latest: bool = False
    breaks_label: str | None = None
    current_count: int = 0
    outdated_count: int = 0
    unknown_count: int = 0
    total: int = 0
    structural_count: int = 0
    broken_count: int = 0


def set_mod_enabled(path: Path, enabled: bool) -> Path:
    """Rename a mod folder between ``<Name>`` and ``DISABLED_<Name>``.

    A no-op returning ``path`` when the folder is already in the requested
    state; raises FileExistsError instead of overwriting an existing target.
    """
    name = path.name
    if enabled:
        if not name.startswith(_DISABLED_PREFIX):
            return path
        target = path.parent / name.removeprefix(_DISABLED_PREFIX)
    else:
        if name.startswith(_DISABLED_PREFIX):
            return path
        target = path.parent / (_DISABLED_PREFIX + name)
    if target.exists():
        raise FileExistsError(f"target already exists: {target}")
    path.rename(target)
    return target


def rename_mod_folder(path: Path, new_name: str) -> Path:
    """Rename a mod or category folder, keeping its DISABLED_ prefix; typed DISABLED_ names are rejected.

    Unchanged display names are a no-op; case-only renames are allowed; a real collision raises FileExistsError.
    """
    cleaned = validate_folder_name(new_name)
    if cleaned.startswith(_DISABLED_PREFIX):
        raise ValueError(
            "Rename to a DISABLED_ name is ambiguous; use enable/disable instead"
        )
    disabled = path.name.startswith(_DISABLED_PREFIX)
    current_display = path.name.removeprefix(_DISABLED_PREFIX)
    if cleaned == current_display:
        return path
    target = path.parent / ((_DISABLED_PREFIX + cleaned) if disabled else cleaned)
    if target.exists() and path.name.lower() != target.name.lower():
        raise FileExistsError(f"target already exists: {target}")
    path.rename(target)
    return target


def validate_folder_name(name: str) -> str:
    """Cleaned folder name; rejects blank names, \\/:*?"<>| and trailing dot/space."""
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Folder name is empty")
    if any(char in cleaned for char in '\\/:*?"<>|'):
        raise ValueError(f"Folder name contains invalid characters: {cleaned}")
    if cleaned.endswith((".", " ")):
        raise ValueError(f"Folder name must not end with a dot or space: {cleaned}")
    return cleaned


def create_mod_folder(mods_dir: Path, name: str) -> Path:
    """Create a new empty folder directly under mods_dir and return it.

    Surrounding whitespace is stripped; raises ValueError for blank names,
    invalid Windows filename characters, a trailing dot, or duplicates.
    """
    cleaned = validate_folder_name(name)
    target = Path(mods_dir) / cleaned
    if target.exists():
        raise ValueError(f"A folder named '{cleaned}' already exists")
    target.mkdir()
    return target


@dataclass
class ModNode:
    """One node of the mods folder tree."""

    path: Path
    name: str
    kind: str
    disabled: bool = False
    empty_folder: bool = False
    children: list["ModNode"] = field(default_factory=list)
    version: ModVersion | None = None
    variant: str | None = None


@dataclass
class AnalysisSummary:
    """Folder-wide counts of one analyze_mods run."""

    mods: int = 0
    current: int = 0
    outdated: int = 0
    unknown: int = 0
    backups_skipped: int = 0
    files_scanned: int = 0
    structural: int = 0
    broken: int = 0
    variants: dict[str, int] = field(default_factory=dict, compare=False)


def analyze_mods(
    root: Path,
    datasets: Mapping[str, FixerData] | FixerData,
    structure: StructureData | None = None,
) -> tuple[ModNode, AnalysisSummary]:
    """Build the mod tree under root and analyze every mod's and file's version.

    ``datasets`` is one FixerData (default variant) or a mapping of variant
    keys to FixerData; a StructureData also counts pending structural fixes.
    Empty directories (no mods anywhere beneath) are built as category nodes
    with ``empty_folder=True``; the UI decides whether to display them.
    """
    root = Path(root)
    if isinstance(datasets, FixerData):
        datasets = {DEFAULT_VARIANT: datasets}
    tree, skipped, root_acts_as_mod = _build_tree(root)
    ladders = {
        variant: version_ladder(dataset.entries)
        for variant, dataset in datasets.items()
    }
    known_sets = {
        variant: known_hashes(dataset) for variant, dataset in datasets.items()
    }
    trigger_hashes = structure.known_hashes() if structure is not None else frozenset()
    _analyze_node(
        tree,
        datasets,
        ladders,
        known_sets,
        root_acts_as_mod=root_acts_as_mod,
        structure=structure,
        trigger_hashes=trigger_hashes,
    )
    return tree, _summarize(tree, skipped)


def analyze_scope(
    node: ModNode,
    datasets: Mapping[str, FixerData] | FixerData,
    structure: StructureData | None = None,
) -> None:
    """Re-analyze one already-built mod or category subtree in place.

    ``datasets`` normalizes a bare FixerData like analyze_mods; raises
    ValueError for root nodes (callers fall back to analyze_mods) and file
    nodes (callers resolve the containing mod first).
    """
    if node.kind == "root":
        raise ValueError("analyze_scope cannot re-analyze the mods root node")
    if node.kind == "file":
        raise ValueError("analyze_scope cannot re-analyze a file node")
    if isinstance(datasets, FixerData):
        datasets = {DEFAULT_VARIANT: datasets}
    ladders = {
        variant: version_ladder(dataset.entries)
        for variant, dataset in datasets.items()
    }
    known_sets = {
        variant: known_hashes(dataset) for variant, dataset in datasets.items()
    }
    trigger_hashes = structure.known_hashes() if structure is not None else frozenset()
    _analyze_node(
        node,
        datasets,
        ladders,
        known_sets,
        structure=structure,
        trigger_hashes=trigger_hashes,
    )


def retarget_subtree_paths(node: ModNode, new_path: Path) -> None:
    """Rebase node and its whole subtree onto a relocated directory.

    Used after a DISABLED_ toggle rename so later subtree file reads target
    the renamed folder instead of the stale pre-rename paths.
    """
    old_path = node.path
    node.path = new_path
    for child in node.children:
        retarget_subtree_paths(child, new_path / child.path.relative_to(old_path))


def _count_ini_files(directory: str) -> int:
    """Count .ini files recursively under one pruned backup directory.

    Unreadable branches count as empty, matching the walk's OSError handling.
    """
    total = 0
    try:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.is_dir():
                    total += _count_ini_files(entry.path)
                elif entry.name.lower().endswith(".ini"):
                    total += 1
    except OSError:
        pass
    return total


def _dir_has_loose_file(directory: Path) -> bool:
    """True when directory directly holds at least one non-.ini file."""
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file() and not entry.name.lower().endswith(".ini"):
                    return True
    except OSError:
        pass
    return False


def _scan_dir(
    root: Path,
    parts: tuple[str, ...],
    dirs: set[tuple[str, ...]],
    included: list[tuple[str, ...]],
    mod_json: set[tuple[str, ...]],
) -> int:
    """Collect one directory subtree's relative parts in a single scandir pass.

    Records directory parts, included .ini parts and mod.json presence into
    the accumulators; backup-named subdirectories are pruned and their .ini
    count (skipped backups) is returned.
    """
    dirs.add(parts)
    path = root.joinpath(*parts) if parts else root
    pruned = 0
    try:
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                name = entry.name
                if entry.is_dir():
                    if is_backup_name(name):
                        pruned += _count_ini_files(entry.path)
                    else:
                        pruned += _scan_dir(
                            root, (*parts, name), dirs, included, mod_json
                        )
                elif name.lower().endswith(".ini"):
                    if is_backup_path((*parts, name)):
                        pruned += 1
                    else:
                        included.append((*parts, name))
                elif name.lower() == "mod.json" and entry.is_file():
                    mod_json.add(parts)
    except OSError:
        pass
    return pruned


def _mod_roots(
    root: Path,
    included: list[Path],
    dirs_with_ini: set[Path],
    dirs_with_mod_json: set[Path],
) -> set[Path]:
    """Mod root of every included ini.

    Each ini's candidate chain runs root-down to its parent, and the first
    directory on it that directly holds an included ini or a mod.json wins.
    Container and part-pack winners are then promoted to their parent, and a
    final single-pass wrapper promotion moves a winner up to a parent that
    holds only loose non-.ini files plus that one nested winner.
    """
    mod_roots: set[Path] = set()
    for path in included:
        chain = [root]
        current = root
        for part in path.parent.relative_to(root).parts:
            current = current / part
            chain.append(current)
        for candidate in chain:
            if candidate in dirs_with_ini or candidate in dirs_with_mod_json:
                mod_roots.add(candidate)
                break
    promoted: dict[Path, Path] = {}
    for winner in mod_roots:
        if winner not in dirs_with_ini:
            continue
        parent = winner.parent
        if len(winner.parts) < len(root.parts) + 2:
            continue
        if parent in dirs_with_ini or parent in dirs_with_mod_json:
            continue
        siblings = [other for other in mod_roots if other.parent == parent]
        if winner.name.lower() in _CONTAINER_DIRS and not any(
            other is not winner and winner.parent in other.parents
            for other in mod_roots
        ) or (
            winner.name.lower() in _PART_DIRS
            and len(siblings) >= 2
            and all(other.name.lower() in _PART_DIRS for other in siblings)
        ):
            promoted[winner] = parent
    winners = {promoted.get(winner, winner) for winner in mod_roots}
    wrapper_promoted: dict[Path, Path] = {}
    for winner in winners:
        parent = winner.parent
        if len(winner.parts) < len(root.parts) + 2:
            continue
        if parent in dirs_with_ini or parent in dirs_with_mod_json:
            continue
        if not _dir_has_loose_file(parent):
            continue
        if any(other != winner and other.parent == parent for other in winners):
            continue
        wrapper_promoted[winner] = parent
    return {wrapper_promoted.get(winner, winner) for winner in winners}


def _build_tree(root: Path) -> tuple[ModNode, int, bool]:
    """Build the mod tree; returns (root node, excluded backups, root acts as a mod)."""
    dirs: set[tuple[str, ...]] = set()
    included_parts: list[tuple[str, ...]] = []
    mod_json_parts: set[tuple[str, ...]] = set()
    skipped = _scan_dir(root, (), dirs, included_parts, mod_json_parts)
    included = [root.joinpath(*parts) for parts in included_parts]
    dirs_with_ini = {path.parent for path in included}
    mod_roots = _mod_roots(
        root,
        included,
        dirs_with_ini,
        {root.joinpath(*parts) for parts in mod_json_parts},
    )
    mod_parts = {
        tuple(mod_root.relative_to(root).parts) for mod_root in mod_roots
    }
    ancestors = {
        part[:index] for part in mod_parts for index in range(1, len(part))
    }
    nodes: dict[tuple[str, ...], ModNode] = {}
    for parts in sorted(dirs, key=lambda item: (len(item), item)):
        empty = False
        if not parts:
            kind = "root"
            name = root.name or str(root)
        elif parts in mod_parts:
            kind = "mod"
            name = parts[-1]
        elif parts in ancestors:
            kind = "category"
            name = parts[-1]
        elif any(parts[:index] in mod_parts for index in range(len(parts))):
            kind = "subfolder"
            name = parts[-1]
        else:
            kind = "category"
            name = parts[-1]
            empty = True
        nodes[parts] = ModNode(
            path=root.joinpath(*parts),
            name=name,
            kind=kind,
            disabled=(parts[-1] if parts else root.name).startswith(_DISABLED_PREFIX),
            empty_folder=empty,
        )
    for parts, node in nodes.items():
        if not parts:
            continue
        parent = nodes.get(parts[:-1])
        if parent is not None:
            parent.children.append(node)
    for parts in included_parts:
        parent = nodes.get(parts[:-1])
        if parent is None:
            continue
        parent.children.append(
            ModNode(
                path=root.joinpath(*parts),
                name=parts[-1],
                kind="file",
                disabled=parts[-1].startswith(_DISABLED_PREFIX),
            )
        )
    for node in nodes.values():
        directories = sorted(
            (child for child in node.children if child.kind != "file"),
            key=lambda child: child.name.removeprefix(_DISABLED_PREFIX),
        )
        files = sorted(
            (child for child in node.children if child.kind == "file"),
            key=lambda child: child.name.removeprefix(_DISABLED_PREFIX),
        )
        node.children = directories + files
    return nodes[()], skipped, () in mod_parts


def version_ladder(
    entries: Iterable[ChangeEntry],
) -> tuple[dict[int, tuple[str, str]], int]:
    """Map version_index -> (left label, right label), split on the arrow.

    The first entry seen for a version_index wins.  Returns the ladder and
    latest_index (0 when there are no entries, i.e. no ladder at all).
    """
    ladder: dict[int, tuple[str, str]] = {}
    latest_index = 0
    for entry in entries:
        index = entry.version_index
        if index not in ladder:
            parts = ARROW_SPLIT_RE.split(entry.version_label, maxsplit=1)
            if len(parts) == 2:
                ladder[index] = (parts[0], parts[1])
            else:
                label = entry.version_label.strip() or "unknown"
                ladder[index] = (label, label)
        latest_index = max(latest_index, index)
    return ladder, latest_index


def _rank_label(ladder: dict[int, tuple[str, str]], rank: int) -> str:
    """Label for a rank: 0 -> left label of version 1, r >= 1 -> right label of r."""
    if rank <= 0:
        pair = ladder.get(1)
        return pair[0] if pair else "unknown"
    pair = ladder.get(rank)
    return pair[1] if pair else "unknown"


def _earliest_fixable(
    hash_value: str, contexts: Iterable[str], data: FixerData
) -> ChangeEntry | None:
    """Earliest breaking entry for one hash across its section hint contexts.

    Each hint resolves its own chain like the fixer does per line; returns
    None when no context is fixable, else the smallest-version_index entry.
    """
    earliest: ChangeEntry | None = None
    for hint in contexts:
        resolved = resolve_hash_chain(hash_value, hint, data)
        if resolved is None:
            candidate = data.chains[hash_value][0]
        elif resolved:
            candidate = resolved[0]
        else:
            continue
        if earliest is None or candidate.version_index < earliest.version_index:
            earliest = candidate
    return earliest


def version_for_hashes(
    hashes: Iterable[str],
    data: FixerData,
    ladder: dict[int, tuple[str, str]],
    latest_index: int,
    hints: Mapping[str, Iterable[str]] | None = None,
) -> ModVersion:
    """Aggregate the version status of one scope's unique hashes.

    Every hash is resolved once per section hint exactly the way the fixer resolves
    it per line; hashes with no applicable rename stay unknown even when chain-known.
    """
    unique = sorted(set(hashes))
    version = ModVersion(total=len(unique))
    best_rank: int | None = None
    breaks_label: str | None = None
    if data.entries:
        hint_map = hints or {}
        for hash_value in unique:
            contexts = hint_map.get(hash_value)
            if contexts:
                earliest = _earliest_fixable(hash_value, sorted(contexts), data)
            else:
                earliest = _earliest_fixable(hash_value, [""], data)
            if earliest is not None:
                version.outdated_count += 1
                rank = earliest.version_index - 1
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    breaks_label = earliest.version_label or None
            elif hash_value in data.db.reverse:
                version.current_count += 1
                if best_rank is None or latest_index < best_rank:
                    best_rank = latest_index
            else:
                version.unknown_count += 1
    else:
        version.unknown_count = len(unique)
    if best_rank is not None:
        version.label = _rank_label(ladder, best_rank)
        version.is_latest = best_rank == latest_index
        version.breaks_label = breaks_label
    return version


_SLOT_ROLES = {"vb0": "position", "vb1": "texcoord", "vb2": "blend"}


def _dump_layout_for_hash(bind: BufferBind, data: FixerData) -> DumpLayout | None:
    """The dump layout covering this bind's hash, or None when uncovered.

    Direct lookup: the bind hash matches a dump component's index buffer, so
    that component's layout for the slot's role applies.  Fallback: the bind
    hash matches the slot role's current hash.  Both take the first sorted
    match.
    """
    role = _SLOT_ROLES.get(bind.slot)
    if role is None:
        return None
    dumps = data.dumps
    for key2 in sorted({(key[0], key[1]) for key in dumps.layouts}):
        if dumps.ibs.get(key2) != bind.hash:
            continue
        layout = dumps.layouts.get((*key2, role))
        if layout is not None:
            return layout
    for key3 in sorted(dumps.current_hashes):
        if key3[2] == role and dumps.current_hashes[key3] == bind.hash:
            layout = dumps.layouts.get(key3)
            if layout is not None:
                return layout
    return None


def _bind_is_broken(bind: BufferBind, data: FixerData) -> bool:
    """Whether one buffer bind is diagnosed broken against the fixer data.

    A declared-but-absent binary is broken (.buf and .ib alike); a present
    vertex buffer with a declared stride is broken when the covering dump
    layout's stride disagrees, and dump-uncovered texcoord binds fall back to
    the legacy face gate (the 36-byte pre-2.54 face format).  Undeclared
    strides are never diagnosed.  Index buffers only ever break by missing.
    """
    if not bind.exists:
        return bind.filename != ""
    if bind.slot not in _SLOT_ROLES:
        return False
    if bind.stride is None:
        return False
    layout = _dump_layout_for_hash(bind, data)
    if layout is None:
        return (
            bind.slot == "vb1"
            and bind.hash in data.face_texcoord_hashes
            and bind.stride == 36
        )
    return bind.stride != layout.stride


def _analyze_node(
    node: ModNode,
    datasets: Mapping[str, FixerData],
    ladders: Mapping[str, tuple[dict[int, tuple[str, str]], int]],
    known: Mapping[str, set[str]],
    root_acts_as_mod: bool = False,
    structure: StructureData | None = None,
    trigger_hashes: frozenset[str] = frozenset(),
    file_structural: dict[Path, int] | None = None,
    file_binds: dict[Path, list[BufferBind]] | None = None,
) -> tuple[set[str], dict[Path, dict[str, set[str]]]]:
    """Analyze node in place; return (subtree hash union, per-file hints).

    File nodes get their own hashes' version, mod nodes (and a qualifying
    root) the subtree union's version; every .ini is read exactly once.
    """
    hashes: set[str] = set()
    file_hints: dict[Path, dict[str, set[str]]] = {}
    file_structural = file_structural if file_structural is not None else {}
    file_binds = file_binds if file_binds is not None else {}
    for child in node.children:
        if child.kind == "file":
            try:
                text = read_ini_text(child.path)
                collected, sections = parse_ini_facts(text)
            except (ValueError, OSError):
                collected = {}
                sections = []
                text = None
            file_hints[child.path] = collected
            hashes.update(collected)
            if text is not None and collected:
                file_binds[child.path] = collect_buffer_binds(text, child.path)
            if (
                structure is not None
                and text is not None
                and collected
                and (set(collected) & trigger_hashes)
            ):
                file_structural[child.path] = structural_fix_count_sections(
                    sections, structure, file=str(child.path)
                )
    for child in node.children:
        if child.kind != "file":
            child_hashes, child_hints = _analyze_node(
                child, datasets, ladders, known, structure=structure,
                trigger_hashes=trigger_hashes,
                file_structural=file_structural,
                file_binds=file_binds,
            )
            hashes |= child_hashes
            file_hints.update(child_hints)
    acts_as_mod = node.kind == "mod" or (node.kind == "root" and root_acts_as_mod)
    if acts_as_mod:
        detected = detect_variant(hashes, datasets, known)
        node.variant = detected
        effective = detected or DEFAULT_VARIANT
        data = datasets[effective]
        ladder, latest_index = ladders[effective]
        scope_hints: dict[str, set[str]] = {}
        for collected in file_hints.values():
            for hash_value, contexts in collected.items():
                scope_hints.setdefault(hash_value, set()).update(contexts)
        node.version = version_for_hashes(
            hashes, data, ladder, latest_index, scope_hints
        )
        structural_total, broken_total = _version_subtree_files(
            node, detected, data, ladder, latest_index, file_hints, file_structural,
            file_binds,
        )
        node.version.broken_count = broken_total
        if structure is not None:
            node.version.structural_count = structural_total
    return hashes, file_hints


def _version_subtree_files(
    node: ModNode,
    detected: str | None,
    data: FixerData,
    ladder: dict[int, tuple[str, str]],
    latest_index: int,
    file_hints: dict[Path, dict[str, set[str]]],
    file_structural: dict[Path, int] | None = None,
    file_binds: dict[Path, list[BufferBind]] | None = None,
) -> tuple[int, int]:
    """Version every file node inside a mod's subtree against its dataset.

    Directory nodes inside the subtree inherit the mod's detected variant,
    and file versions reuse the collected hint maps (each .ini read only once).
    Returns (structural_total, broken_total) accumulated over the subtree.
    """
    file_binds = file_binds if file_binds is not None else {}
    structural_total = 0
    broken_total = 0
    for child in node.children:
        if child.kind == "file":
            child.variant = detected
            collected = file_hints[child.path]
            version = version_for_hashes(
                collected, data, ladder, latest_index, collected
            )
            child.version = version
            version.broken_count = sum(
                1
                for bind in file_binds.get(child.path, ())
                if _bind_is_broken(bind, data)
            )
            broken_total += version.broken_count
            if file_structural:
                count = file_structural.get(child.path, 0)
                version.structural_count = count
                structural_total += count
        else:
            child.variant = detected
            child_structural, child_broken = _version_subtree_files(
                child, detected, data, ladder, latest_index, file_hints,
                file_structural, file_binds,
            )
            structural_total += child_structural
            broken_total += child_broken
    return structural_total, broken_total


def aggregate_updates(node: ModNode) -> str:
    """Subtree update signal for a version-less directory row (category/subfolder).

    "buffers" (broken shipped buffers: missing or old-format) outranks
    "old hash" (chain-fixable stale hashes), which outranks "sections"
    (pending structural ini insertions), which outranks "up to date";
    "" when no nested versioned node is classifiable.
    """
    broken = False
    outdated = False
    structural = False
    known = False
    stack = list(node.children)
    while stack:
        current = stack.pop()
        version = current.version
        if version is not None:
            if version.broken_count > 0:
                broken = True
                break
            if version.outdated_count > 0:
                outdated = True
            if version.structural_count > 0:
                structural = True
            if version.current_count > 0:
                known = True
        stack.extend(current.children)
    if broken:
        return BROKEN_BUFFERS_SIGNAL
    if outdated:
        return OLD_HASH_SIGNAL
    if structural:
        return SECTIONS_SIGNAL
    return CURRENT_SIGNAL if known else ""


def _summarize(tree: ModNode, backups_skipped: int) -> AnalysisSummary:
    """Count mods and their statuses across the analyzed tree."""
    summary = AnalysisSummary(backups_skipped=backups_skipped)
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.kind == "file":
            summary.files_scanned += 1
            continue
        version = node.version
        if (node.kind == "mod" or node.kind == "root") and version is not None:
            summary.mods += 1
            variant_key = node.variant or "unknown"
            summary.variants[variant_key] = summary.variants.get(variant_key, 0) + 1
            if version.is_latest:
                summary.current += 1
            elif version.outdated_count > 0:
                summary.outdated += 1
            else:
                summary.unknown += 1
            if version.structural_count > 0:
                summary.structural += 1
            if version.broken_count > 0:
                summary.broken += 1
        stack.extend(node.children)
    return summary
