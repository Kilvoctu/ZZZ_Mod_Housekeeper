"""Tests for tools.ui.worker: data and archive workers running on a thread."""

import json
import shutil
import subprocess
import tempfile
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

# noinspection PyPackageRequirements
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from tools import importer, presets, promoted, state
from tools.backups import store_folder_for_mod
from tools.blend_remap import blend_state_path
from tools.dumpdata import DumpData, DumpLayout
from tools.fixer import FixerData, empty_fixer_data
from tools.mods import analyze_mods
from tools.repo import DEFAULT_VARIANT, RepoError
from tools.texcoord_upgrade import texcoord_state_path
from tools.ui.worker import (
    delete_folder_worker,
    fix_mod_worker,
    import_archive_worker,
    load_all_data_worker,
    rename_folder_worker,
    revert_worker,
    scope_analyze_worker,
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
            check=False,
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
    assert set(caches) == {"2048p", "1024p", "fix_tool"}
    assert set(datasets) == {DEFAULT_VARIANT}
    fallback = datasets[DEFAULT_VARIANT]
    assert isinstance(fallback, FixerData)
    assert fallback.chains == {}
    assert fallback.entries == []
    assert set(heads) == {"2048p", "1024p", "fix_tool"}
    assert all(head == "" for head in heads.values())
    assert structure is not None
    assert any("No upstream data loaded" in message for message in logs)


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
    assert any("No upstream data loaded" in message for message in logs)


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
    monkeypatch.setattr("tools.state.project_root", lambda: presets_dir)
    for name, entries in preset_paths.items():
        presets.save_preset(name, entries, presets_dir)
    if markers is not None:
        state_path = blend_state_path(store, mods)
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps(markers), encoding="utf-8")
    return mods, store, presets_dir


def test_delete_folder_worker_mod_deletes_folder_and_followups(tmp_path, monkeypatch):
    mods, store, presets_dir = delete_setup(
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
    assert presets.load_presets(presets_dir) == {"p1": ["Other"]}
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
    mods, store, presets_dir = delete_setup(
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
    assert presets.load_presets(presets_dir) == {"p1": ["Other"]}
    assert not store_folder_for_mod(store, mods, cat).exists()
    assert json.loads(blend_state_path(store, mods).read_text(encoding="utf-8")) == {
        "Other/keep.buf": {"hash": "11223344", "stamp": 2}
    }
    assert any("Deleted" in message for message in logs)
    assert any("Presets updated" in message for message in logs)
    assert any("Backup history purged" in message for message in logs)
    assert any("Blend-remap markers removed" in message for message in logs)


def test_delete_folder_worker_prunes_emptied_preset(tmp_path, monkeypatch):
    mods, store, presets_dir = delete_setup(
        monkeypatch, tmp_path, {"solo": ["Cat/OldMod"]}
    )
    mod = mods / "Cat" / "OldMod"
    mod.mkdir(parents=True)
    (mod / "a.ini").write_text("ini", encoding="utf-8")
    mirror = store_folder_for_mod(store, mods, mod)
    mirror.mkdir(parents=True)
    (mirror / "a.ini -- 2026-01-01 00.00.00.bak").write_text("bak", encoding="utf-8")

    holder = run_worker(delete_folder_worker(mods, mod, "mod"))

    assert holder["error"] is None
    assert presets.load_presets(presets_dir) == {}
    assert "solo" not in state.state_path(presets_dir).read_text(encoding="utf-8")


def test_rename_folder_worker_retargets_promoted_key(tmp_path, monkeypatch):
    mods, _store, presets_dir = delete_setup(monkeypatch, tmp_path, {"p1": ["Cat/Old"]})
    mod = mods / "Cat" / "Old"
    mod.mkdir(parents=True)
    (mod / "preview.jpg").write_text("img", encoding="utf-8")
    promoted.save_promoted({"Cat/Old": "preview.jpg"}, presets_dir)

    worker = rename_folder_worker(mods, mod, "New", "mod")
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    assert holder["result"] == mods / "Cat" / "New"
    assert promoted.load_promoted(presets_dir) == {"Cat/New": "preview.jpg"}
    assert any("Preview images updated" in message for message in logs)


def test_delete_folder_worker_removes_promoted_key(tmp_path, monkeypatch):
    mods, _store, presets_dir = delete_setup(
        monkeypatch, tmp_path, {"p1": ["Cat/OldMod"]}
    )
    mod = mods / "Cat" / "OldMod"
    mod.mkdir(parents=True)
    (mod / "preview.jpg").write_text("img", encoding="utf-8")
    promoted.save_promoted(
        {"Cat/OldMod": "preview.jpg", "Stays": "keep.png"}, presets_dir
    )

    worker = delete_folder_worker(mods, mod, "mod")
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    assert promoted.load_promoted(presets_dir) == {"Stays": "keep.png"}
    assert any("Preview images removed" in message for message in logs)


def texcoord_revert_setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """mods/Mod/face.buf, a store backup beside it holding the same bytes, and a convert marker."""
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "Mod" / "face.buf"
    live.parent.mkdir(parents=True)
    payload = b"pre-upgrade face texcoord bytes"
    live.write_bytes(payload)
    state_path = texcoord_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {"Mod/face.buf": {"action": "convert", "before": sha256(payload).hexdigest()}}
        ),
        encoding="utf-8",
    )
    backup = (
        store_folder_for_mod(store, mods, live.parent)
        / "face.buf -- 2026-01-01 00.00.00.bak"
    )
    backup.parent.mkdir(parents=True)
    backup.write_bytes(payload)
    return mods, store, live, backup


def test_revert_worker_prunes_restored_texcoord_markers(tmp_path):
    mods, store, live, backup = texcoord_revert_setup(tmp_path)

    worker = revert_worker([(live, backup)], mods_dir=mods, store_dir=store)
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    assert holder["result"] == 1
    assert json.loads(texcoord_state_path(store, mods).read_text(encoding="utf-8")) == {}
    assert any("Texcoord-upgrade markers cleared: 1" in line for line in logs)


def test_revert_worker_without_dirs_keeps_texcoord_markers(tmp_path):
    mods, store, live, backup = texcoord_revert_setup(tmp_path)

    holder = run_worker(revert_worker([(live, backup)]))

    assert holder["error"] is None
    assert holder["result"] == 1
    assert "Mod/face.buf" in json.loads(
        texcoord_state_path(store, mods).read_text(encoding="utf-8")
    )


def blend_revert_setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """mods/Mod/blend.buf, a store backup beside it holding the same bytes, and a vote marker."""
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "Mod" / "blend.buf"
    live.parent.mkdir(parents=True)
    payload = b"pre-remap blend bytes"
    live.write_bytes(payload)
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {"Mod/blend.buf": {"action": "vote", "before": sha256(payload).hexdigest()}}
        ),
        encoding="utf-8",
    )
    backup = (
        store_folder_for_mod(store, mods, live.parent)
        / "blend.buf -- 2026-01-01 00.00.00.bak"
    )
    backup.parent.mkdir(parents=True)
    backup.write_bytes(payload)
    return mods, store, live, backup


def test_revert_worker_prunes_restored_blend_markers(tmp_path, monkeypatch):
    mods, store, live, backup = blend_revert_setup(tmp_path)
    monkeypatch.setattr("tools.ui.worker.default_backups_dir", lambda: store)

    worker = revert_worker([(live, backup)], mods_dir=mods, store_dir=store)
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    assert holder["result"] == 1
    assert json.loads(blend_state_path(store, mods).read_text(encoding="utf-8")) == {}
    assert any("Blend-remap markers cleared: 1" in line for line in logs)


def test_revert_worker_prunes_empty_backup_folders(tmp_path, monkeypatch):
    mods, store, live, backup = blend_revert_setup(tmp_path)
    monkeypatch.setattr("tools.ui.worker.default_backups_dir", lambda: store)

    worker = revert_worker([(live, backup)], mods_dir=mods, store_dir=store)
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    assert holder["result"] == 1
    assert not store_folder_for_mod(store, mods, mods / "Mod").exists()
    assert any("Empty backup folders cleared: 1 folder(s)" in line for line in logs)
    # _blend_remaps.json keeps the store root non-empty, so the root survives.
    assert blend_state_path(store, mods).is_file()
    assert blend_state_path(store, mods).parent.is_dir()


def test_fix_mod_worker_applies_blend_vote_remap(tmp_path, monkeypatch):
    mods = tmp_path / "mods"
    mod = mods / "Mod"
    mod.mkdir(parents=True)
    (mod / "m.ini").write_bytes(
        b"[TextureOverrideModBlend]\r\n"
        b"hash = ff36809c\r\n"
        b"handling = skip\r\n"
        b"vb0 = ResourceModPos\r\n"
        b"vb2 = ResourceModBlend\r\n"
        b"draw = 2, 0\r\n"
        b"\r\n"
        b"[ResourceModPos]\r\n"
        b"type = Buffer\r\n"
        b"stride = 40\r\n"
        b"filename = position.buf\r\n"
        b"\r\n"
        b"[ResourceModBlend]\r\n"
        b"type = Buffer\r\n"
        b"stride = 32\r\n"
        b"filename = blend.buf\r\n"
    )
    mod_position = b"".join(v.to_bytes(4, "little") * 10 for v in (0, 1))
    mod_blend = b"".join(
        b"\x00" * 16
        + b"".join((10 * v + slot).to_bytes(4, "little") for slot in range(4))
        for v in (0, 1)
    )
    (mod / "position.buf").write_bytes(mod_position)
    (mod / "blend.buf").write_bytes(mod_blend)
    dump = tmp_path / "dump"
    dump.mkdir()
    dump_blend = b"".join(
        b"\x00" * 16
        + b"".join((10 * v + 5 + slot).to_bytes(4, "little") for slot in range(4))
        for v in (0, 1)
    )
    (dump / "d-pos.buf").write_bytes(mod_position)
    (dump / "d-blend.buf").write_bytes(dump_blend)
    dumps = DumpData(
        layouts={
            ("body", "mesh", "position"): DumpLayout(40, "vb0", "d-pos.buf"),
            ("body", "mesh", "blend"): DumpLayout(32, "vb2", "d-blend.buf"),
        },
        current_hashes={("body", "mesh", "position"): "ff36809c"},
        binaries={
            ("body", "mesh", "position"): dump / "d-pos.buf",
            ("body", "mesh", "blend"): dump / "d-blend.buf",
        },
    )
    data = empty_fixer_data()
    data.dumps = dumps
    store = tmp_path / "store"
    monkeypatch.setattr("tools.ui.worker.default_backups_dir", lambda: store)

    worker = fix_mod_worker(mods, mod, {DEFAULT_VARIANT: data})
    logs: list[str] = []
    worker.log.connect(logs.append)
    holder = run_worker(worker)

    assert holder["error"] is None
    result = holder["result"]
    assert isinstance(result, tuple) and len(result) == 3
    variant, plans, written = result
    assert variant == DEFAULT_VARIANT
    assert plans == []
    assert written == 0
    assert (mod / "blend.buf").read_bytes() == dump_blend
    markers = json.loads(
        blend_state_path(store, mods).read_text(encoding="utf-8")
    )
    assert markers["Mod/blend.buf"]["action"] == "vote"
    assert any("remapped blend indices (vote)" in line for line in logs)


def test_scope_analyze_worker_returns_node(tmp_path):
    mod = tmp_path / "mod"
    mod.mkdir()
    (mod / "char.ini").write_text(
        "[TextureOverrideBody]\nhash = aaaa0000\n", encoding="utf-8"
    )
    root, _summary = analyze_mods(tmp_path, empty_fixer_data())
    child = next(node for node in root.children if node.kind == "mod")
    holder = run_worker(scope_analyze_worker(child, empty_fixer_data(), None))
    assert holder["error"] is None
    assert holder["result"] is child
