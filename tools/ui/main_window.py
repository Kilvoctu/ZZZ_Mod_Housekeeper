"""Main window of the ZZZ Mod Housekeeper GUI.

Right-clicking a mod, subfolder, or .ini file in the mods overview offers
"Fix" and "Revert" over the fix-backup history from the context menu.
"""

import base64
import ctypes
from collections import deque
import json
import math
import random
import re
import shutil
import sys

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import cast

# noinspection PyPackageRequirements
from PySide6.QtCore import (
    QAbstractItemModel,
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QModelIndex,
    QMimeData,
    QPersistentModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl)
# noinspection PyPackageRequirements
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QEnterEvent,
    QFontDatabase,
    QGradient,
    QGuiApplication,
    QHelpEvent,
    QIcon,
    QImage,
    QImageReader,
    QImageWriter,
    QKeyEvent,
    QKeySequence,
    QLinearGradient,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut)
# noinspection PyPackageRequirements
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..backups import BackupChain, collect_backup_chains_for, default_backups_dir, is_backup_name, is_backup_path, store_folder_for_mod, store_folder_to_open
from ..blend_remap import blend_marker_kind
from ..fixer import STRUCTURAL_KINDS, FixerData, empty_fixer_data, read_ini_text
from ..mods import (
    AnalysisSummary,
    BROKEN_BUFFERS_SIGNAL,
    CURRENT_SIGNAL,
    OLD_HASH_SIGNAL,
    SECTIONS_SIGNAL,
    ModNode,
    ModVersion,
    aggregate_updates,
    analyze_scope,
    create_mod_folder,
    retarget_subtree_paths,
    set_mod_enabled,
)
from ..modinfo import read_mod_author, scan_mod_info
from ..presets import (_clean_disabled_leaf, delete_preset, enabled_relative_paths, load_presets, missing_preset_paths, mod_relative_paths, preset_changes, preset_names, remove_preset_paths, rename_preset, retarget_preset_paths, save_preset)
from ..promoted import canonical_key, load_promoted, remove_promoted_paths, retarget_promoted_paths, save_promoted
from ..repo import DEFAULT_VARIANT, REPO_VARIANTS, default_cache_dir, project_root
from ..state import load_state, save_state
from ..structure import StructureData
from ..texcoord_upgrade import marker_kind
from .worker import (
    _UPDATE_BUTTON,
    TaskWorker,
    analyze_worker,
    apply_preset_worker,
    backup_chains_worker,
    check_updates_worker,
    delete_folder_worker,
    fix_mod_worker,
    import_archive_worker,
    load_all_data_worker,
    rename_folder_worker,
    revert_worker,
    update_data_worker,
)

_TREE_COLUMNS = ["Mod", "Enable", "Updates", "Hashes"]
_TREE_COLUMN_WIDTHS = ((0, 350), (1, 45), (2, 65), (3, 250))

COLOR_UPDATED = QColor("#c47f00")
COLOR_CURRENT = QColor(Qt.GlobalColor.darkGreen)
COLOR_STRUCTURAL = QColor("#808080")
COLOR_ACCENT = QColor("#5fb8ff")  # Rina-lightning accent: tree row highlight + title-bar busy wash
_DROP_TINT = QColor(
    COLOR_ACCENT.red(),
    COLOR_ACCENT.green(),
    COLOR_ACCENT.blue(),
    140,
)

_ACTIVE_WORKERS: set[TaskWorker] = set()
_DISABLED_PREFIX = "DISABLED_"
_BUSY_HOLD_MS = 600
_ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2


def _display_name(node: ModNode) -> str:
    return node.name.removeprefix(_DISABLED_PREFIX)


def _enabled_view_includes(node: ModNode) -> bool:
    """Whether the node shows in the tree when Show enabled only is on."""
    if node.kind == "mod":
        return not node.disabled
    if node.kind == "file":
        return True
    return any(_enabled_view_includes(child) for child in node.children)


def _forget_worker(worker: TaskWorker) -> None:
    """Drop the keep-alive reference to a finished background worker."""
    _ACTIVE_WORKERS.discard(worker)


def updates_text(version: ModVersion, as_mod: bool) -> str:
    """Updates-column text for a node with a version: its strongest update signal.

    buffers = missing or old-format shipped .buf/.ib; old hash = chain-fixable
    stale hashes; sections = pending structural ini insertions;
    a file with no known hashes shows nothing while a mod with none shows "unknown".
    """
    if version.broken_count > 0:
        return BROKEN_BUFFERS_SIGNAL
    if version.outdated_count > 0:
        return OLD_HASH_SIGNAL
    if version.structural_count > 0:
        return SECTIONS_SIGNAL
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
    if version.broken_count > 0:
        counts += f" · {version.broken_count} broken buffer(s)"
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
    elif signal == OLD_HASH_SIGNAL:
        text = (
            "Some referenced hashes are from an older game version. 'Fix' renames "
            "them (and re-maps toggle offsets) to the current version."
        )
    elif signal == SECTIONS_SIGNAL:
        text = (
            "Hashes are current, but the mod needs structural fixes: sections must "
            "be inserted, duplicated or added to match the current rendering, not "
            "just renamed. 'Fix' can apply them."
        )
    elif signal == BROKEN_BUFFERS_SIGNAL:
        text = (
            "Some shipped .buf/.ib files are missing or declare an old vertex "
            "format. 'Fix' can restore or convert them when the dump data "
            "covers the component."
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
    if version.broken_count > 0:
        lines.append(
            f"{version.broken_count} broken buffer(s) - missing or old-format "
            "shipped binaries; 'Fix' can restore or convert them."
        )
    return wrap_tooltip("\n".join(lines))


def updates_color(version: ModVersion, as_mod: bool) -> QColor | None:
    """Updates-column foreground for the update signal: amber, gray, green, or gray."""
    if version.broken_count > 0:
        return COLOR_UPDATED
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
        f"{summary.outdated} outdated, {summary.unknown} unknown · "
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
    if summary.broken:
        line += f" · {summary.broken} with broken buffer(s)"
    return line


def _stale_mod_refs(
    previous: frozenset[str],
    current: frozenset[str],
    promoted: dict[str, str],
    presets: dict[str, list[str]],
    dismissed: set[str],
) -> list[str]:
    """Sorted mods that vanished from the folder or whose state refs now dangle,
    and the dismissed set is honored across the whole union."""
    dangling = {
        key
        for key in list(promoted)
        + [entry for entries in presets.values() for entry in entries]
        if key not in current
    }
    return sorted(((previous - current) | dangling) - set(dismissed))


def update_button_state(
    statuses: Mapping[str, bool | None], pending: bool
) -> tuple[bool, str]:
    """(enabled, label) for the data-update button from the upstream checks.

    Pending shows a disabled "Checking..."; any True enables "Update data", all
    False grays it as "Data updated", and anything else grays it as "Update unavailable".
    """
    if pending:
        return False, "Checking..."
    if any(status is True for status in statuses.values()):
        return True, _UPDATE_BUTTON
    if statuses and all(status is False for status in statuses.values()):
        return False, "Data updated"
    return False, "Update unavailable"


def _has_hash_knowledge(datasets: Mapping[str, FixerData]) -> bool:
    """Whether any loaded dataset carries usable hash knowledge."""
    return any(
        data.chains or data.entries or data.user_patches or data.db.reverse
        for data in datasets.values()
    )


def _scope_has_backups(mods_dir: Path, store_dir: Path, scope_path: Path) -> bool:
    """Whether the fix-backup store holds any backup for the given scope.

    A single-file scope checks chains for exactly that file; a directory
    scope checks that the scope's store mirror folder exists and holds at
    least one file recursively (empty folders don't count).
    """
    if scope_path.is_file():
        return bool(collect_backup_chains_for([scope_path], mods_dir, store_dir))
    try:
        mirror = store_folder_for_mod(store_dir, mods_dir, scope_path)
    except ValueError:
        return False
    return mirror.is_dir() and any(path.is_file() for path in mirror.rglob("*"))


def _stamp_text(stamp: int) -> str:
    """Human-readable local time for a fix backup's UTC-millisecond stamp."""
    return datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d %H:%M")


def _restored_geometry(raw: object) -> QByteArray | None:
    """Decode a stored window geometry for restoreGeometry; None when unusable.

    State values are hex strings of saveGeometry() bytes; a raw QByteArray is
    passed through untouched so callers keep their not-None restore guard.
    """
    if isinstance(raw, QByteArray):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return QByteArray.fromHex(raw.encode("ascii"))
        except UnicodeEncodeError:
            return None
    return None


def _stored_size_pair(raw: object) -> list[int] | None:
    """Interpret a stored dialog size as [width, height]; None when unusable.

    State values are [width, height] lists; a legacy QSize or "@Size(w h)" text
    (hand-edited state or a not-yet-migrated ini value) is accepted as well.
    """
    if isinstance(raw, QSize):
        return [int(raw.width()), int(raw.height())]
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("@Size(") and text.endswith(")"):
            text = text[len("@Size("):-1]
        parts = text.replace("x", " ").split()
        if len(parts) == 2:
            try:
                width, height = int(parts[0]), int(parts[1])
            except ValueError:
                return None
            if width > 0 and height > 0:
                return [width, height]
        return None
    if (
        isinstance(raw, list)
        and len(raw) == 2
        and all(
            isinstance(part, int) and not isinstance(part, bool) and part > 0
            for part in raw
        )
    ):
        return [raw[0], raw[1]]
    return None


def _expanded_path_list(raw: object) -> list[str]:
    """Interpret a stored tree_expanded value as a list of mod paths.

    Tolerates a native list, a JSON-text string (the closeEvent write-out) and
    a comma-separated fallback, mirroring what the legacy ini held.
    """
    if isinstance(raw, list):
        return [str(path) for path in raw if path]
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except ValueError:
            loaded = None
        if isinstance(loaded, list):
            return [str(path) for path in loaded if path]
        return [part.strip() for part in raw.split(",") if part.strip()]
    return []


class StateSettings:
    """QSettings drop-in backed by the consolidated state.json "settings" section.

    Every value()/setValue() re-reads state.json and writes a fresh snapshot,
    so concurrent presets/promoted saves and the other StateSettings
    instances' own writes are never clobbered by a stale in-memory copy.
    Settings traffic is low-frequency (dialog open/close), so the tiny reload
    is cheap.  value() falls back to the given default for missing keys;
    JSON-encodable scalars and str/list/dict values are stored verbatim.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else project_root()

    def value(self, key: str, default: object = None) -> object:
        """QSettings-style read: the stored value, or the default when absent."""
        section = load_state(self._root).get("settings")
        if not isinstance(section, dict):
            return default
        return section.get(key, default)

    # noinspection PyPep8Naming
    def setValue(self, key: str, value: object) -> None:
        """QSettings-style write: merge the key into a fresh state and save it."""
        state = load_state(self._root)
        section = state.get("settings")
        if not isinstance(section, dict):
            section = {}
            state["settings"] = section
        section[key] = value
        try:
            save_state(state, self._root)
        except OSError:
            pass


class _RevertDialog(QDialog):
    """Pick, per file with fix history, the backup state to restore it to.

    Each row shows the file's fix kind, and its combo offers "Leave as is"
    plus one entry per backup, newest fix first down to the original pre-fix
    content.
    """

    def __init__(
        self,
        chains: Iterable[BackupChain],
        parent: QWidget | None = None,
        kinds: Mapping[Path, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Revert fix history")
        kinds = kinds if kinds is not None else {}
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

        self._table = QTableWidget(len(ordered), 3, self)
        self._table.setHorizontalHeaderLabels(["File", "Fix kind", "Restore to"])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setWordWrap(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        for row, chain in enumerate(ordered):
            file_item = QTableWidgetItem(chain.live.name)
            file_item.setToolTip(str(chain.live))
            self._table.setItem(row, 0, file_item)
            self._table.setItem(row, 1, QTableWidgetItem(kinds.get(chain.live, "")))
            combo = QComboBox(self)
            combo.addItem("Leave as is", None)
            newest_first = list(reversed(chain.backups))
            for position, (stamp, backup) in enumerate(newest_first):
                level = position + 1
                state = "original" if position == len(newest_first) - 1 else f"undo {level} fix(es)"
                combo.addItem(f"{state} — before {_stamp_text(stamp)}", backup)
            self._table.setCellWidget(row, 2, combo)
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
            combo = self._table.cellWidget(row, 2)
            if not isinstance(combo, QComboBox):
                continue
            if combo.count() > 1:
                combo.setCurrentIndex(1)

    def selected_choices(self) -> list[tuple[Path, Path]]:
        """(live, backup) pairs for every row whose combo selects a backup."""
        choices: list[tuple[Path, Path]] = []
        for row in range(self._table.rowCount()):
            combo = self._table.cellWidget(row, 2)
            if not isinstance(combo, QComboBox):
                continue
            backup = combo.currentData()
            if isinstance(backup, Path):
                choices.append((self._lives[row], backup))
        return choices


class _MissingModsDialog(QDialog):
    """Locate or clean up mods that vanished from the mod folder."""

    def __init__(
        self, mods_root: Path, missing: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._mods_root = mods_root
        self.retargeted = False
        self.dismissed: list[str] = []
        self.setWindowTitle("Missing mods")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        info = QLabel("These mods are no longer in the mods folder.", self)
        info.setWordWrap(True)
        layout.addWidget(info)
        self._list = QListWidget(self)
        for rel in missing:
            item = QListWidgetItem(rel.removeprefix(_DISABLED_PREFIX))
            item.setData(Qt.ItemDataRole.UserRole, rel)
            self._list.addItem(item)
        self._list.setCurrentRow(0)
        layout.addWidget(self._list, 1)
        footer = QHBoxLayout()
        locate = QPushButton("Locate…", self)
        locate.clicked.connect(self._locate)
        cleanup = QPushButton("Clean up", self)
        cleanup.clicked.connect(self._cleanup)
        keep = QPushButton("Keep", self)
        keep.clicked.connect(self.accept)
        footer.addWidget(locate)
        footer.addWidget(cleanup)
        footer.addStretch(1)
        footer.addWidget(keep)
        layout.addLayout(footer)

    def done(self, result: int) -> None:
        """Record every still-listed mod as dismissed, then finish."""
        self.dismissed = [
            str(self._list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self._list.count())
        ]
        super().done(result)

    def _locate(self) -> None:
        """Re-point state refs to the folder's new location inside the mods root."""
        current: QListWidgetItem | None = self._list.currentItem()
        if current is None:
            return
        rel = str(current.data(Qt.ItemDataRole.UserRole))
        chosen = QFileDialog.getExistingDirectory(
            self, "Locate mod", str(self._mods_root)
        )
        if not chosen:
            return
        new_path = Path(chosen)
        try:
            new_rel = new_path.resolve().relative_to(self._mods_root.resolve()).as_posix()
        except ValueError:
            return
        if new_rel == rel:
            return
        retarget_preset_paths([(rel, new_rel)])
        retarget_promoted_paths([(rel, new_rel)])
        self.retargeted = True
        self._list.takeItem(self._list.row(current))
        if self._list.count() == 0:
            self.accept()

    def _cleanup(self) -> None:
        """Remove state refs for one missing mod."""
        current: QListWidgetItem | None = self._list.currentItem()
        if current is None:
            return
        rel = str(current.data(Qt.ItemDataRole.UserRole))
        remove_preset_paths([rel])
        remove_promoted_paths([rel])
        self.retargeted = True
        self._list.takeItem(self._list.row(current))
        if self._list.count() == 0:
            self.accept()


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

_INI_RESOURCE_VALUE_RE = re.compile(
    r"^\s*(?:path|filename)\s*=\s*(.+?)\s*$", re.IGNORECASE
)


def _iter_image_files(mod_dir: Path) -> list[Path]:
    """Image files under mod_dir, from a stack-based recursive walk.

    Directories whose name marks a genuine fix-backup are pruned; plain
    ``DISABLED_`` folders are ordinary mod content and are searched too.
    """
    files: list[Path] = []
    stack = [mod_dir]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if not is_backup_name(child.name):
                    stack.append(child)
            elif child.suffix.lower() in _IMAGE_EXTENSIONS:
                files.append(child)
    return files


def _ini_referenced_images(mod_dir: Path) -> set[tuple[str, ...]]:
    """Case-folded mod-relative parts of images referenced by mod .ini files.

    ``path =``/``filename =`` resource values resolve against the ini's
    own directory; values outside mod_dir or game-root-relative ``$``
    paths are skipped.
    """
    referenced: set[tuple[str, ...]] = set()
    stack = [mod_dir]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if not is_backup_name(child.name):
                    stack.append(child)
                continue
            if child.suffix.lower() != ".ini":
                continue
            if is_backup_path(child.relative_to(mod_dir).parts):
                continue
            try:
                text = read_ini_text(child)
            except (ValueError, OSError):
                continue
            for line in text.splitlines():
                match = _INI_RESOURCE_VALUE_RE.match(line)
                if match is None:
                    continue
                value = match.group(1).strip().strip('"').strip()
                if value.startswith("$"):
                    continue
                resolved = child.parent / Path(value)
                try:
                    rel = resolved.relative_to(mod_dir)
                except ValueError:
                    continue
                if rel.suffix.lower() in _IMAGE_EXTENSIONS:
                    referenced.add(
                        tuple(part.casefold() for part in rel.parts)
                    )
    return referenced


def _mod_images(mod_dir: Path) -> list[Path]:
    """Unreferenced preview images under mod_dir, sorted by path.

    Images referenced by a mod .ini resource line are internal assets
    (textures, shader overlays) and are excluded; everything else counts
    as a preview regardless of location.
    """
    referenced = _ini_referenced_images(mod_dir)
    images = [
        path
        for path in _iter_image_files(mod_dir)
        if tuple(part.casefold() for part in path.relative_to(mod_dir).parts)
        not in referenced
    ]
    return sorted(images)


def _unique_destination(folder: Path, name: str) -> Path:
    """First non-existing path in folder for name, inserting ' (2)', ' (3)', ... before the suffix."""
    stem = Path(name).stem
    suffix = Path(name).suffix
    candidate = folder / name
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def _process_elevated() -> bool:
    """Whether this process is running with administrator privileges."""
    try:
        is_user_an_admin = getattr(ctypes.windll.shell32, "IsUserAnAdmin")
        return bool(is_user_an_admin())
    except (AttributeError, OSError):
        return False


def _save_jpeg(image: QImage, destination: Path) -> bool:
    """Write the image as quality-90 JPEG, flattening alpha onto white."""
    if image.isNull():
        return False
    if image.hasAlphaChannel():
        flattened = QImage(image.size(), QImage.Format.Format_RGB32)
        flattened.fill(Qt.GlobalColor.white)
        painter = QPainter(flattened)
        painter.drawImage(0, 0, image)
        painter.end()
        image = flattened
    return image.save(str(destination), quality=90)


class _KeyChip(QLabel):
    """Fixed-size keycap chip drawn with the palette's button colors."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(self.fontMetrics().horizontalAdvance(text) + 14, self.fontMetrics().height() + 8)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        pen = _dimmed_color(self.palette(), 0.45)
        brush = self.palette().color(QPalette.ColorGroup.Normal, QPalette.ColorRole.Button)
        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(self.palette().color(QPalette.ColorGroup.Normal, QPalette.ColorRole.ButtonText))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())


def _dimmed_color(palette: QPalette, amount: float) -> QColor:
    """WindowText blended toward Window by amount (0..1) for secondary text."""
    text = palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.WindowText)
    back = palette.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.Window)
    return QColor(
        round(text.red() + (back.red() - text.red()) * amount),
        round(text.green() + (back.green() - text.green()) * amount),
        round(text.blue() + (back.blue() - text.blue()) * amount),
    )


def _toggle_row(label: str, key_display: str, parent: QWidget | None = None) -> QWidget:
    """One toggle row: keycap chips joined by plus signs, then the label."""
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    tokens = [part for part in key_display.split("+") if part]
    if not tokens:
        tokens = [key_display]
    for index, token in enumerate(tokens):
        if index:
            plus = QLabel("+")
            plus_palette = plus.palette()
            plus_palette.setColor(
                QPalette.ColorRole.Mid, _dimmed_color(plus_palette, 0.4)
            )
            plus.setPalette(plus_palette)
            plus.setForegroundRole(QPalette.ColorRole.Mid)
            layout.addWidget(plus)
        layout.addWidget(_KeyChip(token))
    layout.addSpacing(12)
    layout.addWidget(QLabel(label), 1)
    return row


class _ModInfoDialog(QDialog):
    """Read-only key-toggle summary of a mod."""

    def __init__(self, mod_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Mod info — {mod_dir.name.removeprefix(_DISABLED_PREFIX)}")
        self._settings = StateSettings()
        saved = _stored_size_pair(self._settings.value("mod_info_size"))
        if saved is not None:
            self.resize(QSize(saved[0], saved[1]))
        else:
            self.resize(240, 320)
        layout = QVBoxLayout(self)
        author = read_mod_author(mod_dir)
        if author is not None:
            author_label = QLabel(f"Author: {author}")
            author_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(author_label)
        infos = scan_mod_info(mod_dir)
        if not any(info.toggles for info in infos):
            layout.addWidget(QLabel("No key toggles found."))
            layout.addStretch(1)
            return
        show_headers = sum(1 for info in infos if info.toggles) > 1
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        rows_layout = QVBoxLayout(content)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(6)
        for info in infos:
            if not info.toggles:
                continue
            if show_headers:
                header = QLabel(info.path)
                header_palette = header.palette()
                header_palette.setColor(
                    QPalette.ColorRole.Mid, _dimmed_color(header_palette, 0.4)
                )
                header.setPalette(header_palette)
                header.setForegroundRole(QPalette.ColorRole.Mid)
                rows_layout.addWidget(header)
            for toggle in info.toggles:
                rows_layout.addWidget(_toggle_row(toggle.label, toggle.key_display, content))
        rows_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    def done(self, result: int) -> None:
        """Persist the dialog size, then finish the dialog."""
        self._settings.setValue(
            "mod_info_size", [self.size().width(), self.size().height()]
        )
        super().done(result)


class _FitImageLabel(QLabel):
    """QLabel that draws its pixmap scaled to fit the widget, keeping aspect ratio."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._source: QPixmap = QPixmap()

    def set_source_pixmap(self, pixmap: QPixmap) -> None:
        """Store the pixmap to draw fitted on every repaint."""
        self._source = pixmap
        self.update()

    def clear_source(self) -> None:
        """Drop the stored pixmap so only the label text renders."""
        self._source = QPixmap()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the source pixmap scaled to fit, falling back to normal QLabel text."""
        if self._source.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scaled = self._source.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(
            (self.width() - scaled.width()) // 2,
            (self.height() - scaled.height()) // 2,
            scaled,
        )


class _PreviewGallery(QDialog):
    """Thumbnail gallery of a mod's preview images."""

    def __init__(
        self,
        mod_dir: Path,
        mod_name: str,
        parent: QWidget | None = None,
        on_change: Callable[[Path], None] | None = None,
        on_promote: Callable[[Path, Path | None], None] | None = None,
        promoted_name: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Preview — {mod_name}")
        self._mod_dir = mod_dir
        self._current_image: Path | None = None
        self._on_change = on_change
        self._on_promote = on_promote
        self._promoted_name = promoted_name
        self._settings = StateSettings()
        collapsed_setting = self._settings.value("preview_strip_collapsed", False)
        self._strip_collapsed = collapsed_setting in (True, "true")
        self._drop_active = False
        geometry = _restored_geometry(self._settings.value("preview_geometry"))
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(760, 560)
        layout = QVBoxLayout(self)
        self._image_label = _FitImageLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(0, 320)
        layout.addWidget(self._image_label, 1)
        self._image_label.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._image_label.customContextMenuRequested.connect(
            self._on_image_context_menu
        )
        nav_qss = (
            "QToolButton {"
            " background-color: rgba(0, 0, 0, 35%);"
            " border: none;"
            " border-radius: 18px;"
            " color: white;"
            " font-size: 15px;"
            " }"
            "QToolButton:hover {"
            " background-color: rgba(0, 0, 0, 55%);"
            " }"
        )
        self._nav_left = QToolButton(self._image_label)
        self._nav_right = QToolButton(self._image_label)
        self._nav_left.setFixedSize(36, 36)
        self._nav_right.setFixedSize(36, 36)
        self._nav_left.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._nav_right.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._nav_left.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nav_right.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nav_left.setStyleSheet(nav_qss)
        self._nav_right.setStyleSheet(nav_qss)
        self._nav_left.setText("◀")
        self._nav_right.setText("▶")
        self._nav_left.clicked.connect(partial(self._step_preview, -1))
        self._nav_right.clicked.connect(partial(self._step_preview, 1))
        self._nav_left.setVisible(False)
        self._nav_right.setVisible(False)
        self._image_label.installEventFilter(self)
        button_row = QHBoxLayout()
        self._upload_button = QPushButton("Upload images…", self)
        self._upload_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._upload_button.clicked.connect(self._on_upload_clicked)
        button_row.addWidget(self._upload_button)
        button_row.addStretch(1)
        hint = QLabel("Ctrl+V or drag image files here", self)
        hint_palette = hint.palette()
        hint_palette.setColor(
            QPalette.ColorRole.Mid, _dimmed_color(hint_palette, 0.4)
        )
        hint.setPalette(hint_palette)
        hint.setForegroundRole(QPalette.ColorRole.Mid)
        button_row.addWidget(hint)
        self._collapse_button = QToolButton(self)
        self._collapse_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._collapse_button.setAutoRaise(True)
        self._collapse_button.setFixedSize(28, 28)
        self._collapse_button.clicked.connect(self._toggle_strip)
        button_row.addWidget(self._collapse_button)
        layout.addLayout(button_row)
        self._sync_strip_button()
        self._list = QListWidget(self)
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(96, 96))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setFlow(QListWidget.Flow.LeftToRight)
        self._list.setWrapping(False)
        self._list.setFixedHeight(124)
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        accent = COLOR_ACCENT
        self._image_label.setStyleSheet(
            'QLabel[dropActive="true"] {'
            f" border: 2px solid rgba({accent.red()}, {accent.green()}, {accent.blue()}, 60%);"
            " border-radius: 4px;"
            " }"
        )
        self._list.setStyleSheet(
            "QListWidget::item:hover {"
            f" background-color: rgba({accent.red()}, {accent.green()}, {accent.blue()}, 40%);"
            " }"
        )
        self._list.currentItemChanged.connect(self._on_preview_changed)
        self._list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._list.customContextMenuRequested.connect(
            self._on_list_context_menu
        )
        layout.addWidget(self._list, 0)
        self._status_label = QLabel("", self)
        status_palette = self._status_label.palette()
        status_palette.setColor(
            QPalette.ColorRole.Mid, _dimmed_color(status_palette, 0.4)
        )
        self._status_label.setPalette(status_palette)
        self._status_label.setForegroundRole(QPalette.ColorRole.Mid)
        layout.addWidget(self._status_label)
        paste_shortcut = QShortcut(
            QKeySequence(QKeySequence.StandardKey.Paste), self
        )
        paste_shortcut.activated.connect(self._add_clipboard)
        self.setAcceptDrops(True)
        self._refresh_list(None)
        self._position_nav_buttons()

    def _set_status(self, text: str) -> None:
        """Show feedback for the last add or copy attempt."""
        self._status_label.setText(text)

    def _refresh_list(self, select: Path | None = None) -> None:
        """Rebuild the thumbnail list, selecting the given path or the first item."""
        self._list.clear()
        images = _mod_images(self._mod_dir)
        for path in images:
            pixmap = QPixmap(str(path))
            scaled = pixmap.scaled(
                QSize(96, 96),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            item = QListWidgetItem(QIcon(scaled), path.name, self._list)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setSizeHint(QSize(max(scaled.width() + 12, 60), 112))
            item.setToolTip(path.name)
        self._list.setVisible(bool(images) and not self._strip_collapsed)
        has_multiple = self._list.count() > 1
        self._nav_left.setVisible(has_multiple)
        self._nav_right.setVisible(has_multiple)
        if not images:
            self._image_label.clear_source()
            self._image_label.setText("No preview images found.")
            return
        selected: QListWidgetItem | None = None
        if select is not None:
            for row in range(self._list.count()):
                item = self._list.item(row)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == str(select):
                    selected = item
                    break
        if selected is not None:
            self._list.setCurrentItem(selected)
        else:
            self._list.setCurrentRow(0)

    def _sync_strip_button(self) -> None:
        """Reflect the strip state on the toggle button."""
        self._collapse_button.setText("▴" if self._strip_collapsed else "▾")
        self._collapse_button.setToolTip(
            "Expand thumbnails" if self._strip_collapsed else "Collapse thumbnails"
        )

    def _toggle_strip(self) -> None:
        """Flip the thumbnail strip's visibility and persist the choice."""
        self._strip_collapsed = not self._strip_collapsed
        self._settings.setValue("preview_strip_collapsed", self._strip_collapsed)
        self._sync_strip_button()
        self._refresh_list(None)

    def _add_image_paths(self, sources: list[Path]) -> None:
        """Add image files into the mod root (PNG/BMP re-encoded as JPEG), then refresh the list."""
        added: list[Path] = []
        failed: list[str] = []
        for source in sources:
            if not source.is_file():
                failed.append(source.name)
                continue
            try:
                if source.suffix.lower() in (".png", ".bmp"):
                    destination = _unique_destination(
                        self._mod_dir, f"{source.stem}.jpg"
                    )
                    image = QImage(str(source))
                    if image.isNull() or not _save_jpeg(image, destination):
                        destination = _unique_destination(
                            self._mod_dir, source.name
                        )
                        shutil.copyfile(source, destination)
                else:
                    destination = _unique_destination(self._mod_dir, source.name)
                    shutil.copyfile(source, destination)
            except OSError:
                failed.append(source.name)
                continue
            added.append(destination)
        if sources and added:
            status = f"Added {len(added)} image(s)."
            if failed:
                status += f" {len(failed)} failed."
            self._set_status(status)
        elif sources:
            self._set_status("Could not add the selected images.")
        self._refresh_list(added[0] if added else None)
        if added:
            self._notify_change()

    def _on_upload_clicked(self) -> None:
        """Open a file dialog and copy the chosen images into the mod root."""
        pattern = " ".join(f"*{extension}" for extension in _IMAGE_EXTENSIONS)
        chosen, _ = QFileDialog.getOpenFileNames(
            self, "Add preview images", str(self._mod_dir), f"Images ({pattern})"
        )
        if not chosen:
            return
        self._add_image_paths([Path(path) for path in chosen])

    @staticmethod
    def _clipboard_image_paths(mime: QMimeData | None = None) -> list[Path]:
        """Local image file paths in the clipboard or the given mime data."""
        if mime is None:
            mime = QGuiApplication.clipboard().mimeData()
        if not mime.hasUrls():
            return []
        paths: list[Path] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            if not path.is_file():
                continue
            paths.append(path)
        return paths

    def _add_clipboard(self) -> None:
        """Add the clipboard's image files, or save an embedded bitmap image."""
        paths = self._clipboard_image_paths()
        if paths:
            self._add_image_paths(paths)
            return
        image = QGuiApplication.clipboard().image()
        if not image.isNull():
            destination = _unique_destination(self._mod_dir, "preview.jpg")
            if _save_jpeg(image, destination):
                self._set_status(f"Added {destination.name}.")
                self._refresh_list(destination)
                self._notify_change()
            else:
                self._set_status("Could not save the clipboard image.")
            return
        self._set_status("No image in the clipboard.")

    def _set_drop_active(self, active: bool) -> None:
        """Toggle the accent border shown while an image drag hovers the gallery."""
        if active == self._drop_active:
            return
        self._drop_active = active
        self._image_label.setProperty("dropActive", active)
        self._image_label.style().unpolish(self._image_label)
        self._image_label.style().polish(self._image_label)
        self._image_label.update()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept file drags carrying at least one local image file."""
        if event.mimeData().hasUrls() and self._clipboard_image_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._set_drop_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        """Accept image-file drag moves so the subsequent drop is delivered."""
        if event.mimeData().hasUrls() and self._clipboard_image_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._set_drop_active(True)
        else:
            event.ignore()
            self._set_drop_active(False)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        """Clear the drop highlight when the drag leaves the gallery."""
        self._set_drop_active(False)

    def dropEvent(self, event: QDropEvent) -> None:
        """Copy the dropped image files into the mod root."""
        self._set_drop_active(False)
        event.acceptProposedAction()
        self._add_image_paths(self._clipboard_image_paths(event.mimeData()))

    def _on_preview_changed(self, item: QListWidgetItem | None) -> None:
        if item is None:
            self._current_image = None
            self._image_label.clear_source()
            self._image_label.setText("")
            return
        pixmap = QPixmap(str(item.data(Qt.ItemDataRole.UserRole)))
        if pixmap.isNull():
            self._current_image = None
            self._image_label.clear_source()
            self._image_label.setText("Could not load image.")
            return
        self._current_image = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        self._image_label.setText("")
        self._image_label.set_source_pixmap(pixmap)

    def _on_list_context_menu(self, position: QPoint) -> None:
        """Offer preview promotion and deletion for the right-clicked thumbnail."""
        item = self._list.itemAt(position)
        if item is None:
            return
        image = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        menu = QMenu(self)
        menu.addAction("Set as preview image")
        # Reset only applies to the promoted image itself.
        if image.name == self._promoted_name:
            menu.addAction("Reset preview image")
        menu.addSeparator()
        menu.addAction("Delete image")
        chosen: QAction | None = menu.exec(self._list.mapToGlobal(position))
        if chosen is None:
            return
        if chosen.text() == "Set as preview image":
            self._promote_current(image)
        elif chosen.text() == "Reset preview image":
            self._promote_current(None)
        elif chosen.text() == "Delete image":
            self._delete_image(image)

    def _on_image_context_menu(self, position: QPoint) -> None:
        """Offer preview promotion and deletion for the image shown in the large preview."""
        if self._current_image is None:
            return
        menu = QMenu(self)
        menu.addAction("Set as preview image")
        # Reset only applies to the promoted image itself.
        if self._current_image.name == self._promoted_name:
            menu.addAction("Reset preview image")
        menu.addSeparator()
        menu.addAction("Delete image")
        chosen: QAction | None = menu.exec(self._image_label.mapToGlobal(position))
        if chosen is None:
            return
        if chosen.text() == "Set as preview image":
            self._promote_current(self._current_image)
        elif chosen.text() == "Reset preview image":
            self._promote_current(None)
        elif chosen.text() == "Delete image":
            self._delete_image(self._current_image)

    def _promote_current(self, image: Path | None) -> None:
        """Report the promoted preview through the callback, then remember it."""
        if self._on_promote is None:
            return
        self._on_promote(self._mod_dir, image)
        self._promoted_name = image.name if image is not None else None
        self._set_status(
            "Preview image set." if image is not None else "Preview image reset."
        )

    def _notify_change(self) -> None:
        """Tell the MainWindow that this mod's gallery images changed."""
        if self._on_change is not None:
            self._on_change(self._mod_dir)

    def _delete_image(self, path: Path) -> None:
        """Remove the image file after confirmation, then refresh the gallery."""
        answer = QMessageBox.question(
            self,
            "Delete image",
            f"Delete '{path.name}' permanently?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            path.unlink()
        except OSError as error:
            self._set_status(f"Could not delete {path.name}: {error}")
            return
        self._set_status(f"Deleted {path.name}.")
        self._refresh_list(self._successor_of(path))
        self._notify_change()

    def _successor_of(self, path: Path) -> Path | None:
        """Path of the neighbor to select once the given image is removed."""
        count = self._list.count()
        for row in range(count):
            item = self._list.item(row)
            if item is None or item.data(Qt.ItemDataRole.UserRole) != str(path):
                continue
            if row + 1 < count:
                successor_row = row + 1
            elif row > 0:
                successor_row = row - 1
            else:
                return None
            successor = self._list.item(successor_row)
            return Path(str(successor.data(Qt.ItemDataRole.UserRole)))
        return None

    def _step_preview(self, direction: int) -> None:
        """Step the filmstrip selection by direction, wrapping at both ends."""
        count = self._list.count()
        if not count:
            return
        self._list.setCurrentRow((self._list.currentRow() + direction) % count)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Select the previous/next image with bare Left/Right arrows, wrapping."""
        if (
            event.modifiers() == Qt.KeyboardModifier.NoModifier
            and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right)
        ):
            count = self._list.count()
            if count:
                row = self._list.currentRow()
                if event.key() == Qt.Key.Key_Left:
                    row = count - 1 if row <= 0 else row - 1
                else:
                    row = 0 if row + 1 >= count else row + 1
                self._list.setCurrentRow(row)
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Keep the overlay chevrons centered on the image label's edges."""
        if obj is self._image_label and event.type() == QEvent.Type.Resize:
            self._position_nav_buttons()
        return super().eventFilter(obj, event)

    def _position_nav_buttons(self) -> None:
        """Place the chevron buttons at the vertical center of each image edge."""
        size = self._nav_left.width()
        self._nav_left.move(8, (self._image_label.height() - size) // 2)
        self._nav_right.move(
            self._image_label.width() - size - 8,
            (self._image_label.height() - size) // 2,
        )

    def done(self, result: int) -> None:
        """Persist the dialog geometry, then finish the dialog."""
        self._settings.setValue(
            "preview_geometry", self.saveGeometry().data().hex()
        )
        super().done(result)


_RESIZE_MARGIN = 8

# Tooltip thumbnails are area-normalized instead of box-fit: the scale makes
# width * height hit ~220^2 = 48,400 px^2 (220x280 fits ~61.6k px^2 of a
# 220x280-ratio image, so 48,400 is the like-for-like target), with a 300px
# hard cap per dimension for extreme aspect ratios.
_THUMB_AREA_PX = 48_400
_THUMB_MAX_DIM_PX = 300.0


class _RinaIcon(QLabel):
    """Animated Rina icon: random hover moves, breathing busy glow."""

    # Fraction of its move each sparkle stays visible; starts span 0.0..0.4,
    # so every star finishes before the move ends.
    _SPARKLE_SPAN = 0.6

    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._base = pixmap
        self._glow = 0.0
        # Paint channels written by the active move, resting at zero/one.
        self._angle = 0.0
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._scale = 1.0
        self._squash = 1.0
        self._sparkles: tuple[tuple[float, float, float, float], ...] = ()
        self._sparkle_seeds: tuple[tuple[float, float, float, float], ...] = ()
        self._bolts: tuple[tuple[tuple[float, float], ...], ...] = ()
        self._bolt_alpha = 0.0
        self._energized = False
        self._glow_ms = 0
        self._move: tuple[str, int, Callable[..., None]] = self._MOVES[0]
        self._recent_moves: deque[str] = deque(maxlen=2)
        self._move_ms = 0
        self._move_active = False
        # 24 px art plus a 2 px transparent halo margin on every side.
        self.setFixedSize(28, 28)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._animate)

    def set_energized(self, energized: bool) -> None:
        """Start or stop the breathing glow that mirrors the busy wash."""
        self._energized = energized
        self._sync_timer()

    def play_reaction(self) -> None:
        """Play one random reaction move now, for silent visual feedback."""
        self._start_move()

    def enterEvent(self, event: QEnterEvent) -> None:
        """Play a random reaction move, never restarting one already in flight."""
        if not self._move_active:
            self._start_move()
        super().enterEvent(event)

    def _start_move(self) -> None:
        """Begin a random reaction other than the two moves played most recently."""
        choices: list[tuple[str, int, Callable[..., None]]] = [
            move for move in self._MOVES if move[0] not in self._recent_moves
        ]
        name, duration, tick = random.choice(choices)
        self._recent_moves.append(name)
        self._move = (name, duration, tick)
        self._move_ms = 0
        self._move_active = True
        self._reset_move_channels()
        self._sync_timer()

    def _reset_move_channels(self) -> None:
        """Return every paint channel a move owns to its resting state."""
        self._angle = 0.0
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._scale = 1.0
        self._squash = 1.0
        self._sparkles = ()
        self._sparkle_seeds = ()
        self._bolts = ()
        self._bolt_alpha = 0.0

    def _sync_timer(self) -> None:
        """Keep the animation timer running exactly while any effect is active."""
        active = (
            self._energized
            or self._move_active
            or self._glow > 0.0
        )
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()

    def _animate(self) -> None:
        if self._move_active:
            self._move_ms += 16
            _, duration, tick = self._move
            tick(self, min(1.0, self._move_ms / duration))
            if self._move_ms >= duration:
                self._move_active = False
                self._reset_move_channels()
        if self._energized:
            self._glow_ms = (self._glow_ms + 16) % 2000
            self._glow = 0.45 + 0.45 * (
                0.5 + 0.5 * math.sin(2.0 * math.pi * self._glow_ms / 2000.0)
            )
        elif self._glow > 0.0:
            self._glow *= 0.85
            if self._glow < 0.01:
                self._glow = 0.0
        self.update()
        self._sync_timer()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        glow = self._glow
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        if glow > 0.0:
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            halo = QColor(COLOR_ACCENT)
            for index in range(4):
                halo.setAlpha(int(130 * glow * (index + 1) / 4.0))
                painter.setBrush(halo)
                radius = 12.5 - 1.5 * index
                painter.drawEllipse(QPointF(0.0, 0.0), radius, radius)
        painter.translate(self._offset_x, self._offset_y)
        painter.rotate(self._angle)
        painter.scale(self._scale, self._scale * self._squash)
        painter.drawPixmap(-12, -12, self._base)
        if self._bolts:
            self._paint_bolts(painter)
        if self._sparkles:
            self._paint_sparkles(painter)

    def _paint_bolts(self, painter: QPainter) -> None:
        """Stroke the jagged zap bolts beside the art with a flickering alpha."""
        alpha = self._bolt_alpha
        if alpha <= 0.0:
            return
        glow_color = QColor(COLOR_ACCENT)
        glow_color.setAlpha(int(80 * alpha))
        core_color = QColor(COLOR_ACCENT)
        core_color.setAlpha(int(235 * alpha))
        glow_pen = QPen(glow_color, 3.2)
        core_pen = QPen(core_color, 1.4)
        for pen in (glow_pen, core_pen):
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        for points in self._bolts:
            poly = QPolygonF([QPointF(x, y) for x, y in points])
            painter.setPen(glow_pen)
            painter.drawPolyline(poly)
            painter.setPen(core_pen)
            painter.drawPolyline(poly)

    def _paint_sparkles(self, painter: QPainter) -> None:
        """Draw the four-point sparkle stars on top of the art."""
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        halo = QColor(COLOR_ACCENT)
        core = QColor(Qt.GlobalColor.white)
        for x, y, size, alpha in self._sparkles:
            halo.setAlpha(int(150 * alpha))
            painter.setBrush(halo)
            painter.drawPolygon(self._star_polygon(x, y, size * 1.7))
            core.setAlpha(int(235 * alpha))
            painter.setBrush(core)
            painter.drawPolygon(self._star_polygon(x, y, size))

    def _tick_rock(self, t: float) -> None:
        """Damped ±6° tilt: the original hover wiggle, kept behavior-identical."""
        ms = 450.0 * t
        seconds = ms / 1000.0
        envelope = math.exp(-3.0 * t)
        self._angle = 6.0 * envelope * math.sin(2.0 * math.pi * seconds / 0.3)

    def _tick_hop(self, t: float) -> None:
        """One ~4 px parabolic hop with squash-and-stretch on landing."""
        height = 16.0 * t * (1.0 - t)
        self._offset_y = -height
        stretch = 1.0 + 0.1 * (height / 4.0)
        impact = math.exp(-(((t - 0.92) / 0.055) ** 2))
        self._squash = stretch * (1.0 - 0.15 * impact)

    def _tick_shake(self, t: float) -> None:
        """Decaying horizontal sine jitter, four cycles of ±1.5 px."""
        self._offset_x = 1.5 * (1.0 - t) * math.sin(2.0 * math.pi * 4.0 * t)

    def _tick_curtsy(self, t: float) -> None:
        """Slow eased lean to ~8° with a ~2 px dip, out and back."""
        lean = math.sin(math.pi * t)
        self._angle = -8.0 * lean
        self._offset_y = 2.0 * lean

    def _tick_sparkles(self, t: float) -> None:
        """Stagger stars around her, each growing and fading out."""
        if not self._sparkle_seeds:
            self._sparkle_seeds = tuple(
                (
                    random.uniform(0.0, 360.0),
                    random.uniform(9.0, 13.0),
                    random.uniform(0.0, 0.4),
                    random.uniform(1.8, 2.8),
                )
                for _ in range(random.choice((3, 4)))
            )
        spots = []
        for angle_deg, radius, start, size in self._sparkle_seeds:
            life = (t - start) / self._SPARKLE_SPAN
            if 0.0 < life < 1.0:
                spots.append((
                    radius * math.cos(math.radians(angle_deg)),
                    radius * math.sin(math.radians(angle_deg)),
                    size * life,
                    (1.0 - life) * min(1.0, 6.0 * life),
                ))
        self._sparkles = tuple(spots)

    def _tick_zap(self, t: float) -> None:
        """Flicker one or two jagged bolts beside her."""
        if not self._bolts:
            sides = random.sample((-1, 1), random.choice((1, 2)))
            self._bolts = tuple(self._bolt_points(side) for side in sides)
        envelope = math.sin(math.pi * t)
        flicker = 0.55 + 0.45 * math.sin(2.0 * math.pi * 7.0 * t)
        self._bolt_alpha = max(0.0, envelope * flicker)

    def _tick_pop(self, t: float) -> None:
        """Quick eased scale swell from 1.0 up to 1.18 and back."""
        self._scale = 1.0 + 0.18 * math.sin(math.pi * t)

    def _tick_nod(self, t: float) -> None:
        """Two quick vertical bobs without any rotation."""
        self._offset_y = 3.0 * abs(math.sin(2.0 * math.pi * t))

    @staticmethod
    def _bolt_points(side: int) -> tuple[tuple[float, float], ...]:
        """Build one jagged bolt polyline hugging the given side of the art."""
        points = []
        y = -9.0
        step = 0
        while y < 8.5:
            if step % 2 == 0:
                x = side * random.uniform(10.5, 11.5)
            else:
                x = side * random.uniform(9.0, 10.0)
            points.append((x, y))
            y += random.uniform(3.5, 5.5)
            step += 1
        points.append((side * random.uniform(10.5, 11.5), 9.0))
        return tuple(points)

    @staticmethod
    def _star_polygon(x: float, y: float, radius: float) -> QPolygonF:
        """Four-point star polygon centered on (x, y)."""
        points = QPolygonF()
        for corner in range(8):
            angle = math.pi * corner / 4.0
            spoke = radius if corner % 2 == 0 else radius * 0.35
            points.append(QPointF(
                x + spoke * math.cos(angle),
                y - spoke * math.sin(angle),
            ))
        return points

    # Random hover reactions: (name, duration_ms, per-tick update from t in 0..1).
    _MOVES = (
        ("rock", 450, _tick_rock),
        ("hop", 500, _tick_hop),
        ("shake", 350, _tick_shake),
        ("curtsy", 700, _tick_curtsy),
        ("sparkles", 700, _tick_sparkles),
        ("zap", 250, _tick_zap),
        ("pop", 350, _tick_pop),
        ("nod", 450, _tick_nod),
    )


class _TitleBar(QWidget):
    """Frameless title bar: drag to move, double-click toggles maximize."""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__(window)
        self._window = window
        self.setObjectName("zzzTitleBar")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(6)
        self._icon = _RinaIcon(window.windowIcon().pixmap(24, 24), self)
        layout.addWidget(self._icon)
        self._menu_button = QToolButton(self)
        self._menu_button.setObjectName("menuButton")
        self._menu_button.setText("☰")
        self._menu_button.setAutoRaise(True)
        self._menu_button.setFixedSize(28, 28)
        self.app_menu = QMenu(self)
        self._menu_button.clicked.connect(self._show_menu)
        layout.addWidget(self._menu_button)
        layout.addWidget(QLabel("ZZZ Mod Housekeeper", self))
        layout.addStretch(1)
        self._min_button = QToolButton(self)
        self._min_button.setObjectName("minButton")
        self._min_button.setText("─")
        self._min_button.setAutoRaise(True)
        self._min_button.setFixedSize(28, 28)
        self._min_button.clicked.connect(window.showMinimized)
        layout.addWidget(self._min_button)
        self._max_button = QToolButton(self)
        self._max_button.setObjectName("maxButton")
        self._max_button.setAutoRaise(True)
        self._max_button.setFixedSize(28, 28)
        self._max_button.clicked.connect(self._toggle_maximized)
        layout.addWidget(self._max_button)
        self._close_button = QToolButton(self)
        self._close_button.setObjectName("closeButton")
        self._close_button.setText("✕")
        self._close_button.setAutoRaise(True)
        self._close_button.setFixedSize(28, 28)
        self._close_button.clicked.connect(window.close)
        layout.addWidget(self._close_button)
        self._sync_max_glyph()
        self._busy = False
        self._busy_phase = 0.0
        self._base = self.palette().color(QPalette.ColorRole.Window)
        self._accent = COLOR_ACCENT
        self.setStyleSheet(
            "QToolButton:hover {"
            f" background-color: rgba({self._accent.red()}, {self._accent.green()}, {self._accent.blue()}, 15%);"
            " }"
            "QToolButton:pressed {"
            f" background-color: rgba({self._accent.red()}, {self._accent.green()}, {self._accent.blue()}, 30%);"
            " }"
            "QToolButton#closeButton:hover {"
            " background-color: #e81123;"
            " color: white;"
            " }"
            "QToolButton#closeButton:pressed {"
            " background-color: #c50f1f;"
            " }"
        )
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(16)
        self._busy_timer.timeout.connect(self._tick_busy)
        window.installEventFilter(self)

    def _show_menu(self) -> None:
        self.app_menu.popup(
            self._menu_button.mapToGlobal(QPoint(0, self._menu_button.height()))
        )

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._window and event.type() == QEvent.Type.WindowStateChange:
            self._sync_max_glyph()
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        handle = self._window.windowHandle()
        if handle is not None:
            handle.startSystemMove()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def _sync_max_glyph(self) -> None:
        self._max_button.setText("❐" if self._window.isMaximized() else "□")

    def set_busy(self, busy: bool) -> None:
        """Start or stop the cascading busy wash across the title bar."""
        if busy == self._busy:
            return
        self._busy = busy
        self._icon.set_energized(busy)
        self._busy_phase = 0.0
        if busy:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
        self.update()

    def amuse_rina(self) -> None:
        """Make Rina play a random reaction for silent visual feedback."""
        self._icon.play_reaction()

    def _tick_busy(self) -> None:
        self._busy_phase = (self._busy_phase + 0.008) % 1.0
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        if self._busy:
            painter.fillRect(self.rect(), self._busy_gradient())
        else:
            painter.fillRect(
                self.rect(), self.palette().color(QPalette.ColorRole.Window)
            )
        painter.fillRect(
            0,
            self.height() - 1,
            self.width(),
            1,
            self.palette().color(QPalette.ColorRole.Mid),
        )

    def _busy_gradient(self) -> QLinearGradient:
        """Left-to-right gradient with a soft raised-cosine accent band at the busy phase."""
        gradient = QLinearGradient(0.0, 0.0, 1.0, 0.0)
        gradient.setCoordinateMode(QGradient.CoordinateMode.ObjectBoundingMode)
        half_width = 0.22
        step = 1.0 / 32.0
        for index in range(33):
            position = index * step
            distance = abs(position - self._busy_phase)
            intensity = (
                0.5 + 0.5 * math.cos(math.pi * distance / half_width)
                if distance < half_width
                else 0.0
            )
            gradient.setColorAt(position, self._blend(self._base, self._accent, 0.85 * intensity))
        return gradient

    @staticmethod
    def _blend(base: QColor, accent: QColor, amount: float) -> QColor:
        """Color of base blended toward accent by amount (0..1)."""
        return QColor(
            base.red() + round((accent.red() - base.red()) * amount),
            base.green() + round((accent.green() - base.green()) * amount),
            base.blue() + round((accent.blue() - base.blue()) * amount),
        )


class _CenteredCheckDelegate(QStyledItemDelegate):
    """Draw the enable checkbox centered in its cell."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if index.column() != 1 or index.data(Qt.ItemDataRole.CheckStateRole) is None:
            super().paint(painter, option, index)
            return
        widget = opt.widget
        style = QApplication.style() if widget is None else widget.style()
        plain = QStyleOptionViewItem(opt)
        plain.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, plain, painter, widget)
        check = QStyleOptionViewItem(opt)
        check.rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, check, widget
        )
        raw = index.data(Qt.ItemDataRole.CheckStateRole)
        current = raw.value if hasattr(raw, "value") else raw
        self._paint_indicator(
            painter,
            self._indicator_box(check.rect, option.rect),
            check.palette,
            current,
            index,
        )

    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        """Toggle the enable checkbox when its centered cell area is clicked."""
        if (
            index.column() != 1
            or index.data(Qt.ItemDataRole.CheckStateRole) is None
            or event.type() not in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick)
            or not isinstance(event, QMouseEvent)
        ):
            return super().editorEvent(event, model, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = QApplication.style() if widget is None else widget.style()
        check_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, widget
        )
        if not self._indicator_box(check_rect, option.rect).contains(event.position()):
            return True
        QTimer.singleShot(0, partial(self._deferred_toggle, QPersistentModelIndex(index)))
        return True

    @staticmethod
    def _deferred_toggle(index: QPersistentModelIndex) -> None:
        """Toggle one check-state cell outside the in-flight mouse event."""
        if not index.isValid():
            return
        value = index.data(Qt.ItemDataRole.CheckStateRole)
        if value is None:
            return
        current = value.value if hasattr(value, "value") else value
        toggled = (
            Qt.CheckState.Unchecked
            if current == Qt.CheckState.Checked.value
            else Qt.CheckState.Checked
        )
        index.model().setData(index, toggled, Qt.ItemDataRole.CheckStateRole)

    @staticmethod
    def _indicator_box(
        check_rect: QRect, cell_rect: QRect
    ) -> QRectF:
        """Square box centered exactly on the cell, sized from the native indicator."""
        side = min(check_rect.width(), check_rect.height())
        box = QRectF(0.0, 0.0, float(side), float(side))
        box.moveCenter(QRectF(cell_rect).center())
        return box

    def _paint_indicator(
        self,
        painter: QPainter,
        box: QRectF,
        palette: QPalette,
        current: int,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        """Draw the enable checkbox with the app accent instead of the OS style."""
        window = palette.color(QPalette.ColorRole.Window)
        accent = COLOR_ACCENT
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        stroke_color: QColor | None = None
        if current == 2:
            fill: QColor | None = accent
            tick_color: QColor | None = QColor(Qt.GlobalColor.white)
        elif current == 1:
            fill = self._blend(window, accent, 0.45)
            tick_color = accent
        else:
            fill = None
            tick_color = None
            stroke_color = self._blend(
                palette.color(QPalette.ColorRole.Mid), window, 0.7
            )
        if not enabled:
            if fill is not None:
                fill = self._blend(fill, window, 0.4)
            if tick_color is not None:
                tick_color = self._blend(tick_color, window, 0.4)
            if stroke_color is not None:
                stroke_color = self._blend(stroke_color, window, 0.4)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if stroke_color is None:
            painter.setPen(QPen(Qt.PenStyle.NoPen))
        else:
            painter.setPen(QPen(stroke_color, 1.0))
        if fill is None:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        else:
            painter.setBrush(fill)
        painter.drawRoundedRect(box, 3.0, 3.0)
        if tick_color is not None:
            painter.setPen(
                QPen(
                    tick_color,
                    2.0,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPolyline(
                QPolygonF([
                    QPointF(box.left() + 0.28 * box.width(), box.top() + 0.55 * box.width()),
                    QPointF(box.left() + 0.44 * box.width(), box.top() + 0.70 * box.width()),
                    QPointF(box.left() + 0.74 * box.width(), box.top() + 0.34 * box.width()),
                ])
            )
        painter.restore()

    @staticmethod
    def _blend(base: QColor, accent: QColor, amount: float) -> QColor:
        """Color of base blended toward accent by amount (0..1)."""
        return QColor(
            base.red() + round((accent.red() - base.red()) * amount),
            base.green() + round((accent.green() - base.green()) * amount),
            base.blue() + round((accent.blue() - base.blue()) * amount),
        )


class _ModsTree(QTreeWidget):
    """Mods overview tree accepting archive drops on categories and empty space."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(False)
        self._drop_active = False
        self._drop_item: QTreeWidgetItem | None = None
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Serve mod-row tooltips lazily from hover events instead of precomputing per fill."""
        if obj is self.viewport() and event.type() == QEvent.Type.ToolTip:
            if isinstance(event, QHelpEvent):
                item = self.itemAt(event.pos())
                column = self.columnAt(event.pos().x())
                if item is not None and column == 0:
                    node = item.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(node, ModNode):
                        html = self._window.mod_tooltip_html(node)
                        QToolTip.showText(
                            event.globalPos(), html, self, self.visualItemRect(item)
                        )
                        return True
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept any single-archive drag; dragMoveEvent gates the destination."""
        if self._drop_archive(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()
        self._update_drop_visuals(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._accept_or_ignore(event)
        self._update_drop_visuals(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        """Clear the drop highlight when the drag leaves the tree."""
        self._set_drop_active(False)
        self._set_drop_item(None)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drop_active(False)
        self._set_drop_item(None)
        archive = self._drop_archive(event.mimeData())
        node = self._target_node(event.position().toPoint())
        if archive is None or not self._accepts(node):
            if archive is None:
                self._reject_drop(event.mimeData())
            event.ignore()
            return
        event.acceptProposedAction()
        self._window.handle_archive_drop(archive, node)

    def _set_drop_active(self, active: bool) -> None:
        """Toggle the accent frame shown while an acceptable drag hovers empty space."""
        if active == self._drop_active:
            return
        self._drop_active = active
        self.setProperty("dropActive", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.viewport().update()

    def _set_drop_item(self, item: QTreeWidgetItem | None) -> None:
        """Highlight the single row that would receive the dropped archive."""
        if item is self._drop_item:
            return
        if self._drop_item is not None:
            self._drop_item.setData(0, Qt.ItemDataRole.BackgroundRole, None)
        self._drop_item = item
        if item is not None:
            item.setData(0, Qt.ItemDataRole.BackgroundRole, _DROP_TINT)
        self.viewport().update()

    def _update_drop_visuals(self, event: QDragMoveEvent) -> None:
        """Row tint when hovering a named row, frame tint over empty space."""
        if not event.isAccepted():
            self._set_drop_active(False)
            self._set_drop_item(None)
            return
        item = self.itemAt(event.position().toPoint())
        if item is None:
            self._set_drop_item(None)
            self._set_drop_active(True)
        else:
            self._set_drop_active(False)
            self._set_drop_item(item)

    def _accept_or_ignore(self, event: QDragMoveEvent) -> None:
        archive = self._drop_archive(event.mimeData())
        node = self._target_node(event.position().toPoint())
        if archive is not None and self._accepts(node):
            event.acceptProposedAction()
            return
        event.ignore()

    def _target_node(self, pos: QPoint) -> ModNode | None:
        item = self.itemAt(pos)
        if item is None:
            return None
        node = item.data(0, Qt.ItemDataRole.UserRole)
        return node if isinstance(node, ModNode) else None

    @staticmethod
    def _accepts(node: ModNode | None) -> bool:
        return node is None or node.kind == "category"

    @staticmethod
    def _drop_archive(mime: QMimeData) -> Path | None:
        if not mime.hasUrls():
            return None
        urls = mime.urls()
        if len(urls) != 1:
            return None
        path = Path(urls[0].toLocalFile())
        if not path.is_file() or path.suffix.lower() not in _ARCHIVE_SUFFIXES:
            return None
        return path

    def _reject_drop(self, mime: QMimeData) -> None:
        if not mime.hasUrls():
            return
        urls = mime.urls()
        if len(urls) != 1:
            self._window.append_log("Drop a single archive file")
            return
        path = Path(urls[0].toLocalFile())
        if path.is_file():
            suffix = path.suffix.lower() or "(none)"
            self._window.append_log(f"Unsupported archive type: {suffix}")


class _AboutDialog(QDialog):
    """About box: feature summary, source link, and Rina's greeting."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About ZZZ Mod Housekeeper")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        name_label = QLabel(
            '<span style="font-size:13pt; font-weight:bold;">ZZZ Mod Housekeeper</span>',
            self,
        )
        layout.addWidget(name_label)
        body_label = QLabel(
            "Dusts outdated asset hashes out of your mods, installs new arrivals, "
            "keeps preset loadouts in order, previews each mod's screenshots, and "
            "keeps an eye on upstream hash data.",
            self,
        )
        body_label.setWordWrap(True)
        body_label.setMaximumWidth(440)
        layout.addWidget(body_label)
        credit_label = QLabel(
            'By Kilvoctu: <a href="https://github.com/Kilvoctu/ZZZ_Mod_Housekeeper">Source</a>',
            self,
        )
        credit_label.setOpenExternalLinks(True)
        layout.addWidget(credit_label)
        footer = QHBoxLayout()
        quote_label = QLabel(
            '<i>"Are you the new master? Rina from Victoria Housekeeping, at your service."</i>',
            self,
        )
        quote_label.setWordWrap(True)
        quote_label.setMaximumWidth(340)
        footer.addWidget(quote_label)
        footer.addStretch(1)
        okay_button = QPushButton("Okay", self)
        okay_button.clicked.connect(self.accept)
        okay_button.setDefault(True)
        okay_button.setMinimumWidth(80)
        footer.addWidget(okay_button)
        layout.addLayout(footer)


class MainWindow(QMainWindow):
    """Choose a mods folder, update the hash data, then fix/revert per mod or file."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self._data: dict[str, FixerData] | None = None
        self._repo_dirs: dict[str, Path] | None = None
        self._structure: StructureData | None = None
        self._worker: TaskWorker | None = None
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
        self._busy_hold_timer = QTimer(self)
        self._busy_hold_timer.setSingleShot(True)
        self._busy_hold_timer.timeout.connect(self._busy_hold_expired)
        self._resize_drag_edges: Qt.Edge | None = None
        self._resize_press_global: QPoint | None = None
        self._resize_geometry: QRect | None = None
        self._edge_cursor: Qt.CursorShape | None = None
        self._root_node: ModNode | None = None
        self._mods_path: Path | None = None
        self._preset_name = ""
        self._log_last_message: str | None = None
        self._pending_analyzing_log: str | None = None
        self._last_analysis_line: str | None = None
        self._renaming_node: ModNode | None = None
        self._promoted: dict[str, str] = load_promoted()
        self._thumb_cache: dict[str, str] = {}
        self._images_cache: dict[str, list[Path]] = {}
        self._expected_removals: set[str] = set()
        self._missing_dismissed: set[str] = set()

        self.setWindowTitle("ZZZ Mod Housekeeper")
        self._settings = StateSettings()
        show_empty_setting = self._settings.value("show_empty_folders", True)
        self._show_empty_folders = show_empty_setting in (True, "true")
        self._show_enabled_only = self._settings.value("show_enabled_only", False) in (True, "true")
        self._show_console = self._settings.value("show_console", True) in (True, "true")
        self._build_ui()
        if sys.platform == "win32":
            try:
                set_corner_preference = getattr(
                    ctypes.windll.dwmapi, "DwmSetWindowAttribute"
                )
                preference = ctypes.c_int(_DWMWCP_ROUND)
                set_corner_preference(
                    int(self.winId()),
                    _DWMWA_WINDOW_CORNER_PREFERENCE,
                    ctypes.byref(preference),
                    ctypes.sizeof(preference),
                )
            except OSError:
                pass
        rescan_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        rescan_shortcut.activated.connect(self._start_analyze)
        self.installEventFilter(self)
        self.setMouseTracking(True)
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)
            widget.setMouseTracking(True)
        self.resize(750, 600)
        self.setMinimumSize(640, 420)
        self._refresh_actions()
        for variant in REPO_VARIANTS:
            self.append_log(
                f"Upstream: {REPO_VARIANTS[variant].repo_url}\n"
                f"Data repo: {default_cache_dir(variant)}"
            )
        geometry = _restored_geometry(self._settings.value("window_geometry"))
        if geometry is not None:
            self.restoreGeometry(geometry)
        restored_max = self._settings.value("window_maximized")
        if restored_max in (True, "true"):
            self.showMaximized()
        for column, default in _TREE_COLUMN_WIDTHS:
            try:
                width = int(
                    cast(int, self._settings.value(f"col_width_{column}", default))
                )
            except (TypeError, ValueError):
                width = default
            if width > 0:
                self._tree.setColumnWidth(column, width)
        self._saved_expanded_paths = set(
            _expanded_path_list(self._settings.value("tree_expanded", ""))
        )
        if self._restore_last_folder():
            self._on_load_data()
        else:
            self._check_hash_updates()
        elevated = _process_elevated()
        self.append_log(
            "Running elevated: "
            + ("yes (Explorer drag-and-drop will be blocked by Windows)" if elevated else "no")
        )

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setMouseTracking(True)
        layout = QVBoxLayout(central)

        self._title_bar = _TitleBar(self)
        self._build_hamburger_menu()
        layout.addWidget(self._title_bar)

        mods_row = QHBoxLayout()
        self._mods_edit = QLineEdit(central)
        self._mods_edit.setReadOnly(True)
        self._mods_edit.setPlaceholderText("Select your 3DMigoto mods folder…")
        self._browse_btn = QPushButton("Browse…", central)
        self._update_btn = QPushButton(_UPDATE_BUTTON, central)
        mods_row.addWidget(self._mods_edit, 1)
        mods_row.addWidget(self._browse_btn)
        mods_row.addWidget(self._update_btn)
        layout.addLayout(mods_row)

        self._tree = _ModsTree(self)
        self._tree.setColumnCount(len(_TREE_COLUMNS))
        self._tree.setHeaderLabels(_TREE_COLUMNS)
        self._tree.headerItem().setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
        self._tree.setItemDelegateForColumn(1, _CenteredCheckDelegate(self._tree))
        for column, width in _TREE_COLUMN_WIDTHS:
            self._tree.setColumnWidth(column, width)
        self._tree.header().setStretchLastSection(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setWordWrap(False)
        self._tree.setIndentation(14)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        accent = COLOR_ACCENT
        self._tree.setStyleSheet(
            "QTreeWidget::item:hover {"
            f" background-color: rgba({accent.red()}, {accent.green()}, {accent.blue()}, 40%);"
            " }"
            'QTreeWidget[dropActive="true"] {'
            f" border: 2px solid rgba({accent.red()}, {accent.green()}, {accent.blue()}, 60%);"
            " border-radius: 4px;"
            " }"
        )
        layout.addWidget(self._tree, 3)

        self._log_view = QPlainTextEdit(central)
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(5000)
        self._log_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        layout.addWidget(self._log_view, 1)
        self._log_view.setVisible(self._show_console)

        self.setCentralWidget(central)

        self._browse_btn.clicked.connect(self._on_browse)
        self._update_btn.clicked.connect(self._on_update_data)
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
        self.append_log(f"Mods folder: {chosen}")
        self._settings.setValue("mods_folder", chosen)
        if not self._data:
            self._on_load_data()
            return
        self._start_analyze()

    def _build_hamburger_menu(self) -> None:
        """Populate the title-bar hamburger with the app menu and a Presets submenu."""
        menu = self._title_bar.app_menu
        install = menu.addAction("Install mod…")
        install.triggered.connect(self._pick_import_archive)
        rescan = menu.addAction("Rescan")
        rescan.triggered.connect(self._start_analyze)
        menu.addSeparator()
        self._presets_menu = QMenu("Presets", self)
        menu.addMenu(self._presets_menu)
        self._rebuild_presets_menu()
        menu.addSeparator()
        enabled_only = menu.addAction("Show enabled only")
        enabled_only.setCheckable(True)
        enabled_only.setChecked(self._show_enabled_only)
        enabled_only.toggled.connect(self._on_show_enabled_toggled)
        show_empty = menu.addAction("Show empty folders")
        show_empty.setCheckable(True)
        show_empty.setChecked(self._show_empty_folders)
        show_empty.toggled.connect(self._on_show_empty_toggled)
        show_console = menu.addAction("Show console")
        show_console.setCheckable(True)
        show_console.setChecked(self._show_console)
        show_console.toggled.connect(self._on_show_console_toggled)
        about = menu.addAction("About")
        about.triggered.connect(self._show_about)

    def _rebuild_presets_menu(self) -> None:
        """Refresh the Presets submenu from the saved presets file."""
        menu = self._presets_menu
        menu.clear()
        save = menu.addAction("Save current as…")
        save.triggered.connect(self._save_preset)
        manage = menu.addAction("Manage…")
        manage.triggered.connect(self._manage_presets)
        names = preset_names()
        if names:
            menu.addSeparator()
            for name in names:
                entry = menu.addAction(name)
                entry.triggered.connect(
                    lambda _checked=False, preset=name: self._apply_preset(preset)
                )

    def _save_preset(self) -> None:
        """Prompt for a preset name and snapshot the currently enabled mods."""
        if self._root_node is None:
            self.append_log("Load a mods folder before saving presets")
            return
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        if not ok or not name.strip():
            return
        stripped = name.strip()
        if stripped in load_presets():
            confirm = QMessageBox.question(
                self,
                "Save preset",
                f"A preset named '{stripped}' already exists. Replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        mods_dir = Path(self._mods_edit.text().strip())
        save_preset(name, enabled_relative_paths(self._root_node, mods_dir))
        self.amuse_rina()
        self._rebuild_presets_menu()
        self.append_log(f"Preset saved: {stripped}")

    def _apply_preset(self, name: str) -> None:
        """Toggle mods to match a saved preset, then rescan."""
        if self._worker is not None:
            return
        if self._root_node is None or not self._data:
            self.append_log("Load a mods folder before applying presets")
            return
        mods_dir = Path(self._mods_edit.text().strip())
        paths = load_presets().get(name)
        if not paths:
            self.append_log(f"Preset not found: {name}")
            return
        enabled = frozenset(paths)
        missing = missing_preset_paths(self._root_node, mods_dir, enabled)
        if missing:
            self.append_log(
                "Preset paths not in mods folder: " + ", ".join(missing)
            )
        changes = preset_changes(self._root_node, mods_dir, enabled)
        if not changes:
            self.append_log(f"Preset '{name}' already matches the mods folder")
            return
        self._preset_name = name
        self.append_log(f"Applying preset '{name}'…")
        self._start_worker(apply_preset_worker(changes), self._on_apply_preset_done)

    def _on_apply_preset_done(self, result: object) -> None:
        if not isinstance(result, int):
            return
        self.append_log(
            f"Preset '{self._preset_name}' applied: {result} mod(s) toggled"
        )
        self.amuse_rina()
        QTimer.singleShot(0, self._start_analyze)

    def _manage_presets(self) -> None:
        """Delete or rename one saved preset via input dialogs."""
        names = preset_names()
        if not names:
            self.append_log("No presets to manage")
            return
        name, ok = QInputDialog.getItem(
            self, "Manage presets", "Preset:", names, 0, False
        )
        if not ok:
            return
        action, ok_action = QInputDialog.getItem(
            self, "Manage presets",
            f"Action for '{name}':", ["Delete", "Rename"], 0, False,
        )
        if not ok_action:
            return
        if action == "Delete":
            if delete_preset(name):
                self.append_log(f"Preset deleted: {name}")
                self._rebuild_presets_menu()
            return
        new_name, ok_new = QInputDialog.getText(
            self, "Rename preset", "New name:", text=name
        )
        if ok_new and new_name.strip() and rename_preset(name, new_name):
            self.append_log(f"Preset renamed: {name} -> {new_name.strip()}")
            self._rebuild_presets_menu()

    def _pick_import_archive(self) -> None:
        """Choose a mod archive, then open the destination dialog."""
        if not self._data:
            self.append_log("Load and analyze a mods folder before installing")
            return
        start = self._mods_edit.text().strip() or str(Path.home())
        archive, _ = QFileDialog.getOpenFileName(
            self, "Install mod archive", start, "Mod archives (*.zip *.rar *.7z)"
        )
        if not archive:
            return
        self._start_import_archive(Path(archive))

    def _start_import_archive(self, archive: Path) -> None:
        """Modal destination picker for a mod archive, then a background import."""
        if self._worker is not None or not self._data:
            return
        root = Path(self._mods_edit.text().strip())
        if not root.is_dir():
            self.append_log("Load the mods folder before installing")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Install mod archive")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(f"{archive.name}\nDestination subfolder (empty = mods root)", dialog)
        )
        combo = QComboBox(dialog)
        combo.setEditable(True)
        for child in sorted(root.iterdir(), key=lambda entry: entry.name.lower()):
            if child.is_dir():
                combo.addItem(child.name)
        combo.setCurrentText("")
        layout.addWidget(combo)
        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel", dialog)
        install = QPushButton("Install", dialog)
        cancel.clicked.connect(dialog.reject)
        install.clicked.connect(dialog.accept)
        buttons.addWidget(install)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        sub = combo.currentText().strip()
        destination = root if not sub else root / sub
        self._install_archive_to(archive, destination)

    def _install_archive_to(self, archive: Path, destination: Path) -> None:
        """Create the destination and background-import the archive into it."""
        destination.mkdir(parents=True, exist_ok=True)
        self.append_log(f"Installing {archive.name} -> {destination}…")
        self._start_worker(
            import_archive_worker(archive, destination), self._on_import_done
        )

    def _confirm_install(self, message: str) -> bool:
        """Modal Install/Cancel confirmation with Install as the leftmost button."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Install mod archive")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(message, dialog))
        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel", dialog)
        install = QPushButton("Install", dialog)
        cancel.clicked.connect(dialog.reject)
        install.clicked.connect(dialog.accept)
        buttons.addWidget(install)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self._restore_edge_cursor()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def handle_archive_drop(self, archive: Path, node: ModNode | None) -> None:
        """Confirm and install a dropped archive into a category or the root."""
        self._restore_edge_cursor()
        self.raise_()
        self.activateWindow()
        if self._worker is not None:
            self.append_log("Busy — wait for the current task to finish before installing")
            return
        root = Path(self._mods_edit.text().strip())
        if not root.is_dir():
            self.append_log("Select a mods folder before installing")
            return
        destination = root if node is None else node.path
        label = "the mods root" if node is None else f"'{_display_name(node)}'"
        if not self._confirm_install(f"Install '{archive.name}' into {label}?"):
            self.append_log("Install cancelled")
            return
        self._install_archive_to(archive, destination)

    def _on_import_done(self, result: object) -> None:
        if not isinstance(result, int):
            return
        self.append_log(f"Import finished: {result} file(s)")
        self.amuse_rina()
        QTimer.singleShot(0, self._start_analyze)

    def _on_show_empty_toggled(self, checked: bool) -> None:
        """Persist the show-empty-folders preference and re-filter the mods tree."""
        self._show_empty_folders = checked
        self._settings.setValue("show_empty_folders", checked)
        if self._root_node is not None:
            self._fill_mod_tree(self._root_node)

    def _on_show_enabled_toggled(self, checked: bool) -> None:
        """Persist the show-enabled-only preference and re-filter the mods tree."""
        self._show_enabled_only = checked
        self._settings.setValue("show_enabled_only", checked)
        if self._root_node is None:
            return
        if checked and self._node_items:
            self._saved_expanded_paths = self._save_expand_state()
        self._fill_mod_tree(self._root_node, self._saved_expanded_paths)

    def _on_show_console_toggled(self, checked: bool) -> None:
        """Collapse or restore the console log, persisting the choice."""
        self._show_console = checked
        self._log_view.setVisible(checked)
        self._settings.setValue("show_console", checked)

    def _open_mods_folder(self) -> None:
        """Open the current mods folder in the system file manager."""
        mods_dir = self._mods_edit.text().strip()
        if mods_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(mods_dir))

    def _show_about(self) -> None:
        """Show the About box."""
        _AboutDialog(self).exec()

    def _on_load_data(self) -> None:
        """Parse cloned data repos, or build a fallback dataset without them."""
        if self._worker is not None:
            return
        self.append_log("Loading hash data from local clones...")
        self._start_worker(load_all_data_worker(), self._on_data_loaded)

    def _on_update_data(self) -> None:
        """Update the data repos and parse them in one background worker."""
        if self._worker is not None:
            return
        self._loaded_via_update = True
        self.append_log("Updating upstream data (2048p + 1024p + buffers)...")
        self._start_worker(update_data_worker(), self._on_data_loaded)

    def _check_hash_updates(self) -> None:
        """Check upstream repos for newer commits without blocking the UI.

        Runs on its own worker slot (not the busy guard) so a slow or offline
        network never stalls the app; unknown results re-check on a 60 s retry.
        """
        if self._check_worker is not None:
            return
        if self._update_statuses is None:
            self.append_log("Checking upstream data for updates...")
        else:
            self.append_log("Retrying upstream data check...")
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
            self.append_log(f"Hash data check ({variant}): {outcome}")
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
            self.append_log(
                f"Data loaded ({variant}): {len(data.db.characters)} characters, "
                f"{len(data.chains)} chain sources"
            )
        self.append_log(
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

    def _on_analyze_done(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            return
        node, summary = result
        previous_root = self._root_node
        self._root_node = node
        previous_mods_path = self._mods_path
        self._mods_path = Path(self._mods_edit.text().strip())
        self._fill_mod_tree(node)
        line = _analysis_log_line(summary)
        pending = self._pending_analyzing_log
        self._pending_analyzing_log = None
        if line != self._last_analysis_line:
            if pending:
                self.append_log(pending)
            self.append_log(line)
            self._last_analysis_line = line
        self._prompt_missing_mods(previous_root, previous_mods_path)
        self._rebuild_presets_menu()

    def _prompt_missing_mods(
        self, previous_root: ModNode | None, previous_mods_path: Path | None
    ) -> None:
        """Offer locate/cleanup choices for mods missing after a rescan.

        Runs only when the mods folder is unchanged from the previous scan;
        fresh sessions (no previous root) and paths the app itself just
        renamed or deleted never prompt.
        """
        new_mods_path = Path(self._mods_edit.text().strip())
        if previous_root is None:
            return
        if previous_mods_path is None:
            return
        root_node = self._root_node
        if root_node is None:
            return
        previous_mods = previous_mods_path
        if previous_mods != new_mods_path:
            return
        stale = _stale_mod_refs(
            mod_relative_paths(previous_root, previous_mods),
            mod_relative_paths(root_node, new_mods_path),
            self._promoted,
            load_presets(),
            self._missing_dismissed | self._expected_removals,
        )
        if not stale:
            return
        dialog = _MissingModsDialog(new_mods_path, stale, self)
        dialog.exec()
        self._missing_dismissed.update(dialog.dismissed)
        self._expected_removals.update(dialog.dismissed)
        if dialog.retargeted:
            QTimer.singleShot(0, self._start_analyze)

    def _on_fix_mod(self, node: ModNode) -> None:
        """Scan the given scope and apply its fixes in one background job."""
        if self._worker is not None or not self._data:
            return
        if (
            node.variant is not None
            and self._data is not None
            and node.variant not in self._data
        ):
            self.append_log(
                f"{node.variant} data is not loaded — click '{_UPDATE_BUTTON}' first."
            )
            return
        suffix = " (currently DISABLED)" if node.disabled else ""
        if not self._confirm(
            "Fix",
            f"Fix '{_display_name(node)}{suffix}'?\n"
            f"{self._count_files(node)} file(s) will be scanned; "
            f"changed files are backed up in {default_backups_dir()}.",
        ):
            self.append_log("Fix cancelled")
            return
        self._fixing_name = _display_name(node)
        self._fixing_node = node
        self.append_log(f"Fixing '{_display_name(node)}'…")
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
            self.append_log(f"No fixes needed for '{self._fixing_name}' ({variant})")
        else:
            self.append_log(
                f"Fixed '{self._fixing_name}' ({variant}): "
                f"wrote {written} file(s), {total} change(s)"
            )
            self.amuse_rina()
        QTimer.singleShot(0, self._deferred_scope_refresh)

    def _on_revert_mod(self, node: ModNode) -> None:
        """Enumerate the given scope's fix-backup history in the background."""
        if self._worker is not None or not self._data:
            return
        self._fixing_name = _display_name(node)
        self._fixing_node = node
        self.append_log(f"Checking fix history for '{_display_name(node)}'…")
        self._start_worker(
            backup_chains_worker(self._mods_edit.text().strip(), self._scope_for(node)),
            self._on_chains_done,
        )

    def _on_chains_done(self, chains: object) -> None:
        if not isinstance(chains, dict):
            return
        chain_list = list(chains.values())
        if not chain_list:
            self.append_log(f"Nothing to revert in '{self._fixing_name}'")
            return
        mods_dir = self._mods_edit.text().strip()
        kinds: dict[Path, str] = {}
        for chain in chain_list:
            live = chain.live
            if live.suffix == ".ini":
                kinds[live] = "hash/section fixes"
            elif live.suffix == ".buf":
                kinds[live] = (
                    marker_kind(default_backups_dir(), Path(mods_dir), live)
                    or blend_marker_kind(default_backups_dir(), Path(mods_dir), live)
                    or "buffer fix"
                )
            else:
                kinds[live] = "binary fix"
        dialog = _RevertDialog(chain_list, self, kinds=kinds)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choices = dialog.selected_choices()
        if not choices:
            self.append_log("No states selected for restore")
            return
        self.append_log(f"Restoring {len(choices)} file(s)…")
        self._start_worker(
            revert_worker(choices, mods_dir, default_backups_dir()),
            self._on_revert_done,
        )

    def _on_revert_done(self, count: object) -> None:
        if not isinstance(count, int):
            return
        self.append_log(f"Restored {count} file(s) in '{self._fixing_name}'")
        self.amuse_rina()
        QTimer.singleShot(0, self._deferred_scope_refresh)

    def _on_worker_error(self, message: str) -> None:
        self._pending_analyzing_log = None
        self.append_log(f"ERROR: {message}")

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
        self._restore_edge_cursor()
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
        worker.log.connect(self.append_log)
        worker.done.connect(on_done)
        worker.error.connect(on_error)
        worker.finished.connect(on_finished)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(partial(_forget_worker, worker))

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._busy_hold_timer.stop()
            self._title_bar.set_busy(True)
        elif not self._busy_hold_timer.isActive():
            self._busy_hold_timer.start(_BUSY_HOLD_MS)
        self._refresh_actions()

    def _busy_hold_expired(self) -> None:
        """End the busy wash after the minimum visible busy duration."""
        self._title_bar.set_busy(False)

    def _refresh_actions(self) -> None:
        busy = self._worker is not None
        self._browse_btn.setEnabled(not busy)
        pending = self._update_statuses is None or self._check_worker is not None
        enabled, label = update_button_state(
            self._update_statuses or {}, pending
        )
        self._update_btn.setText(label)
        self._update_btn.setEnabled(enabled and not busy)

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

    @staticmethod
    def _count_mods(node: ModNode) -> int:
        """Number of mod nodes in node's subtree, including node itself."""
        count = 0
        stack = [node]
        while stack:
            current = stack.pop()
            if current.kind == "mod":
                count += 1
            stack.extend(current.children)
        return count

    def _start_analyze(self) -> None:
        """Auto-analyze the current mods folder into the mods overview."""
        if self._worker is not None:
            return
        mods_dir = self._mods_edit.text().strip()
        if not mods_dir:
            return
        datasets = self._data or {DEFAULT_VARIANT: empty_fixer_data()}
        self._pending_analyzing_log = "Analyzing mods…"
        self._start_worker(
            analyze_worker(mods_dir, datasets, self._structure),
            self._on_analyze_done,
        )

    def _restore_last_folder(self) -> bool:
        """Restore the last used mods folder from settings; True when restored."""
        saved = self._settings.value("mods_folder", "")
        if not isinstance(saved, str) or not saved:
            return False
        if not Path(saved).is_dir():
            fallback = project_root() / "mods"
            if fallback.is_dir():
                self._settings.setValue("mods_folder", str(fallback))
                self._mods_edit.setText(str(fallback))
                self.append_log(f"Mods folder (relocated): {fallback}")
                return True
            self.append_log(f"Saved mods folder no longer exists: {saved}")
            return False
        self._mods_edit.setText(saved)
        self.append_log(f"Mods folder (restored): {saved}")
        return True

    def _fill_mod_tree(
        self, root_node: ModNode, expanded: set[str] | None = None
    ) -> None:
        """Populate the mods overview; the mods root's children are the top level."""
        if expanded is None:
            expanded = (
                self._saved_expanded_paths
                if not self._node_items
                else self._save_expand_state()
            )
        self._tree.setUpdatesEnabled(False)
        try:
            self._node_items.clear()
            self._tree.clear()
            self._filling_tree = True
            try:
                for child in root_node.children:
                    if self._show_enabled_only and not _enabled_view_includes(child):
                        continue
                    if not self._show_empty_folders and child.empty_folder:
                        continue
                    self._add_mod_item(self._tree.invisibleRootItem(), child)
            finally:
                self._filling_tree = False
            self._restore_expand_state(expanded)
            if self._show_enabled_only:
                for item in self._node_items.values():
                    node = item.data(0, Qt.ItemDataRole.UserRole)
                    if (
                        isinstance(node, ModNode)
                        and node.kind == "category"
                        and node.children
                    ):
                        item.setExpanded(True)
        finally:
            self._tree.setUpdatesEnabled(True)
            self._tree.viewport().update()

    def _save_expand_state(self) -> set[str]:
        """Path strings of every currently expanded mod-tree item."""
        expanded: set[str] = set()
        for item in self._node_items.values():
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if item.isExpanded() and isinstance(node, ModNode):
                expanded.add(node.path.as_posix())
        return expanded

    def _restore_expand_state(self, expanded: set[str]) -> None:
        """Re-expand every present item whose mod path is in the saved set."""
        for item in self._node_items.values():
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(node, ModNode) and node.path.as_posix() in expanded:
                item.setExpanded(True)

    @staticmethod
    def _apply_version_row(node: ModNode, item: QTreeWidgetItem) -> None:
        """Updates/hashes cells for a node with a version; no-op when unversioned."""
        version = node.version
        if version is None:
            return
        as_mod = node.kind == "mod"
        updates = updates_text(version, as_mod)
        hashes = hashes_text(version, as_mod)
        item.setText(2, updates)
        item.setText(3, hashes)
        item.setToolTip(2, updates_tooltip(updates))
        item.setToolTip(3, hashes_tooltip(version) if hashes else "")
        color = updates_color(version, as_mod)
        if color is not None:
            item.setForeground(2, color)
        else:
            item.setData(2, Qt.ItemDataRole.ForegroundRole, None)

    @staticmethod
    def _apply_aggregate_row(node: ModNode, item: QTreeWidgetItem) -> None:
        """Updates cell for an unversioned node from its children's signal; empty clears color."""
        signal = aggregate_updates(node)
        item.setText(2, signal)
        item.setToolTip(2, updates_tooltip(signal))
        if signal == BROKEN_BUFFERS_SIGNAL:
            item.setForeground(2, COLOR_UPDATED)
        elif signal == OLD_HASH_SIGNAL:
            item.setForeground(2, COLOR_UPDATED)
        elif signal == SECTIONS_SIGNAL:
            item.setForeground(2, COLOR_STRUCTURAL)
        elif signal:
            item.setForeground(2, COLOR_CURRENT)
        else:
            item.setData(2, Qt.ItemDataRole.ForegroundRole, None)

    def _thumb_data_uri(self, mod_dir: Path, image: Path) -> str:
        """JPEG data URI of image area-normalized to ~48.4k px**2, "" on failure.

        The scale is sqrt(48,400 / (w * h)), never upscaled (capped at 1.0)
        and capped at 300px per dimension for extreme aspect ratios.  Alpha
        is flattened onto the app window tone so JPEG keeps the look of the
        surrounding view.  The memo key is the same ``mod_dir/relative``
        pair as before; only the payload changed.
        """
        try:
            relative = image.relative_to(mod_dir).as_posix()
        except ValueError:
            relative = image.as_posix()
        key = f"{mod_dir.as_posix()}/{relative}"
        cached = self._thumb_cache.get(key)
        if cached is not None:
            return cached
        try:
            reader = QImageReader(str(image))
            natural = reader.size()
            if natural.width() <= 0 or natural.height() <= 0:
                return ""
            scale = math.sqrt(_THUMB_AREA_PX / (natural.width() * natural.height()))
            scale = min(
                scale,
                1.0,
                _THUMB_MAX_DIM_PX / natural.width(),
                _THUMB_MAX_DIM_PX / natural.height(),
            )
            reader.setScaledSize(
                QSize(
                    max(1, round(natural.width() * scale)),
                    max(1, round(natural.height() * scale)),
                )
            )
            decoded = reader.read()
            if decoded.isNull():
                return ""
            if decoded.hasAlphaChannel():
                canvas = QImage(decoded.size(), QImage.Format.Format_RGB32)
                canvas.fill(QColor(243, 243, 243))
                painter = QPainter(canvas)
                painter.drawImage(0, 0, decoded)
                painter.end()
                encoded = canvas
            else:
                encoded = decoded
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            writer = QImageWriter(buffer, b"JPEG")
            writer.setQuality(85)
            if not writer.write(encoded):
                return ""
            uri = "data:image/jpeg;base64," + base64.b64encode(buffer.data().data()).decode("ascii")
        except (Exception,):
            return ""
        self._thumb_cache[key] = uri
        return uri

    def _clear_thumb_memo(self, mod_dir: Path) -> None:
        """Drop cached thumbnails and preview lists under mod_dir."""
        prefix = mod_dir.as_posix() + "/"
        for key in [key for key in self._thumb_cache if key.startswith(prefix)]:
            del self._thumb_cache[key]
        self._images_cache.pop(mod_dir.as_posix(), None)

    def _promoted_image(self, mod_dir: Path) -> Path | None:
        """Existing promoted preview image of mod_dir, else None."""
        if self._mods_path is None:
            return None
        try:
            key = canonical_key(mod_dir.relative_to(self._mods_path).as_posix())
        except ValueError:
            return None
        relative = self._promoted.get(key, "")
        if not relative:
            return None
        image = mod_dir / relative
        return image if image.is_file() else None

    def _cached_mod_images(self, mod_dir: Path) -> list[Path]:
        """Preview list of mod_dir, computed on first use; [] when unreadable."""
        key = mod_dir.as_posix()
        cached = self._images_cache.get(key)
        if cached is None:
            try:
                cached = _mod_images(mod_dir)
            except OSError:
                cached = []
            self._images_cache[key] = cached
        return cached

    def mod_tooltip_html(self, node: ModNode) -> str:
        """Mod-column tooltip for a mod: preview image, or the plain path text."""
        image = self._promoted_image(node.path)
        if image is None:
            images = self._cached_mod_images(node.path)
            if images:
                image = images[0]
        if image is not None:
            uri = self._thumb_data_uri(node.path, image)
            if uri:
                return f"<img src='{uri}'>"
        tooltip = str(node.path)
        version = node.version
        if version is not None and version.breaks_label:
            tooltip += f"\nBreaking change: {version.breaks_label}"
        return tooltip

    def _mod_row_for(self, mod_dir: Path) -> tuple[ModNode, QTreeWidgetItem] | None:
        """Tree item of the mod node under mod_dir, or None when absent."""
        for item in self._node_items.values():
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(node, ModNode) and node.path == mod_dir:
                return node, item
        return None

    def _refresh_gallery_tooltip(self, mod_dir: Path) -> None:
        """Drop thumbnail memos and rebuild one mod row's preview tooltip."""
        self._clear_thumb_memo(mod_dir)
        row = self._mod_row_for(mod_dir)
        if row is not None:
            row[1].setToolTip(0, self.mod_tooltip_html(row[0]))

    def _on_gallery_images_changed(self, mod_dir: Path) -> None:
        """A gallery image mutation finished; refresh that mod row's tooltip."""
        self._refresh_gallery_tooltip(mod_dir)

    def _on_gallery_promoted(self, mod_dir: Path, image: Path | None) -> None:
        """Store or clear the mod's promoted preview image, then refresh its row."""
        if self._mods_path is None:
            return
        try:
            key = canonical_key(mod_dir.relative_to(self._mods_path).as_posix())
        except ValueError:
            return
        if image is None:
            self._promoted.pop(key, None)
        else:
            try:
                self._promoted[key] = _clean_disabled_leaf(
                    image.relative_to(mod_dir).as_posix()
                )
            except ValueError:
                return
        save_promoted(self._promoted)
        self._refresh_gallery_tooltip(mod_dir)

    def _add_mod_item(self, parent_item: QTreeWidgetItem, node: ModNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([_display_name(node), "", "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        if node.kind == "mod":
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                1, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
        if node.version is not None:
            self._apply_version_row(node, item)
        else:
            self._apply_aggregate_row(node, item)
        self._node_items[id(node)] = item
        parent_item.addChild(item)
        for child in node.children:
            if self._show_enabled_only and not _enabled_view_includes(child):
                continue
            if not self._show_empty_folders and child.empty_folder:
                continue
            self._add_mod_item(item, child)
        return item

    def _deferred_scope_refresh(self) -> None:
        self._refresh_scope(self._fixing_node)

    def _deferred_disabled_refill(self) -> None:
        """Re-analyze the just-disabled mod's scope, then re-filter the tree."""
        self._deferred_scope_refresh()
        if self._root_node is not None:
            self._fill_mod_tree(self._root_node)

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
            self.append_log(f"ERROR: {exc}")
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
        if column != 1 or self._filling_tree:
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(node, ModNode) or node.kind != "mod":
            return
        desired_enabled = item.checkState(1) == Qt.CheckState.Checked
        if desired_enabled != node.disabled:
            return
        if self._worker is not None:
            item.setCheckState(
                1, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
            self.append_log("Busy — try again when the current task finishes.")
            return
        try:
            new_path = set_mod_enabled(node.path, desired_enabled)
        except FileExistsError:
            self.append_log(
                f"Cannot {'enable' if desired_enabled else 'disable'} "
                f"'{node.name}': target folder already exists"
            )
            item.setCheckState(
                1, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
        except OSError as exc:
            self.append_log(f"ERROR: {exc}")
            item.setCheckState(
                1, Qt.CheckState.Unchecked if node.disabled else Qt.CheckState.Checked
            )
        else:
            node.name = new_path.name
            node.disabled = not desired_enabled
            item.setText(0, _display_name(node))
            self.append_log(
                f"{'Enabled' if desired_enabled else 'Disabled'} '{_display_name(node)}'"
            )
            retarget_subtree_paths(node, new_path)
            self._fixing_node = node
            if self._show_enabled_only and not desired_enabled:
                QTimer.singleShot(0, self._deferred_disabled_refill)
            else:
                QTimer.singleShot(0, self._deferred_scope_refresh)

    def _show_root_context_menu(self, position: QPoint) -> None:
        """Right-click menu on blank space: expand/collapse the tree, open, or create mods folder."""
        menu = QMenu(self)
        expand_all = menu.addAction("Expand all")
        expand_all.triggered.connect(self._tree.expandAll)
        collapse_all = menu.addAction("Collapse all")
        collapse_all.triggered.connect(self._tree.collapseAll)
        menu.addSeparator()
        open_folder = menu.addAction("Open mods folder")
        create_folder = menu.addAction("Create new folder…")
        self._restore_edge_cursor()
        chosen = menu.exec(self._tree.viewport().mapToGlobal(position))
        if chosen is open_folder:
            self._open_mods_folder()
        elif chosen is create_folder:
            self._create_new_folder()

    def _create_new_folder(self) -> None:
        """Ask for a name and create a new folder in the mods root."""
        if self._worker is not None or not self._data:
            self.append_log("Load and analyze a mods folder before creating folders")
            return
        mods_dir = Path(self._mods_edit.text().strip())
        if not mods_dir.is_dir():
            self.append_log("Load the mods folder before creating folders")
            return
        name, ok = QInputDialog.getText(self, "Create new folder", "Folder name:", text="New folder")
        if not ok:
            return
        try:
            created = create_mod_folder(mods_dir, name)
        except (ValueError, OSError) as exc:
            self.append_log(str(exc))
            return
        self.append_log(f"Created folder '{created.name}'")
        QTimer.singleShot(0, self._start_analyze)

    def _on_tree_context_menu(self, position: QPoint) -> None:
        """Right-click menu on a mod, category or file row: info and fix actions."""
        item = self._tree.itemAt(position)
        if item is None:
            self._show_root_context_menu(position)
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if (
            not isinstance(data, ModNode)
            or data.kind not in ("mod", "category", "file")
        ):
            return
        menu = QMenu(self)
        if data.kind == "file":
            self._append_fix_revert_actions(menu, data)
            self._restore_edge_cursor()
            menu.exec(self._tree.viewport().mapToGlobal(position))
            return
        if data.kind == "mod":
            open_mod = menu.addAction("Open mod folder")
            open_backup = menu.addAction("Open backup folder")
            rename = menu.addAction("Rename…")
            delete = menu.addAction("Delete…")
            info = menu.addAction("Mod info…")
            preview = menu.addAction("Preview images…")
            self._append_fix_revert_actions(menu, data)
            self._restore_edge_cursor()
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
            elif chosen is rename:
                self._on_rename_mod(data)
            elif chosen is delete:
                self._on_delete_mod(data)
            elif chosen is info:
                self._show_mod_info(data)
            elif chosen is preview:
                self._show_preview_gallery(data)
        else:
            open_folder = menu.addAction("Open folder")
            rename = menu.addAction("Rename…")
            delete = menu.addAction("Delete…")
            self._append_fix_revert_actions(menu, data)
            self._restore_edge_cursor()
            chosen = menu.exec(self._tree.viewport().mapToGlobal(position))
            if chosen is open_folder:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(data.path)))
            elif chosen is rename:
                self._on_rename_mod(data)
            elif chosen is delete:
                self._on_delete_mod(data)

    def _append_fix_revert_actions(self, menu: QMenu, node: ModNode) -> None:
        """Add Fix and conditional Revert actions for the right-clicked node."""
        mods_dir_text = self._mods_edit.text().strip()
        ready = (
            self._worker is None
            and _has_hash_knowledge(self._data or {})
            and bool(mods_dir_text)
        )
        if not menu.isEmpty():
            menu.addSeparator()
        count = 1 if node.kind == "file" else self._count_files(node)
        label = f"Fix… - {count} file" if count == 1 else f"Fix… - {count} files"
        fix_action = menu.addAction(label)
        fix_action.triggered.connect(
            lambda checked=False, target=node: self._on_fix_mod(target)
        )
        fix_action.setEnabled(ready)
        if mods_dir_text and _scope_has_backups(
            Path(mods_dir_text), default_backups_dir(), node.path
        ):
            revert_action = menu.addAction("Revert fix…")
            revert_action.triggered.connect(
                lambda checked=False, target=node: self._on_revert_mod(target)
            )
            revert_action.setEnabled(ready)

    def _on_rename_mod(self, node: ModNode) -> None:
        """Prompt for a new display name and rename the folder in the background."""
        if self._worker is not None:
            self.append_log("Busy — try again when the current task finishes.")
            return
        mods_dir = Path(self._mods_edit.text().strip())
        if not mods_dir.is_dir():
            self.append_log("Load the mods folder before renaming")
            return
        name, ok = QInputDialog.getText(
            self,
            "Rename",
            f"New name for '{_display_name(node)}':",
            text=_display_name(node),
        )
        if not ok:
            return
        self._renaming_node = node
        try:
            rel = node.path.relative_to(mods_dir)
        except ValueError:
            pass
        else:
            prefix = "" if rel.parent == Path(".") else rel.parent.as_posix() + "/"
            if node.kind == "mod":
                old_entry = prefix + node.path.name.removeprefix(_DISABLED_PREFIX)
            else:
                old_entry = prefix + node.path.name
            self._expected_removals.add(old_entry)
        self._start_worker(
            rename_folder_worker(mods_dir, node.path, name, node.kind),
            self._on_rename_done,
        )

    def _on_rename_done(self, result: object) -> None:
        """Apply a finished rename to the tree node, item and subtree paths."""
        node = self._renaming_node
        self._renaming_node = None
        if not isinstance(result, Path) or node is None:
            return
        node.name = result.name
        node.disabled = node.name.startswith(_DISABLED_PREFIX)
        retarget_subtree_paths(node, result)
        item = self._node_items.get(id(node))
        if item is not None:
            item.setText(0, _display_name(node))
        self._fixing_node = node
        QTimer.singleShot(0, self._deferred_scope_refresh)

    def _on_delete_mod(self, node: ModNode) -> None:
        """Confirm and delete a mod or category folder and its tracked state."""
        if self._worker is not None:
            self.append_log("Busy — try again when the current task finishes.")
            return
        mods_dir = Path(self._mods_edit.text().strip())
        if not mods_dir.is_dir():
            self.append_log("Load the mods folder before deleting")
            return
        display = _display_name(node)
        if node.kind == "mod":
            text = (
                f"Delete '{display}' permanently?\n"
                f"{self._count_files(node)} .ini file(s) will be removed, "
                "including its fix-backup history."
            )
        else:
            mods_inside = self._count_mods(node)
            if mods_inside:
                text = (
                    f"Delete category '{display}' permanently?\n"
                    f"It contains {mods_inside} mod(s); they and their "
                    "fix-backup histories will be removed."
                )
            else:
                text = f"Delete empty category '{display}' permanently?"
        if not self._confirm("Delete", text):
            self.append_log("Delete cancelled")
            return
        try:
            rel = node.path.relative_to(mods_dir)
        except ValueError:
            pass
        else:
            prefix = "" if rel.parent == Path(".") else rel.parent.as_posix() + "/"
            if node.kind == "mod":
                old_entry = prefix + node.path.name.removeprefix(_DISABLED_PREFIX)
            else:
                old_entry = prefix + node.path.name
            self._expected_removals.add(old_entry)
        self._start_worker(
            delete_folder_worker(mods_dir, node.path, node.kind),
            self._on_delete_done,
        )

    def _on_delete_done(self, result: object) -> None:
        """Rescan the mods overview after a finished folder deletion."""
        if not isinstance(result, tuple) or len(result) != 2:
            return
        deleted, _file_count = result
        if not isinstance(deleted, Path):
            return
        self._fixing_node = None
        QTimer.singleShot(0, self._start_analyze)

    def _show_mod_info(self, node: ModNode) -> None:
        """Open the read-only mod info summary dialog for one mod."""
        self._mod_info_dialog = _ModInfoDialog(node.path, self)
        self._mod_info_dialog.exec()

    def _show_preview_gallery(self, node: ModNode) -> None:
        """Open the preview image gallery for one mod."""
        promoted = self._promoted_image(node.path)
        self._preview_gallery_dialog = _PreviewGallery(
            node.path,
            _display_name(node),
            self,
            on_change=self._on_gallery_images_changed,
            on_promote=self._on_gallery_promoted,
            promoted_name=promoted.name if promoted is not None else None,
        )
        self._preview_gallery_dialog.exec()

    def append_log(self, message: str) -> None:
        if message == self._log_last_message:
            return
        self._log_last_message = message
        self._log_view.appendPlainText(message)

    def amuse_rina(self) -> None:
        """Silent visual feedback: Rina plays a random little reaction."""
        self._title_bar.amuse_rina()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Track the mouse for manual edge resizing and mirror the hover cursor."""
        if not isinstance(event, QMouseEvent):
            return False
        pos = self.mapFromGlobal(event.globalPosition().toPoint())
        if event.type() == QEvent.Type.MouseButtonPress:
            if (
                event.button() == Qt.MouseButton.LeftButton
                and not self.isMaximized()
            ):
                edges = self._resize_edges(pos)
                if edges is not None:
                    self._resize_drag_edges = edges
                    self._resize_press_global = event.globalPosition().toPoint()
                    self._resize_geometry = self.geometry()
                    self._restore_edge_cursor()
                    self.grabMouse(self._resize_cursor(pos))
                    return True
            return False
        if event.type() == QEvent.Type.MouseMove:
            if (
                self._resize_drag_edges is not None
                and self._resize_press_global is not None
                and self._resize_geometry is not None
            ):
                self._apply_resize_drag(event.globalPosition().toPoint())
                return True
            if self.isMaximized():
                self._restore_edge_cursor()
            else:
                self._update_edge_cursor(self._resize_cursor(pos))
            return False
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and self._resize_drag_edges is not None
        ):
            self._finish_resize_drag()
            return True
        return False

    def _apply_resize_drag(self, global_pos: QPoint) -> None:
        drag_edges = self._resize_drag_edges
        press_global = self._resize_press_global
        base = self._resize_geometry
        if drag_edges is None or press_global is None or base is None:
            return
        delta = global_pos - press_global
        min_width = self.minimumWidth()
        min_height = self.minimumHeight()
        x = base.x()
        y = base.y()
        width = base.width()
        height = base.height()
        if drag_edges & Qt.Edge.LeftEdge:
            x = min(base.x() + delta.x(), base.right() - min_width + 1)
            width = base.right() - x + 1
        if drag_edges & Qt.Edge.TopEdge:
            y = min(base.y() + delta.y(), base.bottom() - min_height + 1)
            height = base.bottom() - y + 1
        if drag_edges & Qt.Edge.RightEdge:
            width = max(base.width() + delta.x(), min_width)
        if drag_edges & Qt.Edge.BottomEdge:
            height = max(base.height() + delta.y(), min_height)
        self.setGeometry(x, y, max(width, min_width), max(height, min_height))

    def _finish_resize_drag(self) -> None:
        self._restore_edge_cursor()
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self._resize_drag_edges = None
        self._resize_press_global = None
        self._resize_geometry = None

    def _resize_edges(self, pos: QPoint) -> Qt.Edge | None:
        rect = self.rect()
        left = pos.x() <= rect.left() + _RESIZE_MARGIN
        right = pos.x() >= rect.right() - _RESIZE_MARGIN
        top = pos.y() <= rect.top() + _RESIZE_MARGIN
        bottom = pos.y() >= rect.bottom() - _RESIZE_MARGIN
        if not (left or right or top or bottom):
            return None
        edges = Qt.Edge(0)
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _resize_cursor(self, pos: QPoint) -> Qt.CursorShape:
        rect = self.rect()
        left = pos.x() <= rect.left() + _RESIZE_MARGIN
        right = pos.x() >= rect.right() - _RESIZE_MARGIN
        top = pos.y() <= rect.top() + _RESIZE_MARGIN
        bottom = pos.y() >= rect.bottom() - _RESIZE_MARGIN
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (left and bottom) or (right and top):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _update_edge_cursor(self, shape: Qt.CursorShape) -> None:
        """Show the OS resize cursor while hovering a window edge."""
        if shape is Qt.CursorShape.ArrowCursor:
            self._restore_edge_cursor()
            return
        if self._edge_cursor is shape:
            return
        self._restore_edge_cursor()
        QGuiApplication.setOverrideCursor(QCursor(shape))
        self._edge_cursor = shape

    def _restore_edge_cursor(self) -> None:
        """Drop the edge-resize cursor override if one is active."""
        if self._edge_cursor is None:
            return
        QGuiApplication.restoreOverrideCursor()
        self._edge_cursor = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self._restore_edge_cursor()
        self._settings.setValue(
            "window_geometry", self.saveGeometry().data().hex()
        )
        for column, _ in _TREE_COLUMN_WIDTHS:
            self._settings.setValue(f"col_width_{column}", self._tree.header().sectionSize(column))
        self._settings.setValue(
            "tree_expanded",
            json.dumps(
                sorted(
                    self._saved_expanded_paths
                    if self._show_enabled_only
                    else self._save_expand_state()
                )
            ),
        )
        self._settings.setValue("window_maximized", bool(self.isMaximized()))
        self._check_retry_timer.stop()
        for worker in (self._worker, self._check_worker):
            if worker is not None and worker.isRunning():
                worker.wait()
        super().closeEvent(event)
