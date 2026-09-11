"""Background task workers for the ZZZ Hash Fixer GUI.

``TaskWorker`` runs a plain function on a ``QThread`` so the GUI never blocks;
thin factory helpers prebind the engine calls the main window needs.
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Union

from PySide6.QtCore import QObject, QThread, Signal

from ..backups import (
    collect_backup_chains,
    collect_backup_chains_for,
    default_backups_dir,
    included_ini_files,
)
from ..blend_remap import apply_remap, load_blend_remaps, scan_blend_targets
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
from ..mods import analyze_mods
from ..repo import (
    DEFAULT_VARIANT,
    REPO_VARIANTS,
    RepoError,
    changelog_path,
    default_cache_dir,
    ensure_repo,
    repo_head,
    repo_update_available,
)
from ..structure import StructureData, build_structure

LogFn = Callable[[str], None]

_UPDATE_BUTTON = "Update hashes"
"""The data button's label, for error hints."""


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
            "ZZZHashFix's runtime files were cleaned from the temp folder while "
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
        try:
            result = self._fn(*self._args, **kwargs)
        except Exception as exc:
            self.error.emit(friendly_error(exc))
            return
        self.done.emit(result)


Scope = Union[Path, str, Sequence[Path]]
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
    """Parse every repo whose changelog exists; log and skip the others."""
    datasets: dict[str, FixerData] = {}
    for variant, cache in repo_dirs.items():
        if not changelog_path(cache).exists():
            log(f"No {variant} data cloned yet — click '{_UPDATE_BUTTON}' to fetch it.")
            continue
        datasets[variant] = load_fixer_data(cache, include_pcdata=(variant == "2048p"))
    return datasets


def update_data_worker(parent: QObject | None = None) -> TaskWorker:
    """Worker that ensures every variant repo, then parses the available ones.

    Unreachable variants are logged and skipped; ``done`` carries
    ``(repo_dirs, datasets, heads, structure)``.
    """

    def job(
        log: LogFn,
    ) -> tuple[dict[str, Path], dict[str, FixerData], dict[str, str], StructureData | None]:
        repo_dirs: dict[str, Path] = {}
        last_error: Exception | None = None
        for variant in REPO_VARIANTS:
            try:
                repo_dirs[variant] = ensure_repo(variant=variant, log=log)
            except RepoError as exc:
                log(f"Could not update {variant} data: {exc}")
                last_error = exc
        datasets = _parse_available(repo_dirs, log)
        if not datasets:
            if last_error is not None:
                raise last_error
            raise ValueError(
                f"no local hash data — click '{_UPDATE_BUTTON}' to download it first."
            )
        heads = {variant: repo_head(cache) for variant, cache in repo_dirs.items()}
        structure = structure_for(datasets)
        return repo_dirs, datasets, heads, structure

    return TaskWorker(job, log_kwarg="log", parent=parent)


def load_all_data_worker(parent: QObject | None = None) -> TaskWorker:
    """Worker that parses every locally cloned variant repo, no network access.

    A variant without a local clone is logged as a hint to update; ``done``
    carries ``(repo_dirs, datasets, heads, structure)`` like ``update_data_worker``.
    """

    def job(
        log: LogFn,
    ) -> tuple[dict[str, Path], dict[str, FixerData], dict[str, str], StructureData | None]:
        caches = {variant: default_cache_dir(variant) for variant in REPO_VARIANTS}
        datasets = _parse_available(caches, log)
        if not datasets:
            raise ValueError(
                f"no local hash data — click '{_UPDATE_BUTTON}' to download it first."
            )
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
    scopes then remap bound blend buffers using data/blend_remaps.json with store
    backups; ``done`` carries ``(variant, plans, written)``.
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
                f"{variant} hash data is not loaded — click 'Update hashes' first."
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
    choices: list[tuple[Path, Path]], parent: QObject | None = None
) -> TaskWorker:
    """Restore live files from chosen backups; ``done`` carries the count restored.

    Pre-validates that every chosen backup still exists before restoring
    anything, so a stale dialog cannot half-apply.
    """

    def job(log: LogFn) -> int:
        missing = [str(b) for _, b in choices if not Path(b).exists()]
        if missing:
            raise ValueError("backup no longer exists: " + "; ".join(missing))
        return revert_backups(choices, log=log)

    return TaskWorker(job, log_kwarg="log", parent=parent)
