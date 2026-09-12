"""Main window of the ZZZ Mod Housekeeper GUI.

Selecting a mod, subfolder, or .ini file in the mods overview enables
per-scope "Fix" and "Revert" over the fix-backup history.
"""

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import cast

# noinspection PyPackageRequirements
from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QMimeData,
    QPersistentModelIndex,
    QObject,
    QPoint,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QUrl)
# noinspection PyPackageRequirements
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFontDatabase,
    QGradient,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPalette,
    QPixmap)
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
    create_mod_folder,
    retarget_subtree_paths,
    set_mod_enabled,
)
from ..modinfo import read_mod_author, scan_mod_info
from ..presets import (delete_preset, enabled_relative_paths, load_presets, missing_preset_paths, preset_changes, preset_names, rename_preset, save_preset)
from ..repo import REPO_VARIANTS, changelog_path, default_cache_dir, project_root
from ..structure import StructureData
from .worker import (
    _UPDATE_BUTTON,
    TaskWorker,
    analyze_worker,
    apply_preset_worker,
    backup_chains_worker,
    check_updates_worker,
    fix_mod_worker,
    import_archive_worker,
    load_all_data_worker,
    revert_worker,
    update_data_worker,
)

_TREE_COLUMNS = ["Mod", "Enable", "Updates", "Hashes"]
_TREE_COLUMN_WIDTHS = ((0, 240), (1, 70), (2, 150), (3, 210))

COLOR_UPDATED = QColor("#c47f00")
COLOR_CURRENT = QColor(Qt.GlobalColor.darkGreen)
COLOR_STRUCTURAL = QColor("#808080")

_ACTIVE_WORKERS: set[TaskWorker] = set()
_DISABLED_PREFIX = "DISABLED_"
_BUSY_HOLD_MS = 600
_ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")


def _display_name(node: ModNode) -> str:
    return node.name.removeprefix(_DISABLED_PREFIX)


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


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _mod_images(mod_dir: Path) -> list[Path]:
    """Preview-image files under mod_dir, sorted by path."""
    images: list[Path] = []
    for path in sorted(mod_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS:
            images.append(path)
    return images


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
        self.resize(480, 520)
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


class _PreviewGallery(QDialog):
    """Thumbnail gallery of a mod's preview images."""

    def __init__(
        self, mod_dir: Path, mod_name: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Preview — {mod_name}")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(0, 320)
        layout.addWidget(self._image_label, 1)
        images = _mod_images(mod_dir)
        if not images:
            self._image_label.setText("No preview images found.")
            return
        self._list = QListWidget(self)
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(96, 96))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setWordWrap(True)
        for path in images:
            item = QListWidgetItem(QIcon(str(path)), path.name, self._list)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
        self._list.setCurrentRow(0)
        self._list.currentItemChanged.connect(self._on_preview_changed)
        self._on_preview_changed(self._list.currentItem())
        layout.addWidget(self._list, 1)

    def _on_preview_changed(self, item: QListWidgetItem | None) -> None:
        if item is None:
            self._image_label.clear()
            self._image_label.setText("")
            return
        pixmap = QPixmap(str(item.data(Qt.ItemDataRole.UserRole)))
        if pixmap.isNull():
            self._image_label.setPixmap(QPixmap())
            self._image_label.setText("Could not load image.")
            return
        self._image_label.setText("")
        scaled = pixmap.scaled(
            self._image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)


_RESIZE_MARGIN = 8


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
        icon_label = QLabel(self)
        icon_label.setPixmap(window.windowIcon().pixmap(24, 24))
        layout.addWidget(icon_label)
        self._menu_button = QToolButton(self)
        self._menu_button.setText("☰")
        self._menu_button.setAutoRaise(True)
        self._menu_button.setFixedSize(28, 28)
        self.app_menu = QMenu(self)
        self._menu_button.clicked.connect(self._show_menu)
        layout.addWidget(self._menu_button)
        layout.addWidget(QLabel("ZZZ Mod Housekeeper", self))
        layout.addStretch(1)
        self._min_button = QToolButton(self)
        self._min_button.setText("─")
        self._min_button.setAutoRaise(True)
        self._min_button.setFixedSize(28, 28)
        self._min_button.clicked.connect(window.showMinimized)
        layout.addWidget(self._min_button)
        self._max_button = QToolButton(self)
        self._max_button.setAutoRaise(True)
        self._max_button.setFixedSize(28, 28)
        self._max_button.clicked.connect(self._toggle_maximized)
        layout.addWidget(self._max_button)
        self._close_button = QToolButton(self)
        self._close_button.setText("✕")
        self._close_button.setAutoRaise(True)
        self._close_button.setFixedSize(28, 28)
        self._close_button.clicked.connect(window.close)
        layout.addWidget(self._close_button)
        self._sync_max_glyph()
        self._busy = False
        self._busy_phase = 0.0
        self._base = self.palette().color(QPalette.ColorRole.Window)
        self._accent = QColor(120, 170, 230)
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(50)
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
        self._busy_phase = 0.0
        if busy:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
        self.update()

    def _tick_busy(self) -> None:
        self._busy_phase = (self._busy_phase + 0.025) % 1.0
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
        """Left-to-right gradient with a soft accent band at the busy phase."""
        gradient = QLinearGradient(0.0, 0.0, 1.0, 0.0)
        gradient.setCoordinateMode(QGradient.CoordinateMode.ObjectBoundingMode)
        band = self._blend(self._base, self._accent, 0.6)
        core = self._blend(self._base, self._accent, 0.85)
        stops = [
            (0.0, self._base),
            (max(0.0, self._busy_phase - 0.22), self._base),
            (max(0.0, self._busy_phase - 0.1), band),
            (self._busy_phase, core),
            (min(1.0, self._busy_phase + 0.1), band),
            (min(1.0, self._busy_phase + 0.22), self._base),
            (1.0, self._base),
        ]
        by_position: dict[float, QColor] = {}
        for position, color in stops:
            by_position[position] = color
        for position in sorted(by_position):
            gradient.setColorAt(position, by_position[position])
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
        check.rect.moveCenter(option.rect.center())
        raw = index.data(Qt.ItemDataRole.CheckStateRole)
        current = raw.value if hasattr(raw, "value") else raw
        state_mask: QStyle.StateFlag = QStyle.StateFlag.State_On
        state_mask |= QStyle.StateFlag.State_Off
        state_mask |= QStyle.StateFlag.State_NoChange
        check.state &= ~state_mask
        if current == 2:
            check.state |= QStyle.StateFlag.State_On
        elif current == 1:
            check.state |= QStyle.StateFlag.State_NoChange
        else:
            check.state |= QStyle.StateFlag.State_Off
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck, check, painter, widget
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
        check_rect.moveCenter(option.rect.center())
        if not check_rect.contains(event.position().toPoint()):
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

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self._accept_or_ignore(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._accept_or_ignore(event)

    def dropEvent(self, event: QDropEvent) -> None:
        archive = self._drop_archive(event.mimeData())
        node = self._target_node(event.position().toPoint())
        if archive is None or not self._accepts(node):
            if archive is None:
                self._reject_drop(event.mimeData())
            event.ignore()
            return
        event.acceptProposedAction()
        self._window.handle_archive_drop(archive, node)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Clear the selection when a press lands on blank space."""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.itemAt(event.position().toPoint()) is None
        ):
            self.clearSelection()
            self.selectionModel().clearCurrentIndex()
        super().mousePressEvent(event)

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


class MainWindow(QMainWindow):
    """Choose a mods folder, update the hash data, then fix/revert per mod or file."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
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

        self.setWindowTitle("ZZZ Mod Housekeeper")
        self._settings = QSettings(
            str(project_root() / "settings.ini"), QSettings.Format.IniFormat
        )
        show_empty_setting = self._settings.value("show_empty_folders", True)
        self._show_empty_folders = show_empty_setting in (True, "true")
        self._build_ui()
        self.installEventFilter(self)
        self.setMouseTracking(True)
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)
            widget.setMouseTracking(True)
        self.resize(900, 600)
        self.setMinimumSize(640, 420)
        self._refresh_actions()
        for variant in REPO_VARIANTS:
            self.append_log(
                f"Upstream: {REPO_VARIANTS[variant].repo_url}\n"
                f"Data repo: {default_cache_dir(variant)}"
            )
        geometry = self._settings.value("window_geometry")
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
        expanded_setting = self._settings.value("tree_expanded", [])
        self._saved_expanded_paths = (
            {str(path) for path in expanded_setting}
            if isinstance(expanded_setting, list)
            else set()
        )
        if self._restore_last_folder():
            self._on_load_data()
        else:
            self._check_hash_updates()

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
        mods_row.addWidget(self._mods_edit, 1)
        mods_row.addWidget(self._browse_btn)
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
        layout.addWidget(self._tree, 3)
        strip = QHBoxLayout()
        self._update_btn = QPushButton("Update hashes", central)
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
        self.append_log(f"Mods folder: {chosen}")
        self._settings.setValue("mods_folder", chosen)
        self._settings.sync()
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
        show_empty = menu.addAction("Show empty folders")
        show_empty.setCheckable(True)
        show_empty.setChecked(self._show_empty_folders)
        show_empty.toggled.connect(self._on_show_empty_toggled)
        folder = menu.addAction("Open mods folder")
        folder.triggered.connect(self._open_mods_folder)
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
        mods_dir = Path(self._mods_edit.text().strip())
        save_preset(name, enabled_relative_paths(self._root_node, mods_dir))
        self._rebuild_presets_menu()
        self.append_log(f"Preset saved: {name.strip()}")

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
        return dialog.exec() == QDialog.DialogCode.Accepted

    def handle_archive_drop(self, archive: Path, node: ModNode | None) -> None:
        """Confirm and install a dropped archive into a category or the root."""
        self.raise_()
        self.activateWindow()
        if self._worker is not None or not self._data:
            self.append_log("Load and analyze a mods folder before installing")
            return
        root = Path(self._mods_edit.text().strip())
        if not root.is_dir():
            self.append_log("Load the mods folder before installing")
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
        QTimer.singleShot(0, self._start_analyze)

    def _on_show_empty_toggled(self, checked: bool) -> None:
        """Persist the show-empty-folders preference and rescan the mods tree."""
        self._show_empty_folders = checked
        self._settings.setValue("show_empty_folders", checked)
        self._start_analyze()

    def _open_mods_folder(self) -> None:
        """Open the current mods folder in the system file manager."""
        mods_dir = self._mods_edit.text().strip()
        if mods_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(mods_dir))

    def _show_about(self) -> None:
        """About box for the app."""
        QMessageBox.about(
            self,
            "About ZZZ Mod Housekeeper",
            "ZZZ Mod Housekeeper<br><br>"
            "Fixes outdated asset hashes in mods for ZZZ.<br>"
            "Also can install mod archives, manage preset loadouts, and inspect mod info.<br><br>"
            'By Kilvoctu: <a href="https://github.com/Kilvoctu/ZZZ_Mod_Housekeeper">Source</a>',
        )

    def _on_load_data(self) -> None:
        """Parse the already-cloned local data repos (no network access)."""
        if self._worker is not None:
            return
        if not any(
            changelog_path(default_cache_dir(variant)).exists()
            for variant in REPO_VARIANTS
        ):
            self.append_log(
                f"No local hash data yet — click '{_UPDATE_BUTTON}' to download it first."
            )
            QTimer.singleShot(0, self._check_hash_updates)
            return
        self.append_log("Loading hash data from local clones...")
        self._start_worker(load_all_data_worker(), self._on_data_loaded)

    def _on_update_data(self) -> None:
        """Update the data repos and parse them in one background worker."""
        if self._worker is not None:
            return
        self._loaded_via_update = True
        self.append_log("Updating ZZZ-Model-Hash data (2048p + 1024p)...")
        self._start_worker(update_data_worker(), self._on_data_loaded)

    def _check_hash_updates(self) -> None:
        """Check upstream repos for newer commits without blocking the UI.

        Runs on its own worker slot (not the busy guard) so a slow or offline
        network never stalls the app; unknown results re-check on a 60 s retry.
        """
        if self._check_worker is not None:
            return
        if self._update_statuses is None:
            self.append_log("Checking upstream hash data for updates...")
        else:
            self.append_log("Retrying upstream hash data check...")
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
            else f"{_display_name(node)} — {self._count_files(node)} file(s)"
        )

    def _on_analyze_done(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            return
        node, summary = result
        self._root_node = node
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
        self._rebuild_presets_menu()

    def _on_fix_mod(self) -> None:
        """Scan the selected scope and apply its fixes in one background job."""
        if self._worker is not None or not self._data:
            return
        node = self._selected_node
        if node is None:
            self.append_log("Select a mod or .ini file to fix")
            return
        if (
            node.variant is not None
            and self._data is not None
            and node.variant not in self._data
        ):
            self.append_log(
                f"{node.variant} hash data is not loaded — click 'Update hashes' first."
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
        QTimer.singleShot(0, self._deferred_scope_refresh)

    def _on_revert_mod(self) -> None:
        """Enumerate the selected scope's fix-backup history in the background."""
        if self._worker is not None or not self._data:
            return
        node = self._selected_node
        if node is None:
            self.append_log("Select a mod or .ini file to revert")
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
        dialog = _RevertDialog(chain_list, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choices = dialog.selected_choices()
        if not choices:
            self.append_log("No states selected for restore")
            return
        self.append_log(f"Restoring {len(choices)} file(s)…")
        self._start_worker(revert_worker(choices), self._on_revert_done)

    def _on_revert_done(self, count: object) -> None:
        if not isinstance(count, int):
            return
        self.append_log(f"Restored {count} file(s) in '{self._fixing_name}'")
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
        self._pending_analyzing_log = "Analyzing mods…"
        self._start_worker(
            analyze_worker(
                mods_dir, self._data, self._structure, show_empty=self._show_empty_folders
            ),
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
                self._settings.sync()
                self._mods_edit.setText(str(fallback))
                self.append_log(f"Mods folder (relocated): {fallback}")
                return True
            self.append_log(f"Saved mods folder no longer exists: {saved}")
            return False
        self._mods_edit.setText(saved)
        self.append_log(f"Mods folder (restored): {saved}")
        return True

    def _fill_mod_tree(self, root_node: ModNode) -> None:
        """Populate the mods overview; the mods root's children are the top level."""
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
                    self._add_mod_item(self._tree.invisibleRootItem(), child)
            finally:
                self._filling_tree = False
            self._restore_expand_state(expanded)
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
        if signal == UPDATES_SIGNAL:
            item.setForeground(2, COLOR_UPDATED)
        elif signal == STRUCTURAL_SIGNAL:
            item.setForeground(2, COLOR_STRUCTURAL)
        elif signal:
            item.setForeground(2, COLOR_CURRENT)
        else:
            item.setData(2, Qt.ItemDataRole.ForegroundRole, None)

    @staticmethod
    def _apply_mod_tooltip(node: ModNode, item: QTreeWidgetItem) -> None:
        """Mod-column tooltip for a mod: its path plus any breaking-change note."""
        tooltip = str(node.path)
        version = node.version
        if version is not None and version.breaks_label:
            tooltip += f"\nBreaking change: {version.breaks_label}"
        item.setToolTip(0, tooltip)

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
        if column != 1 or self._filling_tree:
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(node, ModNode) or node.kind != "mod":
            return
        self._tree.setCurrentItem(item)
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
            self._update_mod_label(node)
            self.append_log(
                f"{'Enabled' if desired_enabled else 'Disabled'} '{_display_name(node)}'"
            )
            retarget_subtree_paths(node, new_path)
            self._fixing_node = node
            QTimer.singleShot(0, self._deferred_scope_refresh)

    def _show_root_context_menu(self, position: QPoint) -> None:
        """Right-click menu on blank space: open the mods folder or create one."""
        menu = QMenu(self)
        open_folder = menu.addAction("Open mods folder")
        create_folder = menu.addAction("Create new folder…")
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
        """Right-click menu on a mod row: folder, backup folder, info or previews."""
        item = self._tree.itemAt(position)
        if item is None:
            self._show_root_context_menu(position)
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, ModNode) or data.kind != "mod":
            return
        menu = QMenu(self)
        open_mod = menu.addAction("Open mod folder")
        open_backup = menu.addAction("Open backup folder")
        info = menu.addAction("Mod info…")
        preview = menu.addAction("Preview images…")
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
        elif chosen is info:
            self._show_mod_info(data)
        elif chosen is preview:
            self._show_preview_gallery(data)

    def _show_mod_info(self, node: ModNode) -> None:
        """Open the read-only mod info summary dialog for one mod."""
        self._mod_info_dialog = _ModInfoDialog(node.path, self)
        self._mod_info_dialog.exec()

    def _show_preview_gallery(self, node: ModNode) -> None:
        """Open the preview image gallery for one mod."""
        self._preview_gallery_dialog = _PreviewGallery(node.path, _display_name(node), self)
        self._preview_gallery_dialog.exec()

    def append_log(self, message: str) -> None:
        if message == self._log_last_message:
            return
        self._log_last_message = message
        self._log_view.appendPlainText(message)

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
        self._settings.setValue("window_geometry", self.saveGeometry())
        for column, _ in _TREE_COLUMN_WIDTHS:
            self._settings.setValue(f"col_width_{column}", self._tree.header().sectionSize(column))
        self._settings.setValue("tree_expanded", sorted(self._save_expand_state()))
        self._settings.setValue("window_maximized", bool(self.isMaximized()))
        self._settings.sync()
        self._check_retry_timer.stop()
        for worker in (self._worker, self._check_worker):
            if worker is not None and worker.isRunning():
                worker.wait()
        super().closeEvent(event)
