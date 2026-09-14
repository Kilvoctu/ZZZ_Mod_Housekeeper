"""Background task workers for the ZZZ Mod Housekeeper GUI.

``TaskWorker`` runs a plain function on a ``QThread`` so the GUI never blocks;
thin factory helpers prebind the engine calls the main window needs.
"""

import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

# noinspection PyPackageRequirements
from PySide6.QtCore import QObject, QThread, Signal

from ..backups import (
    collect_backup_chains,
    collect_backup_chains_for,
    default_backups_dir,
    delete_store_folder,
    included_ini_files,
    prune_empty_store_folders,
    retarget_store_folder,
)
from ..blend_remap import (
    apply_remap,
    load_blend_remaps,
    prune_blend_markers,
    remove_blend_state_keys,
    rewrite_blend_state_keys,
    scan_blend_targets,
)
from ..blend_vote import (
    apply_blend_vote_remap,
    scan_blend_vote_targets,
)
from ..dumpdata import DumpData, load_dump_data
from ..fixer import (
    FilePlan,
    FixerData,
    apply_plan,
    collect_texture_override_hashes,
    detect_variant,
    known_hashes,
    load_fixer_data,
    revert_backups,
    scan_files,
    scan_folder,
)
from ..importer import extract_archive
from ..mods import ModNode, analyze_mods, rename_mod_folder, set_mod_enabled
from ..presets import remove_preset_paths, retarget_preset_paths
from ..promoted import remove_promoted_paths, retarget_promoted_paths
from ..repo import (
    DEFAULT_VARIANT,
    REPO_VARIANTS,
    RepoError,
    changelog_path,
    default_cache_dir,
    ensure_repo,
    hash_variants,
    repo_head,
    repo_update_available,
)
from ..structure import StructureData, build_structure
from ..texcoord_upgrade import (
    apply_upgrade,
    buffer_gate,
    prune_texcoord_markers,
    remove_texcoord_state_keys,
    rewrite_texcoord_state_keys,
    scan_texcoord_targets,
)

LogFn = Callable[[str], None]

_UPDATE_BUTTON = "Update data"
"""The data button's label, for error hints."""

_DISABLED_PREFIX = "DISABLED_"


def friendly_error(exc: BaseException) -> str:
    """Format a worker error message, special-casing a cleaned onefile runtime.

    A PyInstaller onefile app extracts its stdlib archive (base_library.zip)
    into a _MEI temp dir; if that dir is wiped while the app runs, imports
    fail with a bare FileNotFoundError.  Map that to an actionable message.
    """
    message = f"{type(exc).__name__}: {exc}"
    detail = str(exc)
    if isinstance(exc, OSError) and ("base_library.zip" in detail or "_MEI" in detail):
        return (
            "ZZZModKeeper's runtime files were cleaned from the temp folder while "
            "running - close the app and relaunch it"
        )
    return message


class TaskWorker(QThread):
    """Run ``fn(*args)`` in the background and report the outcome.

    Signals are emitted from the worker thread; Qt's queued connections
    deliver them on the GUI thread of the connected receiver.
    """

    log = Signal(str)
    done = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        log_kwarg: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Prepare a task.

        ``log_kwarg`` names the keyword parameter of ``fn`` that receives the
        log callable (e.g. ``"log"``), or ``None`` when ``fn`` takes no log callable.
        """
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._log_kwarg = log_kwarg

    def run(self) -> None:
        """Execute the task; emit ``done`` with the result or ``error``."""
        kwargs: dict[str, Any] = {}
        if self._log_kwarg is not None:
            kwargs[self._log_kwarg] = self.log.emit
        # noinspection PyBroadException
        try:
            result = self._fn(*self._args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(friendly_error(exc))
            return
        self.done.emit(result)


Scope = Path | str | Sequence[Path]
"""Fix/revert scope: a single directory path, or explicit single-file paths."""


def _as_paths(scope: Scope) -> tuple[bool, list[Path]]:
    """Interpret a scope as (is_dir, paths).

    A single Path/str is a directory path; a sequence is explicit file
    paths (e.g. a single selected .ini row wrapped in a list).
    """
    if isinstance(scope, (str, Path)):
        return True, [Path(scope)]
    return False, [Path(item) for item in scope]


def _parse_available(
    repo_dirs: dict[str, Path], log: LogFn
) -> dict[str, FixerData]:
    """Parse every hash-repo whose changelog exists; log and skip the others.

    Dump-kind variants (fix_tool) are not hash datasets and are skipped
    silently; their parsed dump data is attached separately by
    ``_attach_dump_data``.
    """
    datasets: dict[str, FixerData] = {}
    for variant, cache in repo_dirs.items():
        if variant not in hash_variants():
            continue
        if not changelog_path(cache).exists():
            log(f"No {variant} data cloned yet — click '{_UPDATE_BUTTON}' to fetch it.")
            continue
        datasets[variant] = load_fixer_data(cache, include_pcdata=(variant == "2048p"))
    return datasets


def _attach_dump_data(datasets: dict[str, FixerData], log: LogFn) -> None:
    """Attach the parsed fix-tool dump cache to every loaded dataset.

    The dump variant is shared rather than per-variant: one DumpData is set on
    all datasets (the 2048p character DB splits dump folder names); without a
    dump cache this is a silent no-op and every dataset keeps its empty dumps.
    """
    dump_cache = default_cache_dir("fix_tool")
    if not dump_cache.is_dir():
        return
    source = datasets.get("2048p")
    dumps: DumpData = load_dump_data(
        dump_cache, db=source.db if source is not None else None
    )
    for data in datasets.values():
        data.dumps = dumps
    if dumps.vertexlimit:
        log(f"Loaded {len(dumps.vertexlimit)} dump component(s)")


def _fallback_dataset(log: LogFn) -> FixerData:
    """Dataset built without any repo clone: legacy chains, pcdata, user patches."""
    data = load_fixer_data(None, include_pcdata=True)
    if data.chains or data.entries or data.user_patches or data.db.reverse:
        log(
            "No repo data cloned — using data/*.txt patches, "
            "PlayerCharacterData.json and bundled legacy chains only."
        )
    else:
        log(f"No upstream data loaded — hashes unknown until '{_UPDATE_BUTTON}' fetches it.")
    return data


def update_data_worker(parent: QObject | None = None) -> TaskWorker:
    """Worker that ensures every variant repo, then parses the available ones.

    Unreachable variants are logged and skipped; with no cloned data at all,
    a fallback dataset (legacy chains, pcdata, user patches) is used so the
    GUI never blocks; ``done`` carries ``(repo_dirs, datasets, heads, structure)``.
    """

    def job(
        log: LogFn,
    ) -> tuple[dict[str, Path], dict[str, FixerData], dict[str, str], StructureData | None]:
        repo_dirs: dict[str, Path] = {}
        for variant in REPO_VARIANTS:
            try:
                repo_dirs[variant] = ensure_repo(variant=variant, log=log)
            except RepoError as exc:
                log(f"Could not update {variant} data: {exc}")
        datasets = _parse_available(repo_dirs, log)
        if not datasets:
            datasets = {DEFAULT_VARIANT: _fallback_dataset(log)}
        _attach_dump_data(datasets, log)
        heads = {
            variant: repo_head(repo_dirs.get(variant, default_cache_dir(variant)))
            for variant in datasets
        }
        structure = structure_for(datasets)
        return repo_dirs, datasets, heads, structure

    return TaskWorker(job, log_kwarg="log", parent=parent)


def load_all_data_worker(parent: QObject | None = None) -> TaskWorker:
    """Worker that parses every locally cloned variant repo, no network access.

    A variant without a local clone is logged as a hint to update; with no
    cloned data at all, a fallback dataset (legacy chains, pcdata, user
    patches) is used; ``done`` carries ``(repo_dirs, datasets, heads, structure)``
    like ``update_data_worker``.
    """

    def job(
        log: LogFn,
    ) -> tuple[dict[str, Path], dict[str, FixerData], dict[str, str], StructureData | None]:
        caches = {variant: default_cache_dir(variant) for variant in REPO_VARIANTS}
        datasets = _parse_available(caches, log)
        if not datasets:
            datasets = {DEFAULT_VARIANT: _fallback_dataset(log)}
        _attach_dump_data(datasets, log)
        heads = {variant: repo_head(cache) for variant, cache in caches.items()}
        structure = structure_for(datasets)
        return caches, datasets, heads, structure

    return TaskWorker(job, log_kwarg="log", parent=parent)


def check_updates_worker(parent: QObject | None = None) -> TaskWorker:
    """Worker that checks both variant repos for upstream updates.

    Pure network read that never mutates the local clones or parses anything;
    ``done`` maps each variant to True (newer commits), False (up to date), or None (offline).
    """

    def job() -> dict[str, bool | None]:
        return {
            variant: repo_update_available(variant) for variant in REPO_VARIANTS
        }

    return TaskWorker(job, parent=parent)


def analyze_worker(
    mods_dir: Path | str,
    datasets: Mapping[str, FixerData] | FixerData,
    structure: StructureData | None = None,
    parent: QObject | None = None,
) -> TaskWorker:
    """Worker that analyses a mods folder; ``done`` carries (ModNode, AnalysisSummary).

    Structural rule knowledge from the loaded variants is passed to the analysis;
    ``structure`` accepts a prebuilt StructureData or is derived from the datasets.
    """
    mapping = datasets if isinstance(datasets, Mapping) else {DEFAULT_VARIANT: datasets}

    def job():
        return analyze_mods(
            Path(mods_dir),
            mapping,
            structure if structure is not None else structure_for(mapping),
        )

    return TaskWorker(job, parent=parent)


def structure_for(datasets: Mapping[str, FixerData]) -> StructureData | None:
    """Structural rule knowledge derived from every loaded variant."""
    if not datasets:
        return None
    dbs = {variant: data.db for variant, data in datasets.items()}
    pairs: list[tuple[str, str]] = []
    for data in datasets.values():
        for entry in data.entries:
            from_hash = entry.from_hash
            to_hash = entry.to_hash
            if not from_hash:
                continue
            if not to_hash:
                continue
            pairs.append((from_hash, to_hash))
    return build_structure(dbs, chain_pairs=pairs)


def fix_mod_worker(
    mods_dir: Path | str,
    mod_path: Scope,
    datasets: Mapping[str, FixerData],
    structure: StructureData | None = None,
    parent: QObject | None = None,
) -> TaskWorker:
    """Scan one mod/file scope and apply its fixes in one background job.

    Applies run in a bounded fixpoint with one store backup per file per run; folder
    scopes then remap bound blend buffers using data/blend_remaps.json and upgrade
    face texcoord buffers to the post-2.54 float32 format, both with store backups;
    ``done`` carries ``(variant, plans, written)``.
    """

    def job(log: LogFn) -> tuple[str, list[FilePlan], int]:
        nonlocal structure
        is_dir, paths = _as_paths(mod_path)
        hashes: set[str] = set()
        if is_dir:
            for path in included_ini_files(paths[0]):
                hashes.update(collect_texture_override_hashes(path))
        else:
            for path in paths:
                hashes.update(collect_texture_override_hashes(path))
        known_sets = {
            variant: known_hashes(data) for variant, data in datasets.items()
        }
        variant = detect_variant(hashes, datasets, known_sets) or DEFAULT_VARIANT
        loaded = datasets.get(variant)
        if loaded is None:
            raise ValueError(
                f"{variant} data is not loaded — click '{_UPDATE_BUTTON}' first."
            )
        data = loaded
        if structure is None:
            structure = structure_for(datasets)

        def rescan() -> list[FilePlan]:
            if is_dir:
                return scan_folder(paths[0], data, structure=structure)
            return scan_files(paths, data, structure=structure)

        plans = rescan()
        written_paths: set[str] = set()
        backed_up: set[str] = set()
        for _pass in range(4):
            if not plans:
                break
            wrote_any = False
            for plan in plans:
                if apply_plan(
                    plan,
                    data,
                    store_dir=default_backups_dir(),
                    mods_dir=Path(mods_dir),
                    log=log,
                    structure=structure,
                    suggestions=plan.suggestions,
                    backup=plan.path not in backed_up,
                ):
                    written_paths.add(plan.path)
                    backed_up.add(plan.path)
                    wrote_any = True
            if not wrote_any:
                break
            plans = rescan()
        if is_dir:
            tables = load_blend_remaps()
            for target in scan_blend_targets(paths[0], tables):
                apply_remap(target, default_backups_dir(), Path(mods_dir), log=log)
            for target in scan_blend_vote_targets(paths[0], data.dumps, tables):
                apply_blend_vote_remap(
                    target, default_backups_dir(), Path(mods_dir), log=log
                )
            for target in scan_texcoord_targets(paths[0], buffer_gate(data)):
                apply_upgrade(
                    target,
                    default_backups_dir(),
                    Path(mods_dir),
                    log=log,
                    dumps=data.dumps,
                )
        return variant, plans, len(written_paths)

    return TaskWorker(job, log_kwarg="log", parent=parent)


def backup_chains_worker(
    mods_dir: Path | str, root: Scope, parent: QObject | None = None
) -> TaskWorker:
    """Enumerate fix-backup chains in the app-local store for a scope.

    A Path/str scope restricts the chains to that directory's subtree; ``done``
    carries dict[Path, backup chain mapping] either way.
    """
    mods_dir = Path(mods_dir)
    store_dir = default_backups_dir()
    is_dir, paths = _as_paths(root)
    if is_dir:
        scope = paths[0]
        subtree = None if scope.resolve() == mods_dir.resolve() else scope
        return TaskWorker(
            collect_backup_chains, mods_dir, store_dir, subtree, parent=parent
        )
    return TaskWorker(
        collect_backup_chains_for, paths, mods_dir, store_dir, parent=parent
    )


def revert_worker(
    choices: list[tuple[Path, Path]],
    mods_dir: str | Path | None = None,
    store_dir: Path | None = None,
    parent: QObject | None = None,
) -> TaskWorker:
    """Restore live files from chosen backups; ``done`` carries the count restored.

    Pre-validates that every chosen backup still exists before restoring
    anything, so a stale dialog cannot half-apply.

    With ``mods_dir`` and ``store_dir`` both given, the restore is followed by:

    - pruning texcoord-upgrade markers whose live buffer was restored
      byte-identical to its pre-upgrade content;
    - pruning blend-remap markers restored to their pre-remap bytes;
    - pruning store folders left empty by the consumed backups.
    """

    def job(log: LogFn) -> int:
        missing = [str(b) for _, b in choices if not Path(b).exists()]
        if missing:
            raise ValueError("backup no longer exists: " + "; ".join(missing))
        restored = revert_backups(choices, log=log)
        if mods_dir is not None and store_dir is not None:
            pruned = prune_texcoord_markers(
                store_dir, Path(mods_dir), [live for live, _backup in choices]
            )
            if pruned:
                log(f"Texcoord-upgrade markers cleared: {pruned} reverted buffer(s)")
            pruned_blends = prune_blend_markers(
                store_dir, Path(mods_dir), [live for live, _backup in choices]
            )
            if pruned_blends:
                log(f"Blend-remap markers cleared: {pruned_blends} reverted buffer(s)")
            pruned_folders = prune_empty_store_folders(store_dir, Path(mods_dir))
            if pruned_folders:
                log(f"Empty backup folders cleared: {pruned_folders} folder(s)")
        return restored

    return TaskWorker(job, log_kwarg="log", parent=parent)


def import_archive_worker(
    archive: Path,
    destination: Path,
    *,
    replace: bool = False,
    parent: QObject | None = None,
) -> TaskWorker:
    """Extract one mod archive in the background; ``done`` carries the file count."""

    def job(log: LogFn) -> int:
        return extract_archive(Path(archive), Path(destination), replace=replace, log=log)

    return TaskWorker(job, log_kwarg="log", parent=parent)


def apply_preset_worker(
    changes: Sequence[tuple[ModNode, bool]],
    parent: QObject | None = None,
) -> TaskWorker:
    """Toggle mods to a preset's enabled states; ``done`` carries the count applied."""
    changes = [(mod, enabled) for mod, enabled in changes]

    def job(log: LogFn) -> int:
        applied = 0
        for mod, enabled in changes:
            set_mod_enabled(mod.path, enabled)
            log(f"{'enabled' if enabled else 'disabled'} {mod.name}")
            applied += 1
        return applied

    return TaskWorker(job, log_kwarg="log", parent=parent)


def rename_folder_worker(
    mods_dir: Path | str,
    old_path: Path | str,
    new_name: str,
    kind: str,
    parent: QObject | None = None,
) -> TaskWorker:
    """Rename a mod or category folder on disk; presets, backups and blend markers follow.

    ``kind`` selects the stored-entry form ("mod" leaf-cleaned, "category" on-disk); ``done`` carries the new Path.
    """

    def job(log: LogFn) -> Path:
        mods = Path(mods_dir)
        old = Path(old_path)
        new = rename_mod_folder(old, new_name)
        log(f"Renamed '{old.name}' -> '{new.name}'")
        rel_old = old.relative_to(mods)
        rel_new = new.relative_to(mods)
        parent_rel = rel_old.parent
        prefix = "" if parent_rel == Path(".") else parent_rel.as_posix() + "/"
        if kind == "mod":
            old_entry = prefix + old.name.removeprefix(_DISABLED_PREFIX)
            new_entry = prefix + new.name.removeprefix(_DISABLED_PREFIX)
        else:
            old_entry = prefix + old.name
            new_entry = prefix + new.name
        changed = retarget_preset_paths([(old_entry, new_entry)])
        if changed:
            log(f"Presets updated: {changed} path(s) now point at '{new.name}'")
        promoted_changed = retarget_promoted_paths([(old_entry, new_entry)])
        if promoted_changed:
            log(
                f"Preview images updated: {promoted_changed} path(s) "
                f"now point at '{new.name}'"
            )
        moved = retarget_store_folder(
            default_backups_dir(), mods, rel_old, rel_new
        )
        log(
            f"Backup history {'moved' if moved else 'kept in place'} "
            f"for '{new.name}'"
        )
        canonical_old = "/".join(
            part.removeprefix(_DISABLED_PREFIX) for part in rel_old.parts
        )
        canonical_new = "/".join(
            part.removeprefix(_DISABLED_PREFIX) for part in rel_new.parts
        )
        rewritten = rewrite_blend_state_keys(
            default_backups_dir(), mods, canonical_old, canonical_new
        )
        if rewritten:
            log(f"Blend-remap markers updated: {rewritten} key(s)")
        texcoord_rewritten = rewrite_texcoord_state_keys(
            default_backups_dir(), mods, canonical_old, canonical_new
        )
        if texcoord_rewritten:
            log(f"Texcoord-upgrade markers updated: {texcoord_rewritten} key(s)")
        return new

    return TaskWorker(job, log_kwarg="log", parent=parent)


def delete_folder_worker(
    mods_dir: Path | str,
    node_path: Path | str,
    kind: str,
    parent: QObject | None = None,
) -> TaskWorker:
    """Delete a mod or category folder; presets, backup history and markers follow.

    ``kind`` is the ModNode kind ("mod" or "category"); mods use the stored
    DISABLED_-leaf-cleaned entry form, categories keep their on-disk name.
    ``done`` carries (deleted_path, file_count).
    """

    def job(log: LogFn) -> tuple[Path, int]:
        mods = Path(mods_dir)
        old = Path(node_path)
        rel = old.relative_to(mods)
        try:
            file_count = sum(1 for item in old.rglob("*") if item.is_file())
        except OSError:
            file_count = 0
        shutil.rmtree(old)
        log(f"Deleted '{old.name}' ({file_count} file(s))")
        parent_rel = rel.parent
        entry_prefix = "" if parent_rel == Path(".") else parent_rel.as_posix() + "/"
        if kind == "mod":
            old_entry = entry_prefix + old.name.removeprefix(_DISABLED_PREFIX)
        else:
            old_entry = entry_prefix + old.name
        removed = remove_preset_paths([old_entry])
        if removed:
            log(f"Presets updated: {removed} path(s) removed")
        promoted_removed = remove_promoted_paths([old_entry])
        if promoted_removed:
            log(f"Preview images removed: {promoted_removed} path(s)")
        if delete_store_folder(default_backups_dir(), mods, rel):
            log(f"Backup history purged for '{old.name}'")
        canonical = "/".join(
            part.removeprefix(_DISABLED_PREFIX) for part in rel.parts
        )
        removed_markers = remove_blend_state_keys(
            default_backups_dir(), mods, canonical
        )
        if removed_markers:
            log(f"Blend-remap markers removed: {removed_markers} key(s)")
        removed_texcoord_markers = remove_texcoord_state_keys(
            default_backups_dir(), mods, canonical
        )
        if removed_texcoord_markers:
            log(f"Texcoord-upgrade markers removed: {removed_texcoord_markers} key(s)")
        return old, file_count

    return TaskWorker(job, log_kwarg="log", parent=parent)
