"""Tests for tools.ui.worker: data and archive workers running on a thread."""

import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest
# noinspection PyPackageRequirements
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from tools import importer, presets
from tools.backups import store_folder_for_mod
from tools.blend_remap import blend_state_path
from tools.fixer import FixerData
from tools.repo import DEFAULT_VARIANT, RepoError
from tools.ui.worker import (
    delete_folder_worker,
    import_archive_worker,
    load_all_data_worker,
    update_data_worker,
)


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


def patch_empty_datasets(monkeypatch, tmp_path):
    """Point every dataset source at empty temp paths: no clones, no static
    datasets, no user patches."""
    monkeypatch.setattr(
        "tools.ui.worker.default_cache_dir",
        lambda variant=DEFAULT_VARIANT: tmp_path / f"cache-{variant}",
    )
    missing = tmp_path / "no-such-static-dataset.json"
    monkeypatch.setattr("tools.fixer.legacy_chains_path", lambda: missing)
    monkeypatch.setattr("tools.fixer.player_character_data_path", lambda: missing)
    monkeypatch.setattr("tools.patches.user_patches_dir", lambda: tmp_path)


def test_load_all_data_worker_falls_back_without_local_data(tmp_path, monkeypatch):
    """No locally cloned variant repo: the job returns a fallback dataset
    under the default variant instead of raising; heads stay empty."""
    patch_empty_datasets(monkeypatch, tmp_path)

    worker = load_all_data_worker()
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    result = holder["result"]
    assert isinstance(result, tuple) and len(result) == 4
    caches, datasets, heads, structure = result
    assert set(caches) == {"2048p", "1024p"}
    assert set(datasets) == {DEFAULT_VARIANT}
    fallback = datasets[DEFAULT_VARIANT]
    assert isinstance(fallback, FixerData)
    assert fallback.chains == {}
    assert fallback.entries == []
    assert set(heads) == {"2048p", "1024p"}
    assert all(head == "" for head in heads.values())
    assert structure is not None
    assert any("No hash data at all" in message for message in logs)


def test_update_data_worker_offline_returns_fallback(tmp_path, monkeypatch):
    """ensure_repo failing for every variant logs 'Could not update' plus the
    fallback hint and still returns the fallback dataset; nothing raises."""

    def offline(*_args, **_kwargs):
        raise RepoError("offline")

    monkeypatch.setattr("tools.ui.worker.ensure_repo", offline)
    patch_empty_datasets(monkeypatch, tmp_path)

    worker = update_data_worker()
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    result = holder["result"]
    assert isinstance(result, tuple) and len(result) == 4
    repo_dirs, datasets, heads, structure = result
    assert repo_dirs == {}
    assert set(datasets) == {DEFAULT_VARIANT}
    assert isinstance(datasets[DEFAULT_VARIANT], FixerData)
    assert heads == {DEFAULT_VARIANT: ""}
    assert structure is not None
    assert any(
        "Could not update" in message and "offline" in message
        for message in logs
    )
    assert any("No hash data at all" in message for message in logs)


def delete_setup(
    monkeypatch,
    tmp_path,
    preset_paths: dict[str, list[str]],
    markers: dict[str, dict] | None = None,
):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    presets_dir = tmp_path / "presets"
    mods.mkdir()
    presets_dir.mkdir()
    monkeypatch.setattr("tools.ui.worker.default_backups_dir", lambda: store)
    monkeypatch.setattr("tools.presets.project_root", lambda: presets_dir)
    for name, entries in preset_paths.items():
        presets.save_preset(name, entries, presets_dir)
    if markers is not None:
        state_path = blend_state_path(store, mods)
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps(markers), encoding="utf-8")
    return mods, store


def test_delete_folder_worker_mod_deletes_folder_and_followups(tmp_path, monkeypatch):
    mods, store = delete_setup(
        monkeypatch,
        tmp_path,
        {"p1": ["Cat/OldMod", "Other"], "p2": ["Cat/OldMod"]},
        {
            "Cat/OldMod/a.buf": {"hash": "aabbccdd", "stamp": 1},
            "Other/keep.buf": {"hash": "11223344", "stamp": 2},
        },
    )
    mod = mods / "Cat" / "OldMod"
    mod.mkdir(parents=True)
    (mod / "a.ini").write_text("ini", encoding="utf-8")
    mirror = store_folder_for_mod(store, mods, mod)
    mirror.mkdir(parents=True)
    (mirror / "a.ini -- 2026-01-01 00.00.00.bak").write_text("bak", encoding="utf-8")

    worker = delete_folder_worker(mods, mod, "mod")
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    result = holder["result"]
    assert isinstance(result, tuple) and len(result) == 2
    deleted, file_count = result
    assert deleted == mod
    assert file_count == 1
    assert not mod.exists()
    # p2 loses its last entry, so the prune-on-empty rule removes it entirely.
    assert presets.load_presets() == {"p1": ["Other"]}
    assert not mirror.exists()
    assert json.loads(blend_state_path(store, mods).read_text(encoding="utf-8")) == {
        "Other/keep.buf": {"hash": "11223344", "stamp": 2}
    }
    assert any("Deleted" in message for message in logs)
    assert any("Presets updated" in message for message in logs)
    assert any("Backup history purged" in message for message in logs)
    assert any("Blend-remap markers removed" in message for message in logs)


def test_delete_folder_worker_category_deletes_subtree_and_followups(
    tmp_path, monkeypatch
):
    mods, store = delete_setup(
        monkeypatch,
        tmp_path,
        {"p1": ["Cat/M1", "Cat/M2", "Other"]},
        {
            "Cat/M1/x.buf": {"hash": "aabbccdd", "stamp": 1},
            "Other/keep.buf": {"hash": "11223344", "stamp": 2},
        },
    )
    cat = mods / "Cat"
    (cat / "M1").mkdir(parents=True)
    (cat / "M1" / "a.ini").write_text("a", encoding="utf-8")
    (cat / "M2").mkdir(parents=True)
    (cat / "M2" / "b.ini").write_text("b", encoding="utf-8")
    mirror = store_folder_for_mod(store, mods, cat) / "M1"
    mirror.mkdir(parents=True)
    (mirror / "a.ini -- 2026-01-01 00.00.00.bak").write_text("bak", encoding="utf-8")

    worker = delete_folder_worker(mods, cat, "category")
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    result = holder["result"]
    assert isinstance(result, tuple) and len(result) == 2
    deleted, file_count = result
    assert deleted == cat
    assert file_count == 2
    assert not cat.exists()
    assert presets.load_presets() == {"p1": ["Other"]}
    assert not store_folder_for_mod(store, mods, cat).exists()
    assert json.loads(blend_state_path(store, mods).read_text(encoding="utf-8")) == {
        "Other/keep.buf": {"hash": "11223344", "stamp": 2}
    }
    assert any("Deleted" in message for message in logs)
    assert any("Presets updated" in message for message in logs)
    assert any("Backup history purged" in message for message in logs)
    assert any("Blend-remap markers removed" in message for message in logs)


def test_delete_folder_worker_prunes_emptied_preset(tmp_path, monkeypatch):
    mods, store = delete_setup(monkeypatch, tmp_path, {"solo": ["Cat/OldMod"]})
    mod = mods / "Cat" / "OldMod"
    mod.mkdir(parents=True)
    (mod / "a.ini").write_text("ini", encoding="utf-8")
    mirror = store_folder_for_mod(store, mods, mod)
    mirror.mkdir(parents=True)
    (mirror / "a.ini -- 2026-01-01 00.00.00.bak").write_text("bak", encoding="utf-8")

    holder = run_worker(delete_folder_worker(mods, mod, "mod"))

    assert holder["error"] is None
    assert presets.load_presets() == {}
    assert "solo" not in presets.presets_path().read_text(encoding="utf-8")
