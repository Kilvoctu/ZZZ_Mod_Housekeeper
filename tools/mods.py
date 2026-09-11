"""Classify mod folders and analyze each mod's game-version status.

A mod root is the highest directory directly holding an included .ini or a
``mod.json``; backups are excluded and hashes are classified as the fixer resolves them.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .backups import is_backup_name, is_backup_path
from .changelog import ARROW_SPLIT_RE
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
UPDATES_SIGNAL = "updates available"
CURRENT_SIGNAL = "up to date"
STRUCTURAL_SIGNAL = "structural"


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


def is_outdated(version: ModVersion) -> bool:
    """True when the version has outdated or pending-structural hashes."""
    return version.outdated_count > 0 or version.structural_count > 0


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


@dataclass
class ModNode:
    """One node of the mods folder tree."""

    path: Path
    name: str
    kind: str
    disabled: bool = False
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
    variants: dict[str, int] = field(default_factory=dict, compare=False)


def analyze_mods(
    root: Path,
    datasets: Mapping[str, FixerData] | FixerData,
    structure: StructureData | None = None,
) -> tuple[ModNode, AnalysisSummary]:
    """Build the mod tree under root and analyze every mod's and file's version.

    ``datasets`` is one FixerData (default variant) or a mapping of variant
    keys to FixerData; a StructureData also counts pending structural fixes.
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
    return mod_roots


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
            continue
        nodes[parts] = ModNode(
            path=root.joinpath(*parts),
            name=name,
            kind=kind,
            disabled=(parts[-1] if parts else root.name).startswith(_DISABLED_PREFIX),
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
            key=lambda child: child.name,
        )
        files = sorted(
            (child for child in node.children if child.kind == "file"),
            key=lambda child: child.name,
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

    Every hash is resolved once per section hint exactly the way the fixer
    resolves it per line; table-absent chain-known hashes count as outdated.
    """
    unique = sorted(set(hashes))
    version = ModVersion(total=len(unique))
    best_rank: int | None = None
    breaks_label: str | None = None
    if data.entries:
        hint_map = hints or {}
        chain_known = set(data.chains) | {
            entry.to_hash for entry in data.entries if entry.to_hash
        }
        table_present = bool(data.db.reverse)
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
            elif table_present and hash_value in chain_known:
                version.outdated_count += 1
            else:
                version.unknown_count += 1
    else:
        version.unknown_count = len(unique)
    if best_rank is not None:
        version.label = _rank_label(ladder, best_rank)
        version.is_latest = best_rank == latest_index
        version.breaks_label = breaks_label
    return version


def _analyze_node(
    node: ModNode,
    datasets: Mapping[str, FixerData],
    ladders: Mapping[str, tuple[dict[int, tuple[str, str]], int]],
    known: Mapping[str, set[str]],
    root_acts_as_mod: bool = False,
    structure: StructureData | None = None,
    trigger_hashes: frozenset[str] = frozenset(),
    file_structural: dict[Path, int] | None = None,
) -> tuple[set[str], dict[Path, dict[str, set[str]]]]:
    """Analyze node in place; return (subtree hash union, per-file hints).

    File nodes get their own hashes' version, mod nodes (and a qualifying
    root) the subtree union's version; every .ini is read exactly once.
    """
    hashes: set[str] = set()
    file_hints: dict[Path, dict[str, set[str]]] = {}
    file_structural = file_structural if file_structural is not None else {}
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
        structural_total = _version_subtree_files(
            node, detected, data, ladder, latest_index, file_hints, file_structural
        )
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
) -> int:
    """Version every file node inside a mod's subtree against its dataset.

    Directory nodes inside the subtree inherit the mod's detected variant,
    and file versions reuse the collected hint maps (each .ini read only once).
    """
    total = 0
    for child in node.children:
        if child.kind == "file":
            child.variant = detected
            collected = file_hints[child.path]
            child.version = version_for_hashes(
                collected, data, ladder, latest_index, collected
            )
            if file_structural:
                count = file_structural.get(child.path, 0)
                child.version.structural_count = count
                total += count
        else:
            child.variant = detected
            total += _version_subtree_files(
                child, detected, data, ladder, latest_index, file_hints, file_structural
            )
    return total


def aggregate_updates(node: ModNode) -> str:
    """Subtree update signal for a version-less directory row (category/subfolder).

    "updates available" outranks "structural", which outranks "up to date";
    "" when no nested versioned node is classifiable.
    """
    outdated = False
    structural = False
    known = False
    stack = list(node.children)
    while stack:
        current = stack.pop()
        version = current.version
        if version is not None:
            if version.outdated_count > 0:
                outdated = True
                break
            if version.structural_count > 0:
                structural = True
            if version.current_count > 0:
                known = True
        stack.extend(current.children)
    if outdated:
        return UPDATES_SIGNAL
    if structural:
        return STRUCTURAL_SIGNAL
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
        stack.extend(node.children)
    return summary
