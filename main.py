"""ZZZ Mod Housekeeper entry point."""
import sys
from pathlib import Path

# noinspection PyPackageRequirements
from PySide6.QtCore import QLockFile, QStandardPaths

# noinspection PyPackageRequirements
from PySide6.QtGui import QIcon

# noinspection PyPackageRequirements
from PySide6.QtWidgets import QApplication, QMessageBox, QProxyStyle, QStyle

from tools.repo import project_root
from tools.ui.main_window import MainWindow

_LOCK_NAME = "zzzmodkeeper.lock"
_TOOLTIP_DELAY_MS = 450


class _TooltipDelayStyle(QProxyStyle):
    """App-wide style that shows tooltips only after a fixed hover delay."""

    def styleHint(self, hint, *args, **kwargs):
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return _TOOLTIP_DELAY_MS
        return super().styleHint(hint, *args, **kwargs)


def _acquire_instance_lock() -> QLockFile | None:
    """Single-instance lock beside the app; per-user app-data as a fallback."""
    lock_dir = project_root()
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        if not base:
            base = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.TempLocation
            )
        lock_dir = Path(base)
        lock_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_dir / _LOCK_NAME))
    return lock if lock.tryLock(0) else None


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("ZZZ Mod Housekeeper")
    app.setStyle(_TooltipDelayStyle(app.style()))
    lock = _acquire_instance_lock()
    if lock is None:
        QMessageBox.warning(
            None, "ZZZ Mod Housekeeper", "ZZZ Mod Housekeeper is already running."
        )
        sys.exit(0)
    icon = project_root() / "icon.ico"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
