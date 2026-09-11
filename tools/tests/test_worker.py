"""Tests for tools.ui.worker: the worker -> GUI payload contract.

``MainWindow._on_data_loaded`` unpacks the ``TaskWorker.done`` payload as
``repo_dirs, datasets, heads, structure``; these tests pin that shape.
"""

from collections.abc import Callable

import hashlib
import json
import struct

from tools import repo
from tools.blend_remap import BlendTables, blend_state_path
from tools.changelog import build_chain_index, parse_changelog_file
from tools.fixer import FixerData
from tools.mods import CURRENT_SIGNAL, STRUCTURAL_SIGNAL, UPDATES_SIGNAL, ModVersion
from tools.structure import StructureData
from tools.tests._test_data import make_repo
from tools.ui.main_window import (
    hashes_tooltip,
    update_button_state,
    updates_tooltip,
    wrap_tooltip,
)
from tools.ui.worker import (
    fix_mod_worker,
    friendly_error,
    load_all_data_worker,
    update_data_worker,
)


def test_load_all_data_worker_delivers_variant_caches_and_datasets(tmp_path, monkeypatch):
    make_repo(tmp_path)
    missing = tmp_path / "no-such-static-dataset.json"
    monkeypatch.setattr("tools.fixer.legacy_chains_path", lambda: missing)
    monkeypatch.setattr("tools.fixer.player_character_data_path", lambda: missing)
    monkeypatch.setattr(
        "tools.ui.worker.default_cache_dir",
        lambda variant=repo.DEFAULT_VARIANT: tmp_path,
    )
    received = []
    worker = load_all_data_worker()
    worker.done.connect(received.append)
    worker.run()

    assert len(received) == 1
    caches, datasets, heads, structure = received[0]
    assert set(caches) == set(datasets) == {"2048p", "1024p"}
    assert set(caches.values()) == {tmp_path}
    assert heads == {"2048p": "", "1024p": ""}
    assert isinstance(structure, StructureData)
    assert structure.rules_by_hash == {}
    assert len(structure.chain_aliases) > 0
    expected_chains = len(
        build_chain_index(parse_changelog_file(repo.changelog_path(tmp_path)))
    )
    for data in datasets.values():
        assert isinstance(data, FixerData)
        assert len(data.chains) == expected_chains


def test_update_button_state_truth_table():
    assert update_button_state({}, True) == (False, "Checking...")
    assert update_button_state({"2048p": None}, True) == (False, "Checking...")
    assert update_button_state({"2048p": True, "1024p": False}, False) == (
        True,
        "Update hashes",
    )
    assert update_button_state({"2048p": True, "1024p": None}, False) == (
        True,
        "Update hashes",
    )
    assert update_button_state({"2048p": False, "1024p": False}, False) == (
        False,
        "Hashes updated",
    )
    assert update_button_state({"2048p": None}, False) == (False, "Update unavailable")
    assert update_button_state({}, False) == (False, "Update unavailable")


def test_fix_mod_worker_defaults_structure_when_none(tmp_path, monkeypatch):
    make_repo(tmp_path)
    missing = tmp_path / "no-such-static-dataset.json"
    monkeypatch.setattr("tools.fixer.legacy_chains_path", lambda: missing)
    monkeypatch.setattr("tools.fixer.player_character_data_path", lambda: missing)
    monkeypatch.setattr(
        "tools.ui.worker.default_cache_dir",
        lambda _variant=repo.DEFAULT_VARIANT: tmp_path,
    )
    loaded = []
    loader = load_all_data_worker()
    loader.done.connect(loaded.append)
    loader.run()
    assert len(loaded) == 1
    _, datasets, _, _ = loaded[0]

    mod = tmp_path / "mods"
    mod.mkdir()
    received = []
    errors = []
    worker = fix_mod_worker(mod, mod, datasets)
    worker.done.connect(received.append)
    worker.error.connect(errors.append)
    worker.run()

    assert errors == [], f"worker errored: {errors}"
    assert len(received) == 1
    variant, plans, written = received[0]
    assert variant == repo.DEFAULT_VARIANT
    assert isinstance(plans, list)
    assert plans == []  # empty mods dir -> nothing to fix
    assert written == 0


def test_fix_mod_worker_folder_scope_runs_blend_pass(tmp_path, monkeypatch):
    make_repo(tmp_path)
    missing = tmp_path / "no-such-static-dataset.json"
    monkeypatch.setattr("tools.fixer.legacy_chains_path", lambda: missing)
    monkeypatch.setattr("tools.fixer.player_character_data_path", lambda: missing)
    monkeypatch.setattr(
        "tools.ui.worker.default_cache_dir",
        lambda _variant=repo.DEFAULT_VARIANT: tmp_path,
    )
    loaded = []
    loader = load_all_data_worker()
    loader.done.connect(loaded.append)
    loader.run()
    assert len(loaded) == 1
    _, datasets, _, _ = loaded[0]

    store = tmp_path / "store"
    monkeypatch.setattr("tools.ui.worker.default_backups_dir", lambda: store)
    monkeypatch.setattr(
        "tools.ui.worker.load_blend_remaps",
        lambda: BlendTables(mappings={"aabbccdd": {5: 9}}, position_to_blend={}),
    )

    mod = tmp_path / "mods"
    mod.mkdir()
    buf = mod / "x.blend.buf"
    original = struct.pack("<4f4I", 1.0, 0.0, 0.0, 0.0, 5, 1, 2, 3)
    buf.write_bytes(original)
    (mod / "m.ini").write_text(
        "[TextureOverrideXBlend]\n"
        "hash = aabbccdd\n"
        "handling = skip\n"
        "vb2 = ResourceXBlend\n"
        "\n"
        "[ResourceXBlend]\n"
        "type = Buffer\n"
        "stride = 32\n"
        "filename = x.blend.buf\n",
        encoding="utf-8",
    )
    remapped = struct.pack("<4f4I", 1.0, 0.0, 0.0, 0.0, 9, 1, 2, 3)

    received = []
    errors = []
    worker = fix_mod_worker(mod, mod, datasets)
    worker.done.connect(received.append)
    worker.error.connect(errors.append)
    worker.run()
    assert errors == [], f"worker errored: {errors}"
    assert len(received) == 1
    variant, plans, written = received[0]
    assert variant == repo.DEFAULT_VARIANT
    assert isinstance(plans, list)
    assert plans == []  # unknown hash -> no ini fixes
    assert written == 0
    assert buf.read_bytes() == remapped
    backups = sorted(store.rglob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert backups[0].parent == blend_state_path(store, mod).parent
    marker = json.loads(blend_state_path(store, mod).read_text(encoding="utf-8"))
    assert set(marker) == {"x.blend.buf"}
    entry = marker["x.blend.buf"]
    assert entry["hash"] == "aabbccdd"
    assert entry["after"] == hashlib.sha256(remapped).hexdigest()
    assert entry["before"] == hashlib.sha256(original).hexdigest()
    assert isinstance(entry["stamp"], int)

    received2 = []
    errors2 = []
    worker2 = fix_mod_worker(mod, mod, datasets)
    worker2.done.connect(received2.append)
    worker2.error.connect(errors2.append)
    worker2.run()
    assert errors2 == [], f"worker errored: {errors2}"
    assert buf.read_bytes() == remapped  # marker guard: no double application
    assert sorted(store.rglob("*.bak")) == backups


def test_friendly_error_maps_cleaned_runtime_dir():
    exc = FileNotFoundError(
        2, "No such file or directory: 'C:\\\\Temp\\\\_MEI000064402\\\\base_library.zip'"
    )
    message = friendly_error(exc)
    assert message.startswith("ZZZHashFix's runtime files were cleaned")
    assert "relaunch" in message


def test_friendly_error_passes_other_messages_through():
    exc = ValueError("backup no longer exists: x.bak")
    assert friendly_error(exc) == "ValueError: backup no longer exists: x.bak"
    os_error = OSError(5, "Access is denied: 'C:\\\\mods\\\\x.ini'")
    assert friendly_error(os_error) == (
        "OSError: [Errno 5] Access is denied: 'C:\\\\mods\\\\x.ini'"
    )


def test_updates_tooltip_maps_every_signal():
    current = updates_tooltip(CURRENT_SIGNAL).lower()
    assert "nothing" in current and "to fix" in current.replace("\n", " ")
    assert "older game version" in updates_tooltip(UPDATES_SIGNAL).lower()
    assert "renames" in updates_tooltip(UPDATES_SIGNAL).lower()
    structural = updates_tooltip(STRUCTURAL_SIGNAL)
    assert "structural" in structural
    assert "insert" in structural and "added" in structural
    assert "cannot be determined" in updates_tooltip("unknown").replace("\n", " ")
    assert updates_tooltip("") == ""


def test_hashes_tooltip_spells_out_counts():
    version = ModVersion(
        current_count=2,
        outdated_count=3,
        unknown_count=1,
        total=6,
        structural_count=1,
    )
    text = hashes_tooltip(version)
    assert "3" in text and "2" in text and "1" in text and "6" in text
    assert "structural" in text and "insert" in text
    assert hashes_tooltip(ModVersion()) == ""


def test_tooltip_text_wraps_at_width():
    text = "read some of this long line that goes on and on"
    wrapped = wrap_tooltip(text, width=20)
    lines = wrapped.split("\n")
    assert all(len(line) <= 20 for line in lines)
    assert wrapped.replace("\n", " ") == text
    assert wrap_tooltip("a\n\nb") == "a\n\nb"
    assert wrap_tooltip("") == ""
    for signal in (CURRENT_SIGNAL, UPDATES_SIGNAL, STRUCTURAL_SIGNAL, "unknown"):
        assert all(
            len(line) <= 70 for line in updates_tooltip(signal).split("\n")
        )


def test_update_data_worker_calls_ensure_repo(tmp_path, monkeypatch):
    called: list[str] = []

    def fake_ensure(variant, cache_dir=None, log: Callable[[str], None] = print):
        assert cache_dir is None
        called.append(variant)
        log(f"ensured {variant}")
        return tmp_path

    monkeypatch.setattr("tools.ui.worker.ensure_repo", fake_ensure)
    errors: list[str] = []
    logs: list[str] = []
    worker = update_data_worker()
    worker.log.connect(logs.append)
    worker.error.connect(errors.append)
    worker.run()

    assert called == ["2048p", "1024p"]
    assert logs[:2] == ["ensured 2048p", "ensured 1024p"]
    assert len(errors) == 1
    assert "no local hash data" in errors[0]
