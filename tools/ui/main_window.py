"""Main window of the ZZZ Hash Fixer GUI.

Selecting a mod, subfolder, or .ini file in the mods overview enables
per-scope "Fix" and "Revert" over the fix-backup history.
"""

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import cast

from PySide6.QtCore import QPoint, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..backups import BackupChain, default_backups_dir, store_folder_to_open
from ..fixer import STRUCTURAL_KINDS, FixerData
from ..mods import (
    AnalysisSummary,
    CURRENT_SIGNAL,
    UPDATES_SIGNAL,
    STRUCTURAL_SIGNAL,
    ModNode,
    ModVersion,
    aggregate_updates,
    analyze_scope,
    retarget_subtree_paths,
    set_mod_enabled,
)
from ..repo import REPO_VARIANTS, changelog_path, default_cache_dir, project_root
from ..structure import StructureData
from .worker import (
    _UPDATE_BUTTON,
    TaskWorker,
    analyze_worker,
    backup_chains_worker,
    check_updates_worker,
    fix_mod_worker,
    load_all_data_worker,
    revert_worker,
    update_data_worker,
)

_TREE_COLUMNS = ["Mod", "Updates", "Hashes", "Enable"]
_TREE_COLUMN_WIDTHS = ((0, 240), (1, 150), (2, 210), (3, 70))

COLOR_UPDATED = QColor("#c47f00")
COLOR_CURRENT = QColor(Qt.GlobalColor.darkGreen)
COLOR_STRUCTURAL = QColor("#808080")

_ACTIVE_WORKERS: set[TaskWorker] = set()


def _forget_worker(worker: TaskWorker) -> None:
    """Drop the keep-alive reference to a finished background worker."""
    _ACTIVE_WORKERS.discard(worker)


def updates_text(version: ModVersion, as_mod: bool) -> str:
    """Updates-column text for a node with a version: a tri-state update signal.

    Hash-outdated shows "updates available", structural-only "structural";
    a file with no known hashes shows nothing while a mod with none shows "unknown".
    """
    if version.outdated_count > 0:
        return UPDATES_SIGNAL
    if version.structural_count > 0:
        return STRUCTURAL_SIGNAL
    if version.current_count > 0:
        return CURRENT_SIGNAL
    return "unknown" if as_mod else ""


def hashes_text(version: ModVersion, as_mod: bool) -> str:
    """Hashes-column text for a node with a version; empty when nothing was hashed."""
    if version.total == 0:
        return ""
    counts = (
        f"{version.outdated_count} to update, {version.current_count} current, "
        f"{version.unknown_count} unknown · {version.total} hashes"
    )
    if version.structural_count > 0:
        counts += f" · {version.structural_count} structural"
    return f"mod · {counts}" if as_mod else counts


def wrap_tooltip(text: str, width: int = 70) -> str:
    """Word-wrap text to ``width`` columns, preserving existing breaks."""
    wrapped: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            wrapped.append("")
            continue
        line = ""
        for word in paragraph.split(" "):
            if line and len(line) + 1 + len(word) > width:
                wrapped.append(line)
                line = word
            elif line:
                line += " " + word
            else:
                line = word
        wrapped.append(line)
    return "\n".join(wrapped)


def updates_tooltip(signal: str) -> str:
    """Explanation text for one Updates-column signal; empty for a blank cell."""
    if signal == CURRENT_SIGNAL:
        text = (
            "All hashes this mod references match the current hash table - "
            "nothing to fix."
        )
    elif signal == UPDATES_SIGNAL:
        text = (
            "Some referenced hashes are from an older game version. 'Fix' renames "
            "them (and re-maps toggle offsets) to the current version."
        )
    elif signal == STRUCTURAL_SIGNAL:
        text = (
            "Hashes are current, but the mod needs structural fixes: sections must "
            "be inserted, duplicated or added to match the current rendering, not "
            "just renamed. 'Fix' can apply them."
        )
    elif signal == "unknown":
        text = (
            "No referenced hash is in the loaded hash table - the version cannot "
            "be determined."
        )
    else:
        return ""
    return wrap_tooltip(text)


def hashes_tooltip(version: ModVersion) -> str:
    """Explanation of the Hashes-cell counts; empty when nothing was hashed."""
    if version.total == 0:
        return ""
    lines = [
        f"{version.outdated_count} outdated - older-version hashes 'Fix' can rename.",
        f"{version.current_count} current - already matching the hash table.",
        f"{version.unknown_count} unknown - not present in the loaded table.",
        f"{version.total} hashes total.",
    ]
    if version.structural_count > 0:
        lines.append(
            f"{version.structural_count} structural - sections need "
            "insert/duplicate/add edits, not just renames; 'Fix' can apply them."
        )
    return wrap_tooltip("\n".join(lines))


def updates_color(version: ModVersion, as_mod: bool) -> QColor | None:
    """Updates-column foreground for the update signal: amber, gray, green, or gray."""
    if version.outdated_count > 0:
        return COLOR_UPDATED
    if version.structural_count > 0:
        return COLOR_STRUCTURAL
    if version.current_count > 0:
        return COLOR_CURRENT
    return QColor("#808080") if as_mod else None


def _analysis_log_line(summary: AnalysisSummary) -> str:
    """One-line log summary of a mods analysis run."""
    line = (
        f"Analyzed {summary.mods} mod(s): {summary.current} up to date, "
        f"{summary.outdated} with updates available, {summary.unknown} unknown · "
        f"scanned {summary.files_scanned} file(s), "
        f"skipped {summary.backups_skipped} backup(s)"
    )
    if summary.variants:
        detected = ", ".join(
            f"{count}× {variant}" for variant, count in sorted(summary.variants.items())
        )
        line += f" · detected: {detected}"
    if summary.structural:
        line += f" · {summary.structural} with structural fixes"
    return line


def update_button_state(
    statuses: Mapping[str, bool | None], pending: bool
) -> tuple[bool, str]:
    """(enabled, label) for the data-update button from the upstream checks.

    Pending shows a disabled "Checking..."; any True enables "Update hashes", all
    False grays it as "Hashes updated", and anything else grays it as "Update unavailable".
    """
    if pending:
        return False, "Checking..."
    if any(status is True for status in statuses.values()):
        return True, _UPDATE_BUTTON
    if statuses and all(status is False for status in statuses.values()):
        return False, "Hashes updated"
    return False, "Update unavailable"


def _stamp_text(stamp: int) -> str:
    """Human-readable local time for a fix backup's UTC-millisecond stamp."""
    return datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d %H:%M")


class _RevertDialog(QDialog):
    """Pick, per file with fix history, the backup state to restore it to.

    Each row's combo offers "Leave as is" plus one entry per backup, newest fix
    first down to the original pre-fix content.
    """

    def __init__(
        self, chains: Iterable[BackupChain], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Revert fix history")
        ordered = sorted(chains, key=lambda chain: str(chain.live))
        self._lives = [chain.live for chain in ordered]

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"{len(ordered)} file(s) with fix history — "
                "pick the state to restore each to.",
                self,
            )
        )

        self._table = QTableWidget(len(ordered), 2, self)
        self._table.setHorizontalHeaderLabels(["File", "Restore to"])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setWordWrap(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        for row, chain in enumerate(ordered):
            file_item = QTableWidgetItem(chain.live.name)
            file_item.setToolTip(str(chain.live))
            self._table.setItem(row, 0, file_item)
            combo = QComboBox(self)
            combo.addItem("Leave as is", None)
            newest_first = list(reversed(chain.backups))
            for position, (stamp, backup) in enumerate(newest_first):
                level = position + 1
                state = "original" if position == len(newest_first) - 1 else f"undo {level} fix(es)"
                combo.addItem(f"{state} — before {_stamp_text(stamp)}", backup)
            self._table.setCellWidget(row, 1, combo)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        all_btn = QPushButton("Set all: undo 1 fix", self)
        all_btn.clicked.connect(self._set_all_undo_one)
        buttons.addWidget(all_btn)
        buttons.addStretch(1)
        ok_btn = QPushButton("OK", self)
        ok_btn.clicked.connect(self.accept)
        buttons.addWidget(ok_btn)
        cancel_btn = QPushButton("Cancel", self)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

    def _set_all_undo_one(self) -> None:
        """Point every combo at its first non-leave option (index 1)."""
        for row in range(self._table.rowCount()):
            combo = self._table.cellWidget(row, 1)
            if not isinstance(combo, QComboBox):
                continue
            if combo.count() > 1:
                combo.setCurrentIndex(1)

    def selected_choices(self) -> list[tuple[Path, Path]]:
        """(live, backup) pairs for every row whose combo selects a backup."""
        choices: list[tuple[Path, Path]] = []
        for row in range(self._table.rowCount()):
            combo = self._table.cellWidget(row, 1)
            if not isinstance(combo, QComboBox):
                continue
            backup = combo.currentData()
            if isinstance(backup, Path):
                choices.append((self._lives[row], backup))
        return choices


class MainWindow(QMainWindow):
    """Choose a mods folder, update the hash data, then fix/revert per mod or file."""

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, FixerData] | None = None
        self._repo_dirs: dict[str, Path] | None = None
        self._structure: StructureData | None = None
        self._worker: TaskWorker | None = None
        self._selected_node: ModNode | None = None
        self._fixing_name = ""
        self._fixing_node: ModNode | None = None
        self._node_items: dict[int, QTreeWidgetItem] = {}
        self._check_worker: TaskWorker | None = None
        self._update_statuses: dict[str, bool | None] | None = None
        self._loaded_via_update = False
        self._filling_tree = False
        self._check_retry_timer = QTimer(self)
        self._check_retry_timer.setSingleShot(True)
        self._check_retry_timer.timeout.connect(self._check_hash_updates)

        self.setWindowTitle("ZZZ Hash Fixer")
        self._build_ui()
        self.resize(900, 600)
        self.setMinimumSize(640, 420)
        self._refresh_actions()
        for variant in REPO_VARIANTS:
            self._append_log(
                f"Upstream: {REPO_VARIANTS[variant].repo_url}\n"
                f"Data repo: {default_cache_dir(variant)}"
            )
        self._settings = QSettings(
            str(project_root() / "settings.ini"), QSettings.Format.IniFormat
        )
        geometry = self._settings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        for column, default in _TREE_COLUMN_WIDTHS:
            try:
                width = int(
                    cast(int, self._settings.value(f"col_width_{column}", default))
                )
            except (TypeError, ValueError):
                width = default
            if width > 0:
                self._tree.setColumnWidth(column, width)
        if self._restore_last_folder():
            self._on_load_data()
        else:
            self._check_hash_updates()

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        mods_row = QHBoxLayout()
        self._mods_edit = QLineEdit(central)
        self._mods_edit.setReadOnly(True)
        self._mods_edit.setPlaceholderText("Select your 3DMigoto mods folder…")
        self._browse_btn = QPushButton("Browse…", central)
        mods_row.addWidget(self._mods_edit, 1)
        mods_row.addWidget(self._browse_btn)
        layout.addLayout(mods_row)

        self._progress = QProgressBar(central)
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._tree = QTreeWidget(central)
        self._tree.setColumnCount(len(_TREE_COLUMNS))
        self._tree.setHeaderLabels(_TREE_COLUMNS)
        for column, width in _TREE_COLUMN_WIDTHS:
            self._tree.setColumnWidth(column, width)
        self._tree.header().setStretchLastSection(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setWordWrap(False)
        self._tree.setIndentation(8)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self._tree, 3)
        strip = QHBoxLayout()
        self._load_btn = QPushButton("Load mods", central)
        self._update_btn = QPushButton("Update hashes", central)
        strip.addWidget(self._load_btn)
        strip.addWidget(self._update_btn)
        strip.addStretch(1)
        self._mod_label = QLabel("Select a mod or .ini file…", central)
        strip.addWidget(self._mod_label)
        self._fix_btn = QPushButton("Fix", central)
        self._revert_btn = QPushButton("Revert", central)
        strip.addWidget(self._fix_btn)
        strip.addWidget(self._revert_btn)
        layout.addLayout(strip)

        self._log_view = QPlainTextEdit(central)
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(5000)
        self._log_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        layout.addWidget(self._log_view, 1)

        self.setCentralWidget(central)

        self._browse_btn.clicked.connect(self._on_browse)
        self._load_btn.clicked.connect(self._on_load_data)
        self._update_btn.clicked.connect(self._on_update_data)
        self._fix_btn.clicked.connect(self._on_fix_mod)
        self._revert_btn.clicked.connect(self._on_revert_mod)
        self._tree.itemSelectionChanged.connect(self._on_tree_selection)
        self._tree.itemChanged.connect(self._on_mod_toggle)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._mods_edit.textChanged.connect(self._refresh_actions)

    def _on_browse(self) -> None:
        start = self._mods_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select mods folder", start)
        if not chosen:
            return
        self._mods_edit.setText(chosen)
        self._append_log(f"Mods folder: {chosen}")
        self._settings.setValue("mods_folder", chosen)
        self._settings.sync()
        if not self._data:
            self._append_log("Hash data not loaded — click 'Load mods' first.")
            return
        self._start_analyze()

    def _on_load_data(self) -> None:
        """Parse the already-cloned local data repos (no network access)."""
        if self._worker is not None:
            return
        if not any(
            changelog_path(default_cache_dir(variant)).exists()
            for variant in REPO_VARIANTS
        ):
            self._append_log(
                f"No local hash data yet — click '{_UPDATE_BUTTON}' to download it first."
            )
            QTimer.singleShot(0, self._check_hash_updates)
            return
        self._append_log("Loading hash data from local clones...")
        self._start_worker(load_all_data_worker(), self._on_data_loaded)

    def _on_update_data(self) -> None:
        """Update the data repos and parse them in one background worker."""
        if self._worker is not None:
            return
        self._loaded_via_update = True
        self._append_log("Updating ZZZ-Model-Hash data (2048p + 1024p)...")
        self._start_worker(update_data_worker(), self._on_data_loaded)

    def _check_hash_updates(self) -> None:
        """Check upstream repos for newer commits without blocking the UI.

        Runs on its own worker slot (not the busy guard) so a slow or offline
        network never stalls the app; unknown results re-check on a 60 s retry.
        """
        if self._check_worker is not None:
            return
        if self._update_statuses is None:
            self._append_log("Checking upstream hash data for updates...")
        else:
            self._append_log("Retrying upstream hash data check...")
        worker = check_updates_worker()
        self._check_worker = worker
        _ACTIVE_WORKERS.add(worker)
        self._wire_worker(
            worker,
            self._on_update_check_done,
            self._on_worker_error,
            self._on_check_finished,
        )
        worker.start()

    def _on_update_check_done(self, statuses: object) -> None:
        if not isinstance(statuses, dict):
            return
        resolved: dict[str, bool | None] = dict(statuses)
        self._update_statuses = resolved
        for variant in sorted(resolved):
            status = resolved[variant]
            if status:
                outcome = "update available"
            elif status is False:
                outcome = "up to date"
            else:
                outcome = "could not check (offline?)"
            self._append_log(f"Hash data check ({variant}): {outcome}")
        self._refresh_actions()
        if any(status is None for status in resolved.values()):
            self._check_retry_timer.start(60_000)

    def _on_check_finished(self) -> None:
        self._check_worker = None
        self._refresh_actions()

    def _on_data_loaded(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 4:
            return
        repo_dirs, datasets, heads, structure = result
        loaded = cast(dict[str, FixerData], datasets)
        self._repo_dirs = repo_dirs
        self._data = loaded
        self._structure = structure
        variants = sorted(loaded)
        for variant in variants:
            data = loaded[variant]
            self._append_log(
                f"Data loaded ({variant}): {len(data.db.characters)} characters, "
                f"{len(data.chains)} chain sources"
            )
        self._append_log(
            "Heads " + ", ".join(f"{v}={heads[v] or '?'}" for v in variants)
        )
        via_update = self._loaded_via_update
        self._loaded_via_update = False
        if via_update:
            self._update_statuses = {variant: False for variant in variants}
            self._refresh_actions()
        else:
            QTimer.singleShot(0, self._check_hash_updates)
        QTimer.singleShot(0, self._start_analyze)

    def _on_tree_selection(self) -> None:
        """Track the selected tree node and update the strip label/actions."""
        items = self._tree.selectedItems()
        node = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        self._selected_node = node
        self._update_mod_label(node)
        self._refresh_mod_actions()

    def _update_mod_label(self, node: ModNode | None) -> None:
        """Refresh the strip label for the selected node, or the placeholder."""
        self._mod_label.setText(
            "Select a mod or .ini file…"
            if node is None
            else f"{node.name} — {self._count_files(node)} file(s)"
        )

    def _on_analyze_done(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            return
        node, summary = result
        self._fill_mod_tree(node)
        self._append_log(_analysis_log_line(summary))

    def _on_fix_mod(self) -> None:
        """Scan the selected scope and apply its fixes in one background job."""
        if self._worker is not None or not self._data:
            return
        node = self._selected_node
        if node is None:
            self._append_log("Select a mod or .ini file to fix")
            return
        if (
            node.variant is not None
            and self._data is not None
            and node.variant not in self._data
        ):
            self._append_log(
                f"{node.variant} hash data is not loaded — click 'Update hashes' first."
            )
            return
        suffix = " (currently DISABLED)" if node.disabled else ""
        if not self._confirm(
            "Fix",
            f"Fix '{node.name}{suffix}'?\n"
            f"{self._count_files(node)} file(s) will be scanned; "
            f"changed files are backed up in {default_backups_dir()}.",
        ):
            self._append_log("Fix cancelled")
            return
        self._fixing_name = node.name
        self._fixing_node = node
        self._append_log(f"Fixing '{node.name}'…")
        worker = fix_mod_worker(
            self._mods_edit.text().strip(), self._scope_for(node), self._data, self._structure
        )
        self._start_worker(worker, self._on_fix_done)

    def _on_fix_done(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 3:
            return
        variant, plans, written = result
        total = sum(
            1
            for plan in plans
            for suggestion in plan.suggestions
            if suggestion.old.lower() != suggestion.new.lower()
            or suggestion.kind in STRUCTURAL_KINDS
        )
        if written == 0:
            self._append_log(f"No fixes needed for '{self._fixing_name}' ({variant})")
        else:
            self._append_log(
                f"Fixed '{self._fixing_name}' ({variant}): "
                f"wrote {written} file(s), {total} change(s)"
            )
        QTimer.singleShot(0, self._deferred_scope_refresh)

    def _on_revert_mod(self) -> None:
        """Enumerate the selected scope's fix-backup history in the background."""
        if self._worker is not None or not self._data:
            return
        node = self._selected_node
        if node is None:
            self._append_log("Select a mod or .ini file to revert")
            return
        self._fixing_name = node.name
        self._fixing_node = node
        self._append_log(f"Checking fix history for '{node.name}'…")
        self._start_worker(
            backup_chains_worker(self._mods_edit.text().strip(), self._scope_for(node)),
            self._on_chains_done,
        )

    def _on_chains_done(self, chains: object) -> None:
        if not isinstance(chains, dict):
            return
        chain_list = list(chains.values())
        if not chain_list:
            self._append_log(f"Nothing to revert in '{self._fixing_name}'")
            return
        dialog = _RevertDialog(chain_list, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choices = dialog.selected_choices()
        if not choices:
            self._append_log("No states selected for restore")
            return
        self._append_log(f"Restoring {len(choices)} file(s)…")
        self._start_worker(revert_worker(choices), self._on_revert_done)

    def _on_revert_done(self, count: object) -> None:
        if not isinstance(count, int):
            return
        self._append_log(f"Restored {count} file(s) in '{self._fixing_name}'")
        QTimer.singleShot(0, self._deferred_scope_refresh)

    def _on_worker_error(self, message: str) -> None:
        self._append_log(f"ERROR: {message}")

    def _on_worker_finished(self) -> None:
        """Worker thread done: drop the reference and re-enable the UI."""
        self._worker = None
        self._loaded_via_update = False
        self._set_busy(False)

    @staticmethod
    def _scope_for(node: ModNode) -> Path | list[Path]:
        """Directory path for dir-ish nodes; [file path] for single file nodes."""
        return [node.path] if node.kind == "file" else node.path

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _start_worker(self, worker: TaskWorker, on_done: Callable[[object], None]) -> None:
        """Start a background task and disable actions until it finishes."""
        self._worker = worker
        _ACTIVE_WORKERS.add(worker)
        self._wire_worker(worker, on_done, self._on_worker_error, self._on_worker_finished)
        self._set_busy(True)
        worker.start()

    def _wire_worker(
        self,
        worker: TaskWorker,
        on_done: Callable[[object], None],
        on_error: Callable[[str], None],
        on_finished: Callable[[], None],
    ) -> None:
        """Connect the shared worker signals (log, done, error, finished)."""
        worker.log.connect(self._append_log)
        worker.done.connect(on_done)
        worker.error.connect(on_error)
        worker.finished.connect(on_finished)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(partial(_forget_worker, worker))

    def _set_busy(self, busy: bool) -> None:
        self._progress.setVisible(busy)
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        busy = self._worker is not None
        self._browse_btn.setEnabled(not busy)
        self._load_btn.setEnabled(not busy)
        pending = self._update_statuses is None or self._check_worker is not None
        enabled, label = update_button_state(
            self._update_statuses or {}, pending
        )
        self._update_btn.setText(label)
        self._update_btn.setEnabled(enabled and not busy)
        self._refresh_mod_actions()

    def _refresh_mod_actions(self) -> None:
        """Enable Fix/Revert only with a loaded scope: data, folder, selection."""
        ready = (
            self._worker is None
            and bool(self._data)
            and bool(self._mods_edit.text().strip())
            and self._selected_node is not None
        )
        self._fix_btn.setEnabled(ready)
        self._revert_btn.setEnabled(ready)

    @staticmethod
    def _count_files(node: ModNode) -> int:
        """Number of .ini file nodes in node's subtree, including node itself."""
        count = 0
        stack = [node]
        while stack:
            current = stack.pop()
            if current.kind == "file":
                count += 1
            stack.extend(current.children)
        return count

    def _start_analyze(self) -> None:
        """Auto-analyze the current mods folder into the mods overview."""
        if self._worker is not None or not self._data:
            return
        mods_dir = self._mods_edit.text().strip()
        if not mods_dir:
            return
        self._append_log("Analyzing mods…")
        self._start_worker(
            analyze_worker(mods_dir, self._data, self._structure), self._on_analyze_done
        )

    def _restore_last_folder(self) -> bool:
        """Restore the last used mods folder from settings; True when restored."""
        saved = self._settings.value("mods_folder", "")
        if not isinstance(saved, str) or not saved:
            return False
        if not Path(saved).is_dir():
            self._append_log(f"Saved mods folder no longer exists: {saved}")
            return False
        self._mods_edit.setText(saved)
        self._append_log(f"Mods folder (restored): {saved}")
        return True

    def _fill_mod_tree(self, root_node: ModNode) -> None:
        """Populate the mods overview; the mods root's children are the top level."""
        self._node_items.clear()
        self._tree.clear()
        self._filling_tree = True
        try:
            for child in root_node.children:
                self._add_mod_item(self._tree.invisibleRootItem(), child)
        finally:
            self._filling_tree = False

    @staticmethod
    def _apply_version_row(node: ModNode, item: QTreeWidgetItem) -> None:
        """Updates/hashes cells for a node with a version; no-op when unversioned."""
        version = node.version
        if version is None:
            return
        as_mod = node.kind == "mod"
        updates = updates_text(version, as_mod)
        hashes = hashes_text(version, as_mod)
        item.setText(1, updates)
        item.setText(2, hashes)
        item.setToolTip(1, updates_tooltip(updates))
        item.setToolTip(2, hashes_tooltip(version) if hashes else "")
        color = updates_color(version, as_mod)
        if color is not None:
            item.setForeground(1, color)
        else:
            item.setData(1, Qt.ItemDataRole.ForegroundRole, None)

    @staticmethod
    def _apply_aggregate_row(node: ModNode, item: QTreeWidgetItem) -> None:
        """Updates cell for an unversioned node from its children's signal; empty clears color."""
        signal = aggregate_updates(node)
        item.setText(1, signal)
        item.setToolTip(1, updates_tooltip(signal))
        if signal == UPDATES_SIGNAL:
            item.setForeground(1, COLOR_UPDATED)
        elif signal == STRUCTURAL_SIGNAL:
            item.setForeground(1, COLOR_STRUCTURAL)
        elif signal:
            item.setForeground(1, COLOR_CURRENT)
        else:
            item.setData(1, Qt.ItemDataRole.ForegroundRole, None)

    @staticmethod
    def _apply_mod_tooltip(node: ModNode, item: QTreeWidgetItem) -> None:
        """Column-0 tooltip for a mod: its path plus any breaking-change note."""
        tooltip = str(node.path)
        version = node.version
        if version is not None and version.breaks_label:
            tooltip += f"\nBreaking change: {version.breaks_label}"
        item.setToolTip(0, tooltip)

    def _add_mod_item(self, parent_item: QTreeWidgetItem, node: ModNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.name, "", "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        if node.kind == "mod":
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                3, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
        if node.version is not None:
            self._apply_version_row(node, item)
        else:
            self._apply_aggregate_row(node, item)
        if node.kind == "mod":
            self._apply_mod_tooltip(node, item)
        self._node_items[id(node)] = item
        parent_item.addChild(item)
        for child in node.children:
            self._add_mod_item(item, child)
        return item

    def _deferred_scope_refresh(self) -> None:
        self._refresh_scope(self._fixing_node)

    def _refresh_scope(self, node: ModNode | None) -> None:
        """Re-analyze one mod/category subtree in place and refresh its tree rows."""
        if self._worker is not None or not self._data or node is None:
            return
        if node.kind == "root":
            self._start_analyze()
            return
        scope = node
        if node.kind in ("file", "subfolder"):
            item = self._node_items.get(id(node))
            parent_item = item.parent() if item is not None else None
            while parent_item is not None:
                ancestor = parent_item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(ancestor, ModNode) and ancestor.kind == "mod":
                    scope = ancestor
                    break
                parent_item = parent_item.parent()
            if scope is node:
                self._start_analyze()
                return
        try:
            analyze_scope(scope, self._data, self._structure)
        except Exception as exc:
            self._append_log(f"ERROR: {exc}")
            self._start_analyze()
            return
        stack = [scope]
        while stack:
            current = stack.pop()
            item = self._node_items.get(id(current))
            if item is not None:
                if current.version is not None:
                    self._apply_version_row(current, item)
                else:
                    self._apply_aggregate_row(current, item)
                if current.kind == "mod":
                    self._apply_mod_tooltip(current, item)
            stack.extend(current.children)
        scope_item = self._node_items.get(id(scope))
        if scope_item is None:
            return
        parent_item = scope_item.parent()
        while parent_item is not None:
            ancestor = parent_item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(ancestor, ModNode):
                if ancestor.version is not None:
                    self._apply_version_row(ancestor, parent_item)
                else:
                    self._apply_aggregate_row(ancestor, parent_item)
            parent_item = parent_item.parent()

    def _on_mod_toggle(self, item: QTreeWidgetItem, column: int) -> None:
        """Enable or disable the clicked mod by renaming its folder on disk."""
        if column != 3 or self._filling_tree:
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(node, ModNode) or node.kind != "mod":
            return
        self._tree.setCurrentItem(item)
        desired_enabled = item.checkState(3) == Qt.CheckState.Checked
        if desired_enabled != node.disabled:
            return
        if self._worker is not None:
            item.setCheckState(
                3, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
            self._append_log("Busy — try again when the current task finishes.")
            return
        try:
            new_path = set_mod_enabled(node.path, desired_enabled)
        except FileExistsError:
            self._append_log(
                f"Cannot {'enable' if desired_enabled else 'disable'} "
                f"'{node.name}': target folder already exists"
            )
            item.setCheckState(
                3, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
        except OSError as exc:
            self._append_log(f"ERROR: {exc}")
            item.setCheckState(
                3, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
        else:
            node.name = new_path.name
            node.disabled = not desired_enabled
            item.setText(0, node.name)
            self._update_mod_label(node)
            self._append_log(
                f"{'Enabled' if desired_enabled else 'Disabled'} '{node.name}'"
            )
            retarget_subtree_paths(node, new_path)
            self._fixing_node = node
            QTimer.singleShot(0, self._deferred_scope_refresh)

    def _on_tree_context_menu(self, position: QPoint) -> None:
        """Right-click menu on a mod row: open the mod or its backup store folder."""
        item = self._tree.itemAt(position)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(data, ModNode) or data.kind != "mod":
            return
        menu = QMenu(self)
        open_mod = menu.addAction("Open mod folder")
        open_backup = menu.addAction("Open backup folder")
        chosen = menu.exec(self._tree.viewport().mapToGlobal(position))
        if chosen is open_mod:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(data.path)))
        elif chosen is open_backup:
            target = store_folder_to_open(
                default_backups_dir(),
                Path(self._mods_edit.text().strip()),
                data.path,
            )
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _append_log(self, message: str) -> None:
        self._log_view.appendPlainText(message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.setValue("window_geometry", self.saveGeometry())
        for column, _ in _TREE_COLUMN_WIDTHS:
            self._settings.setValue(f"col_width_{column}", self._tree.header().sectionSize(column))
        self._settings.sync()
        self._check_retry_timer.stop()
        for worker in (self._worker, self._check_worker):
            if worker is not None and worker.isRunning():
                worker.wait()
        super().closeEvent(event)
