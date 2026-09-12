"""Tests for tools.ui.worker: import_archive_worker drives extraction on a thread."""

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest
# noinspection PyPackageRequirements
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from tools import importer
from tools.ui.worker import import_archive_worker


def build_archive(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return path


def build_7z_archive(path: Path, members: dict[str, str]) -> Path:
    """Create a real .7z archive, skipping when 7-Zip is not installed."""
    exe = importer.find_7z()
    if exe is None:
        pytest.skip("7-Zip not installed")
    staging = Path(tempfile.mkdtemp(prefix="zzz-fixture-"))
    try:
        for name, payload in members.items():
            target = staging / Path(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        result = subprocess.run(
            [exe, "a", "-y", "-r", str(path), "*"],
            cwd=staging,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return path


def run_worker(worker):
    """Run a TaskWorker to completion; return {"result": int, "error": str | None}."""
    if QCoreApplication.instance() is None:
        QCoreApplication([])
    holder: dict[str, object] = {"result": None, "error": None}
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(15000)

    def on_done(result: object) -> None:
        holder["result"] = result
        loop.quit()

    def on_error(message: str) -> None:
        holder["error"] = message
        loop.quit()

    worker.done.connect(on_done)
    worker.error.connect(on_error)
    worker.start()
    loop.exec()
    worker.wait(5000)
    return holder


def test_import_archive_worker_constructs_and_extracts(tmp_path):
    destination = tmp_path / "mods"
    archive = build_archive(
        tmp_path / "MyMod.zip",
        {
            "MyMod/ini/a.ini": "a",
            "MyMod/b.buf": "b",
            "MyMod/": "",
            "__MACOSX/_foo": "junk",
        },
    )

    worker = import_archive_worker(archive, destination)

    holder = run_worker(worker)

    assert holder["error"] is None
    assert holder["result"] == 2
    assert (destination / "MyMod" / "ini" / "a.ini").read_text() == "a"
    for path in destination.rglob("*"):
        assert "__MACOSX" not in path.parts


def test_import_archive_worker_replace_flag(tmp_path):
    destination = tmp_path / "mods"
    archive = build_archive(tmp_path / "MyMod.zip", {"MyMod/a.ini": "v1"})

    holder = run_worker(import_archive_worker(archive, destination, replace=False))

    assert holder["result"] == 1
    assert (destination / "MyMod" / "a.ini").read_text() == "v1"

    holder = run_worker(import_archive_worker(archive, destination, replace=False))

    assert holder["result"] == 0
    assert (destination / "MyMod" / "a.ini").read_text() == "v1"

    holder = run_worker(import_archive_worker(archive, destination, replace=True))

    assert holder["result"] == 1
    assert (destination / "MyMod" / "a.ini").read_text() == "v1"


def test_import_archive_worker_7z(tmp_path):
    destination = tmp_path / "mods"
    archive = build_7z_archive(
        tmp_path / "MyMod.7z", {"MyMod/a.ini": "a", "MyMod/b.buf": "b"}
    )

    holder = run_worker(import_archive_worker(archive, destination))

    assert holder["error"] is None
    assert holder["result"] == 2
    assert (destination / "MyMod" / "a.ini").read_text() == "a"
    assert (destination / "MyMod" / "b.buf").read_text() == "b"
