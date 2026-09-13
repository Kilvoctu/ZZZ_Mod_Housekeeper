"""Tests for tools.texcoord_upgrade: conversion, target finding, apply and markers."""

import json
import struct
from hashlib import sha256
from pathlib import Path

import pytest

from tools.characters import CharacterDB
from tools.dumpdata import DumpData, DumpLayout
from tools.fixer import FixerData
from tools.texcoord_upgrade import (
    TexcoordTarget,
    apply_upgrade,
    buffer_gate,
    dump_face_texcoord_hashes,
    dump_match_for_hash,
    find_targets,
    marker_kind,
    prune_texcoord_markers,
    remove_texcoord_state_keys,
    rewrite_texcoord_state_keys,
    scan_texcoord_targets,
    texcoord_state_path,
    upgrade_bytes,
)

FACE_HASHES = frozenset({"aaaa0001"})


def _two_record_buffer() -> bytes:
    return bytes((0, 255, 16, 0)) + bytes(range(4, 36)) + bytes((200, 1, 64, 0)) + bytes(range(36, 68))


def _expected_upgrade(data: bytes) -> bytes:
    out = b""
    for index in range(0, len(data), 36):
        out += struct.pack("<ffff", *(byte / 255.0 for byte in data[index : index + 4]))
        out += data[index + 4 : index + 36]
    return out


def test_upgrade_bytes_unpacks_packed_component():
    data = _two_record_buffer()
    upgraded = upgrade_bytes(data)
    assert upgraded == _expected_upgrade(data)
    assert len(upgraded) == 96


def test_upgrade_bytes_rejects_unaligned():
    with pytest.raises(ValueError):
        upgrade_bytes(b"\x00" * 37)


TARGETS_INI = (
    "[TextureOverrideCharFaceTexcoord]\n"
    "hash = aaaa0001\n"
    "vb1 = ResourceCharFaceTexcoord.0\n"
    "\n"
    "[TextureOverrideCharBodyTexcoord]\n"
    "hash = bbbb0002\n"
    "vb0 = ResourceCharBodyTexcoord\n"
    "\n"
    "[ResourceCharFaceTexcoord.0]\n"
    "type = Buffer\n"
    "stride = 36\n"
    "filename = .\\01\\face.buf\n"
    "\n"
    "[ResourceCharFaceTexcoord.1]\n"
    "type = Buffer\n"
    "stride = 36\n"
    "filename = .\\02\\face.buf\n"
    "\n"
    "[ResourceCharFaceTexcoord.2]\n"
    "type = Buffer\n"
    "stride = 48\n"
    "filename = .\\03\\face.buf\n"
    "\n"
    "[ResourceCharBodyTexcoord]\n"
    "type = Buffer\n"
    "stride = 36\n"
    "filename = body.buf\n"
)


def test_find_targets_matches_variant_blocks_and_gates_on_face_hashes(tmp_path):
    targets = find_targets(TARGETS_INI, tmp_path / "m.ini", FACE_HASHES)
    assert [(t.resource, t.path.name) for t in targets] == [
        ("CharFaceTexcoord.0", "face.buf"),
        ("CharFaceTexcoord.1", "face.buf"),
    ]
    assert targets[0].path == tmp_path / "01" / "face.buf"
    assert targets[1].path == tmp_path / "02" / "face.buf"
    assert targets[0].hash == "aaaa0001"
    assert targets[0].ini_path == tmp_path / "m.ini"


def test_scan_texcoord_targets_dedupes_and_skips_undecodable(tmp_path):
    mod = tmp_path / "DISABLED_Mod"
    mod.mkdir()
    (mod / "m.ini").write_text(TARGETS_INI, encoding="utf-8")
    (mod / "broken.ini").write_bytes(b"\xff\xfe\x00bad")
    targets = scan_texcoord_targets(tmp_path, FACE_HASHES)
    assert len(targets) == 2


def _write_mod(tmp_path: Path) -> tuple[Path, Path, bytes]:
    mod = tmp_path / "mods" / "DISABLED_Mod"
    mod.mkdir(parents=True)
    ini = (
        "[TextureOverrideCharFaceTexcoord]\r\n"
        "hash = aaaa0001\r\n"
        "vb1 = ResourceCharFaceTexcoord\r\n"
        "\r\n"
        "[ResourceCharFaceTexcoord]\r\n"
        "type = Buffer\r\n"
        "stride = 36\r\n"
        "filename = face.buf\r\n"
    )
    (mod / "m.ini").write_text(ini, encoding="utf-8", newline="")
    original = _two_record_buffer()
    (mod / "face.buf").write_bytes(original)
    return mod, tmp_path / "store", original


def _target(mod: Path) -> TexcoordTarget:
    return TexcoordTarget(
        hash="aaaa0001",
        resource="CharFaceTexcoord",
        path=mod / "face.buf",
        ini_path=mod / "m.ini",
    )


def test_apply_upgrade_converts_rewrites_stride_backs_up_and_marks(tmp_path):
    mod, store, original = _write_mod(tmp_path)
    assert apply_upgrade(_target(mod), store, tmp_path / "mods", log=lambda *_: None) is True
    assert (mod / "face.buf").read_bytes() == upgrade_bytes(original)
    ini_text = (mod / "m.ini").read_text(encoding="utf-8")
    assert "stride = 48" in ini_text and "stride = 36" not in ini_text
    marker = json.loads(
        texcoord_state_path(store, tmp_path / "mods").read_text(encoding="utf-8")
    )
    assert marker["Mod/face.buf"]["before"] == sha256(original).hexdigest()
    assert marker["Mod/face.buf"]["hash"] == "aaaa0001"
    assert any("face.buf" in path.name for path in store.rglob("*.bak"))
    assert any("m.ini" in path.name for path in store.rglob("*.bak"))


def test_apply_upgrade_is_idempotent_and_completes_stride(tmp_path):
    mod, store, _original = _write_mod(tmp_path)
    assert apply_upgrade(_target(mod), store, tmp_path / "mods", log=lambda *_: None) is True
    (mod / "m.ini").write_text(
        (mod / "m.ini")
        .read_text(encoding="utf-8")
        .replace("stride = 48", "stride = 36"),
        encoding="utf-8",
        newline="",
    )
    assert apply_upgrade(_target(mod), store, tmp_path / "mods", log=lambda *_: None) is True
    assert "stride = 48" in (mod / "m.ini").read_text(encoding="utf-8")
    assert apply_upgrade(_target(mod), store, tmp_path / "mods", log=lambda *_: None) is False


def test_apply_upgrade_skips_unaligned_buffers(tmp_path):
    mod, store, _original = _write_mod(tmp_path)
    (mod / "face.buf").write_bytes(b"\x00" * 37)
    logs: list[str] = []
    assert apply_upgrade(_target(mod), store, tmp_path / "mods", log=logs.append) is False
    assert any("36-byte aligned" in line for line in logs)


def test_rewrite_and_remove_texcoord_state_keys(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    state_path = texcoord_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"Old/x.buf": {"after": "a"}, "Keep/y.buf": {"after": "b"}}),
        encoding="utf-8",
    )
    assert rewrite_texcoord_state_keys(store, mods, "Old", "New") == 1
    reloaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(reloaded) == {"New/x.buf", "Keep/y.buf"}
    assert remove_texcoord_state_keys(store, mods, "New") == 1
    assert set(json.loads(state_path.read_text(encoding="utf-8"))) == {"Keep/y.buf"}
    assert remove_texcoord_state_keys(store, mods, "Ghost") == 0


def _write_state_file(store: Path, mods: Path, state: dict[str, object]) -> None:
    """Write the marker JSON directly, so tests never go through _write_state."""
    state_path = texcoord_state_path(store, mods)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def test_marker_kind_distinguishes_action_and_legacy(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    _write_state_file(
        store,
        mods,
        {
            "Mod/converted.buf": {"action": "convert", "before": "a"},
            "Mod/restored.buf": {"action": "restore", "before": "b"},
            "Mod/legacy.buf": {"before": "c"},
            "Mod/notdict.buf": "junk",
        },
    )
    assert (
        marker_kind(store, mods, mods / "Mod" / "converted.buf") == "buffer conversion"
    )
    assert marker_kind(store, mods, mods / "Mod" / "restored.buf") == "dump restore"
    assert marker_kind(store, mods, mods / "Mod" / "legacy.buf") == "buffer conversion"
    assert marker_kind(store, mods, mods / "Mod" / "absent.buf") is None
    assert marker_kind(store, mods, mods / "Mod" / "notdict.buf") is None


def test_prune_texcoord_markers_drops_only_restored_buffers(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "Mod" / "face.buf"
    live.parent.mkdir(parents=True)
    payload = _two_record_buffer()
    live.write_bytes(payload)
    stale = mods / "Mod" / "edited.buf"
    stale.write_bytes(b"\x00" * 72)
    ini_live = mods / "Mod" / "m.ini"
    ini_live.write_text("[ResourceCharFaceTexcoord]\n", encoding="utf-8")
    _write_state_file(
        store,
        mods,
        {
            "Mod/face.buf": {
                "action": "convert",
                "before": sha256(payload).hexdigest(),
            },
            "Mod/edited.buf": {"action": "convert", "before": "0" * 64},
            "Mod/gone.buf": {
                "action": "convert",
                "before": sha256(payload).hexdigest(),
            },
            "Mod/m.ini": {
                "action": "convert",
                "before": sha256(ini_live.read_bytes()).hexdigest(),
            },
        },
    )
    assert (
        prune_texcoord_markers(
            store,
            mods,
            [live, stale, mods / "Mod" / "gone.buf", ini_live],
        )
        == 1
    )
    reloaded = json.loads(
        texcoord_state_path(store, mods).read_text(encoding="utf-8")
    )
    assert set(reloaded) == {"Mod/edited.buf", "Mod/gone.buf", "Mod/m.ini"}


def test_prune_texcoord_markers_without_state_is_noop(tmp_path):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "Mod" / "face.buf"
    live.parent.mkdir(parents=True)
    live.write_bytes(_two_record_buffer())
    assert prune_texcoord_markers(store, mods, [live]) == 0
    assert not texcoord_state_path(store, mods).exists()


def test_prune_texcoord_markers_writes_state_once(tmp_path, monkeypatch):
    mods = tmp_path / "mods"
    store = tmp_path / "store"
    live = mods / "Mod" / "face.buf"
    other = mods / "Other" / "face.buf"
    live.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    payload = _two_record_buffer()
    live.write_bytes(payload)
    other.write_bytes(payload)
    before = sha256(payload).hexdigest()
    _write_state_file(
        store,
        mods,
        {
            "Mod/face.buf": {"action": "convert", "before": before},
            "Other/face.buf": {"action": "convert", "before": before},
        },
    )
    writes: list[dict] = []

    def counting_write(store_dir: Path, mods_dir: Path, state: dict[str, dict]) -> None:
        writes.append(state)
        # Persist the pruned state too, so the follow-up prune reloads it empty.
        _write_state_file(store_dir, mods_dir, state)

    monkeypatch.setattr("tools.texcoord_upgrade._write_state", counting_write)
    assert prune_texcoord_markers(store, mods, [live, other]) == 2
    assert len(writes) == 1
    assert prune_texcoord_markers(store, mods, [live, other]) == 0
    assert len(writes) == 1


def _dump_data(
    tmp_path: Path,
    *,
    stride: int = 48,
    binary: bytes | None = None,
    hash_value: str = "aaaa0001",
) -> DumpData:
    """Synthetic dump data for one face component ("chara"/"脸") plus its binary."""
    data = DumpData()
    key = ("chara", "脸", "texcoord")
    data.current_hashes[key] = hash_value.lower()
    data.layouts[key] = DumpLayout(stride=stride, slot="vb1", filename="t.buf")
    if binary is not None:
        path = tmp_path / "dump" / "chara-脸" / "t.buf"
        path.parent.mkdir(parents=True)
        path.write_bytes(binary)
        data.binaries[key] = path
    return data


def _marker(store: Path, mods: Path) -> dict:
    return json.loads(
        texcoord_state_path(store, mods).read_text(encoding="utf-8")
    )["Mod/face.buf"]


def test_dump_face_texcoord_helpers():
    empty = DumpData()
    assert dump_face_texcoord_hashes(empty) == frozenset()
    assert dump_match_for_hash(empty, "aaaa0001") is None
    data = DumpData()
    data.current_hashes = {
        ("aardvark", "脸", "texcoord"): "aaaa0001",
        ("chara", "脸", "texcoord"): "aaaa0001",
        ("chara", "Face", "texcoord"): "cccc0003",
        ("chara", "body", "texcoord"): "bbbb0002",
        ("chara", "腿", "texcoord"): "dddd0004",
        ("chara", "脸", "position"): "eeee0005",
    }
    assert dump_face_texcoord_hashes(data) == frozenset({"aaaa0001", "cccc0003"})
    assert dump_match_for_hash(data, "AAAA0001") == ("aardvark", "脸")
    assert dump_match_for_hash(data, "cccc0003") == ("chara", "Face")
    assert dump_match_for_hash(data, "ffff0006") is None


def test_buffer_gate_merges_override_and_dump_hashes():
    data = FixerData(
        chains={},
        ib_index_changes={},
        db=CharacterDB(),
        face_texcoord_hashes=frozenset({"aaaa0001"}),
        dumps=DumpData(
            current_hashes={
                ("chara", "脸", "texcoord"): "cccc0003",
                ("chara", "body", "texcoord"): "dddd0004",
            }
        ),
    )
    assert buffer_gate(data) == frozenset({"aaaa0001", "cccc0003"})


def test_apply_upgrade_restores_missing_buffer_from_dump(tmp_path):
    mod, store, _original = _write_mod(tmp_path)
    (mod / "face.buf").unlink()
    converted = upgrade_bytes(_two_record_buffer())
    logs: list[str] = []
    assert (
        apply_upgrade(
            _target(mod), store, tmp_path / "mods", log=logs.append,
            dumps=_dump_data(tmp_path, binary=converted),
        )
        is True
    )
    assert (mod / "face.buf").read_bytes() == converted
    marker = _marker(store, tmp_path / "mods")
    assert marker["action"] == "restore"
    assert marker["source"] == "t.buf"
    assert "stride = 48" in (mod / "m.ini").read_text(encoding="utf-8")
    assert any("restored missing face texcoord from dump" in line for line in logs)


def test_apply_upgrade_missing_buffer_without_dump_still_skips(tmp_path):
    mod, store, _original = _write_mod(tmp_path)
    (mod / "face.buf").unlink()
    logs: list[str] = []
    assert apply_upgrade(_target(mod), store, tmp_path / "mods", log=logs.append) is False
    assert any("missing" in line for line in logs)
    assert not (mod / "face.buf").exists()


def test_apply_upgrade_restores_dump_bytes_when_conversion_matches(tmp_path):
    mod, store, original = _write_mod(tmp_path)
    converted = upgrade_bytes(original)
    logs: list[str] = []
    assert (
        apply_upgrade(
            _target(mod), store, tmp_path / "mods", log=logs.append,
            dumps=_dump_data(tmp_path, binary=converted),
        )
        is True
    )
    assert (mod / "face.buf").read_bytes() == converted
    marker = _marker(store, tmp_path / "mods")
    assert marker["action"] == "restore"
    assert marker["source"] == "t.buf"
    assert marker["before"] == sha256(original).hexdigest()
    assert any("restored face texcoord from dump" in line for line in logs)


def test_apply_upgrade_converts_edited_buffer_and_keeps_dump_reference(tmp_path):
    mod, store, original = _write_mod(tmp_path)
    converted = upgrade_bytes(original)
    edited_dump = converted[:-1] + bytes((converted[-1] ^ 0xFF,))
    logs: list[str] = []
    assert (
        apply_upgrade(
            _target(mod), store, tmp_path / "mods", log=logs.append,
            dumps=_dump_data(tmp_path, binary=edited_dump),
        )
        is True
    )
    assert (mod / "face.buf").read_bytes() == converted
    marker = _marker(store, tmp_path / "mods")
    assert marker["action"] == "convert"
    assert marker["source"] == ""
    assert any("mod buffer had edits" in line for line in logs)
    assert any("t.buf" in line for line in logs)


def test_apply_upgrade_without_dump_logs_unverified(tmp_path):
    mod, store, original = _write_mod(tmp_path)
    logs: list[str] = []
    assert apply_upgrade(_target(mod), store, tmp_path / "mods", log=logs.append) is True
    assert (mod / "face.buf").read_bytes() == upgrade_bytes(original)
    marker = _marker(store, tmp_path / "mods")
    assert marker["action"] == "convert"
    assert marker["source"] == ""
    assert any("unverified (no dump for this component)" in line for line in logs)


def test_apply_upgrade_restores_non_48_dump_without_converting(tmp_path):
    mod, store, _original = _write_mod(tmp_path)
    dump_bytes = bytes(range(48))
    logs: list[str] = []
    assert (
        apply_upgrade(
            _target(mod), store, tmp_path / "mods", log=logs.append,
            dumps=_dump_data(tmp_path, stride=24, binary=dump_bytes),
        )
        is True
    )
    assert (mod / "face.buf").read_bytes() == dump_bytes
    marker = _marker(store, tmp_path / "mods")
    assert marker["action"] == "restore"
    assert marker["source"] == "t.buf"
    assert "stride = 48" in (mod / "m.ini").read_text(encoding="utf-8")
    assert any("no conversion rule" in line for line in logs)


def test_apply_upgrade_ignores_dump_match_without_shipped_binary(tmp_path):
    mod, store, original = _write_mod(tmp_path)
    logs: list[str] = []
    assert (
        apply_upgrade(
            _target(mod), store, tmp_path / "mods", log=logs.append,
            dumps=_dump_data(tmp_path, binary=None),
        )
        is True
    )
    assert (mod / "face.buf").read_bytes() == upgrade_bytes(original)
    marker = _marker(store, tmp_path / "mods")
    assert marker["action"] == "convert"
    assert any("unverified" in line for line in logs)


def test_apply_upgrade_old_marker_without_action_still_suppresses(tmp_path):
    mod, store, original = _write_mod(tmp_path)
    converted = upgrade_bytes(original)
    (mod / "face.buf").write_bytes(converted)
    state_path = texcoord_state_path(store, tmp_path / "mods")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "Mod/face.buf": {
                    "after": sha256(converted).hexdigest(),
                    "before": sha256(original).hexdigest(),
                    "hash": "aaaa0001",
                    "stamp": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    logs: list[str] = []
    assert (
        apply_upgrade(
            _target(mod), store, tmp_path / "mods", log=logs.append,
            dumps=_dump_data(tmp_path, binary=converted),
        )
        is True
    )
    assert (mod / "face.buf").read_bytes() == converted
    assert "stride = 48" in (mod / "m.ini").read_text(encoding="utf-8")
    assert "action" not in _marker(store, tmp_path / "mods")
