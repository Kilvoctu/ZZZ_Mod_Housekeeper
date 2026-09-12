"""Tests for tools.blend_remap: dataset pinning, ini parsing, byte remapping, and the
marker/backup interplay that keeps the not-involution tables from double-applying."""

import hashlib
import json
import struct

import pytest

from tools.blend_remap import (
    BlendTables,
    BlendTarget,
    apply_remap,
    blend_remaps_path,
    blend_state_path,
    find_targets,
    load_blend_remaps,
    remap_bytes,
    remove_blend_state_keys,
    resolve_blend_table,
    rewrite_blend_state_keys,
)
from tools.fixer import revert_backups


def _record(weights: tuple[float, ...], indices: tuple[int, ...]) -> bytes:
    """One 32-byte blend record: 4 float weights then 4 little-endian uint32 indices."""
    return struct.pack("<4f4I", *weights, *indices)


_SIX_SECTION_INI = (
    "[TextureOverrideFoo]\n"
    "hash = 93b69961\n"
    "handling = skip\n"
    "vb2 = ResourceFoo\n"
    "if DRAW_TYPE == 1\n"
    "\tdraw = 64534, 0\n"
    "endif\n"
    "\n"
    "[TextureOverrideBar]\n"
    "hash = 0929171b\n"
    "handling = skip\n"
    "vb2 = ResourceBar\n"
    "\n"
    "[TextureOverrideBaz]\n"
    "hash = 2702db6d\n"
    "handling = skip\n"
    "vb2 = ResourceBaz\n"
    "\n"
    "[TextureOverrideNoVB]\n"
    "hash = 93b69961\n"
    "handling = skip\n"
    "\n"
    "[TextureOverrideZap]\n"
    "hash = da54a57a\n"
    "handling = skip\n"
    "vb2 = ResourceZap\n"
    "\n"
    "[TextureOverrideMissing]\n"
    "hash = da54a57a\n"
    "handling = skip\n"
    "vb2 = ResourceMissing\n"
    "\n"
    "[ResourceFoo]\n"
    "type = Buffer\n"
    "stride = 32\n"
    "filename = foo.buf\n"
    "\n"
    "[ResourceBar]\n"
    "type = Buffer\n"
    "stride = 32\n"
    "filename = bar.buf\n"
    "\n"
    "[ResourceBaz]\n"
    "type = Buffer\n"
    "stride = 32\n"
    "filename = baz.buf\n"
    "\n"
    "[ResourceZap]\n"
    "type = Buffer\n"
    "stride = 40\n"
    "filename = zap.buf\n"
    "\n"
    "[ResourceMissing]\n"
    "type = Buffer\n"
    "stride = 32\n"
    "filename = missing.buf\n"
)

_X_SECTION_INI = (
    "[TextureOverrideX]\n"
    "hash = aabbccdd\n"
    "handling = skip\n"
    "vb2 = ResourceX\n"
    "\n"
    "[ResourceX]\n"
    "type = Buffer\n"
    "stride = 32\n"
    "filename = x.buf\n"
)

_X_TABLES = BlendTables(mappings={"aabbccdd": {5: 9}}, position_to_blend={})


def test_load_blend_remaps_pins_shipped_tables():
    tables = load_blend_remaps(blend_remaps_path())
    assert {name: len(table) for name, table in tables.mappings.items()} == {
        "da54a57a": 4,
        "93b69961": 59,
        "3841a909": 20,
    }
    assert tables.position_to_blend == {
        "d16f6790": "da54a57a",
        "0929171b": "93b69961",
        "11fbd4a3": "3841a909",
    }
    body = tables.mappings["93b69961"]
    assert body[68] == 127
    assert body[69] == 68
    assert body[71] == 69
    assert body[127] == 126
    assert 70 not in body
    legs = tables.mappings["3841a909"]
    assert legs[18] == 19
    assert legs[19] == 18
    assert legs[26] == 43
    assert legs[43] == 42
    white = tables.mappings["da54a57a"]
    assert white[0] == 1
    assert white[1] == 0
    assert white[42] == 43
    assert white[43] == 42


def test_load_blend_remaps_rejects_malformed(tmp_path):
    missing = tmp_path / "missing-mappings.json"
    missing.write_text('{"position_to_blend": {}}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_blend_remaps(missing)
    non_decimal = tmp_path / "non-decimal.json"
    non_decimal.write_text(
        '{"mappings": {"aabbccdd": {"5": "x"}}, "position_to_blend": {}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_blend_remaps(non_decimal)


def test_resolve_blend_table_direct_alias_and_miss():
    tables = BlendTables(
        mappings={"93b69961": {71: 69}},
        position_to_blend={"0929171b": "93b69961", "deadbeef": "00112233"},
    )
    table = tables.mappings["93b69961"]
    assert resolve_blend_table("93B69961", tables) == ("93b69961", table)
    assert resolve_blend_table("0929171B", tables) == ("93b69961", table)
    assert resolve_blend_table("ffffffff", tables) is None
    assert resolve_blend_table("deadbeef", tables) is None


def test_find_targets_finds_direct_alias_and_skips_others(tmp_path):
    tables = load_blend_remaps()
    (tmp_path / "foo.buf").write_bytes(b"")
    (tmp_path / "bar.buf").write_bytes(b"")
    targets = find_targets(_SIX_SECTION_INI, tmp_path, tables)
    assert [(t.hash, t.resource, t.path) for t in targets] == [
        ("93b69961", "Foo", tmp_path / "foo.buf"),
        ("93b69961", "Bar", tmp_path / "bar.buf"),
    ]
    assert targets[0].table is targets[1].table is tables.mappings["93b69961"]


def test_find_targets_requires_consecutive_resource_lines(tmp_path):
    tables = load_blend_remaps()
    (tmp_path / "foo.buf").write_bytes(b"")
    broken = (
        "[TextureOverrideFoo]\n"
        "hash = 93b69961\n"
        "handling = skip\n"
        "vb2 = ResourceFoo\n"
        "\n"
        "[ResourceFoo]\n"
        "type = Buffer\n"
        "format = DXGI_FORMAT_R32_UINT\n"
        "stride = 32\n"
        "filename = foo.buf\n"
    )
    assert find_targets(broken, tmp_path, tables) == []
    tolerated = (
        "[TextureOverrideFoo]\n"
        "hash = 93b69961\n"
        "handling = skip\n"
        "vb2 = ResourceFoo\n"
        "\n"
        "[ResourceFoo]\n"
        "; generated by a mod tool\n"
        "\n"
        "type = Buffer\n"
        "stride = 32\n"
        "filename = foo.buf\n"
    )
    targets = find_targets(tolerated, tmp_path, tables)
    assert [(t.hash, t.resource, t.path.name) for t in targets] == [
        ("93b69961", "Foo", "foo.buf")
    ]


def test_find_targets_dedupes_by_hash_and_path(tmp_path):
    tables = load_blend_remaps()
    (tmp_path / "foo.buf").write_bytes(b"")
    text = (
        "[TextureOverrideFoo]\n"
        "hash = 93b69961\n"
        "handling = skip\n"
        "vb2 = ResourceFoo\n"
        "\n"
        "[TextureOverrideFoo2]\n"
        "hash = 93b69961\n"
        "handling = skip\n"
        "vb2 = ResourceFoo2\n"
        "\n"
        "[ResourceFoo]\n"
        "type = Buffer\n"
        "stride = 32\n"
        "filename = foo.buf\n"
        "\n"
        "[ResourceFoo2]\n"
        "type = Buffer\n"
        "stride = 32\n"
        "filename = foo.buf\n"
    )
    targets = find_targets(text, tmp_path, tables)
    assert [(t.hash, t.resource, t.path) for t in targets] == [
        ("93b69961", "Foo", tmp_path / "foo.buf")
    ]


def test_remap_bytes_applies_table_and_preserves_weights():
    tables = load_blend_remaps()
    data = (
        _record((0.25, -1.5, 2.0, 3.25), (71, 66, 63, 82))
        + _record((0.5, 0.5, 0.0, 0.0), (68, 127, 5, 9))
        + b"\x07\x09\x00\xab\xcd"
    )
    new, changed = remap_bytes(data, tables.mappings["93b69961"])
    assert changed == 4
    assert len(new) == len(data)
    assert new[-5:] == data[-5:]
    assert new[:16] == data[:16]
    assert new[32:48] == data[32:48]
    assert struct.unpack("<4f", new[:16]) == (0.25, -1.5, 2.0, 3.25)
    assert struct.unpack("<4f", new[32:48]) == (0.5, 0.5, 0.0, 0.0)
    assert struct.unpack("<4I", new[16:32]) == (69, 66, 63, 81)
    assert struct.unpack("<4I", new[48:64]) == (127, 126, 5, 9)


def test_remap_bytes_identity_for_out_of_domain():
    tables = load_blend_remaps()
    data = _record((1.0, 0.0, 0.0, 0.0), (5, 9, 5, 9)) + _record(
        (0.5, 0.5, 0.0, 0.0), (6, 7, 8, 10)
    )
    new, changed = remap_bytes(data, tables.mappings["93b69961"])
    assert changed == 0
    assert new == data


def test_apply_remap_round_trip_backup_marker_and_idempotence(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    buf = mods / "x.buf"
    original = _record((1.0, 0.0, 0.0, 0.0), (5, 1, 2, 3)) + _record(
        (0.5, 0.5, 0.0, 0.0), (5, 5, 1, 2)
    )
    buf.write_bytes(original)
    ini = mods / "m.ini"
    ini.write_text(_X_SECTION_INI, encoding="utf-8")
    (target,) = find_targets(ini.read_text(encoding="utf-8"), mods, _X_TABLES)
    assert (target.hash, target.resource, target.path) == ("aabbccdd", "X", buf)
    remapped = _record((1.0, 0.0, 0.0, 0.0), (9, 1, 2, 3)) + _record(
        (0.5, 0.5, 0.0, 0.0), (9, 9, 1, 2)
    )
    logs: list[str] = []
    assert apply_remap(target, store, mods, log=logs.append) is True
    assert buf.read_bytes() == remapped
    baks = sorted(store.rglob("*.bak"))
    assert len(baks) == 1 and baks[0].read_bytes() == original
    assert len(logs) == 1
    assert logs[0].startswith(
        "remapped blend indices: x.buf (aabbccdd) — 3 index values changed (backup: x.buf -- "
    )
    assert logs[0].endswith(".bak)")
    marker = json.loads(blend_state_path(store, mods).read_text(encoding="utf-8"))
    assert set(marker) == {"x.buf"}
    entry = marker["x.buf"]
    assert entry["hash"] == "aabbccdd"
    assert entry["after"] == hashlib.sha256(remapped).hexdigest()
    assert entry["before"] == hashlib.sha256(original).hexdigest()
    assert isinstance(entry["stamp"], int)
    logs.clear()
    assert apply_remap(target, store, mods, log=logs.append) is False
    assert logs == []
    assert buf.read_bytes() == remapped
    assert sorted(store.rglob("*.bak")) == baks


def test_apply_remap_changed_zero_records_marker_without_backup(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    buf = mods / "x.buf"
    data = _record((1.0, 0.0, 0.0, 0.0), (1, 2, 3, 4))
    buf.write_bytes(data)
    target = BlendTarget(
        hash="aabbccdd", table=_X_TABLES.mappings["aabbccdd"], resource="X", path=buf
    )
    logs: list[str] = []
    assert apply_remap(target, store, mods, log=logs.append) is False
    assert logs == ["blend indices already current: x.buf (aabbccdd)"]
    assert list(store.rglob("*.bak")) == []
    assert buf.read_bytes() == data
    entry = json.loads(blend_state_path(store, mods).read_text(encoding="utf-8"))["x.buf"]
    assert entry["after"] == entry["before"] == hashlib.sha256(data).hexdigest()
    assert entry["hash"] == "aabbccdd"
    assert isinstance(entry["stamp"], int)


def test_apply_remap_backup_false_writes_no_store_file(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    buf = mods / "x.buf"
    original = _record((1.0, 0.0, 0.0, 0.0), (5, 1, 2, 3))
    buf.write_bytes(original)
    target = BlendTarget(
        hash="aabbccdd", table=_X_TABLES.mappings["aabbccdd"], resource="X", path=buf
    )
    remapped = _record((1.0, 0.0, 0.0, 0.0), (9, 1, 2, 3))
    logs: list[str] = []
    assert apply_remap(target, store, mods, log=logs.append, backup=False) is True
    assert buf.read_bytes() == remapped
    assert list(store.rglob("*.bak")) == []
    assert logs == ["remapped blend indices: x.buf (aabbccdd) — 1 index values changed"]
    entry = json.loads(blend_state_path(store, mods).read_text(encoding="utf-8"))["x.buf"]
    assert entry["after"] == hashlib.sha256(remapped).hexdigest()
    assert entry["before"] == hashlib.sha256(original).hexdigest()
    assert entry["hash"] == "aabbccdd"


def test_apply_remap_state_key_canonicalizes_disabled_dirs(tmp_path):
    mods = tmp_path / "mods"
    sub = mods / "DISABLED_Sub"
    sub.mkdir(parents=True)
    store = tmp_path / "store"
    buf = sub / "x.buf"
    buf.write_bytes(_record((1.0, 0.0, 0.0, 0.0), (5, 1, 2, 3)))
    ini = sub / "m.ini"
    ini.write_text(_X_SECTION_INI, encoding="utf-8")
    (target,) = find_targets(ini.read_text(encoding="utf-8"), sub, _X_TABLES)
    assert target.path == sub / "x.buf"
    assert apply_remap(target, store, mods, log=lambda *_: None) is True
    marker = json.loads(blend_state_path(store, mods).read_text(encoding="utf-8"))
    assert set(marker) == {"Sub/x.buf"}
    baks = sorted(store.rglob("*.bak"))
    assert len(baks) == 1 and baks[0].parent.name == "Sub"


def test_revert_then_reapply_restores_original_and_reapplies(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    buf = mods / "x.buf"
    original = _record((1.0, 0.0, 0.0, 0.0), (5, 1, 2, 3))
    buf.write_bytes(original)
    remapped = _record((1.0, 0.0, 0.0, 0.0), (9, 1, 2, 3))
    target = BlendTarget(
        hash="aabbccdd", table=_X_TABLES.mappings["aabbccdd"], resource="X", path=buf
    )
    assert apply_remap(target, store, mods, log=lambda *_: None) is True
    assert buf.read_bytes() == remapped
    (backup,) = sorted(store.rglob("*.bak"))
    assert backup.read_bytes() == original
    assert revert_backups([(buf, backup)], log=lambda *_: None) == 1
    assert buf.read_bytes() == original
    assert not backup.exists()
    assert apply_remap(target, store, mods, log=lambda *_: None) is True
    assert buf.read_bytes() == remapped
    (fresh,) = sorted(store.rglob("*.bak"))
    # The consumed backup was moved away, so the reapply writes a fresh one; its
    # name may legitimately be recycled when both applies land in the same second.
    assert fresh.read_bytes() == original
    assert fresh.name.startswith("x.buf -- ")
    marker = json.loads(blend_state_path(store, mods).read_text(encoding="utf-8"))
    assert marker["x.buf"]["after"] == hashlib.sha256(remapped).hexdigest()
    assert marker["x.buf"]["before"] == hashlib.sha256(original).hexdigest()


def test_rewrite_blend_state_keys_rewrites_and_matches_apply_style(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "Old/a.buf": {"hash": "aabbccdd", "stamp": 1},
                "Other/b.buf": {"hash": "11223344", "stamp": 2},
            }
        ),
        encoding="utf-8",
    )

    assert rewrite_blend_state_keys(store, mods, "Old", "New") == 1

    expected = {
        "New/a.buf": {"hash": "aabbccdd", "stamp": 1},
        "Other/b.buf": {"hash": "11223344", "stamp": 2},
    }
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected
    assert state_path.read_text(encoding="utf-8") == (
        json.dumps(expected, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )


def test_rewrite_blend_state_keys_category_prefix(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"Cat/x/y.buf": {"hash": "aabbccdd"}, "keep/z.buf": {}}),
        encoding="utf-8",
    )

    assert rewrite_blend_state_keys(store, mods, "Cat", "Pets") == 1

    rewritten = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(rewritten) == {"Pets/x/y.buf", "keep/z.buf"}
    assert rewritten["keep/z.buf"] == {}


def test_rewrite_blend_state_keys_no_marker_file_writes_nothing(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"

    assert rewrite_blend_state_keys(store, mods, "Old", "New") == 0

    assert not blend_state_path(store, mods).exists()
    assert not store.exists()


def test_rewrite_blend_state_keys_ignores_blank_and_identical_prefixes(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"Old/a.buf": {"hash": "aabbccdd"}}), encoding="utf-8"
    )
    raw = state_path.read_bytes()

    assert rewrite_blend_state_keys(store, mods, "", "New") == 0
    assert rewrite_blend_state_keys(store, mods, "Old", "Old") == 0

    assert state_path.read_bytes() == raw


def test_remove_blend_state_keys_exact_and_others_kept(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "Old/a.buf": {"hash": "aabbccdd", "stamp": 1},
                "Other/b.buf": {"hash": "11223344", "stamp": 2},
            }
        ),
        encoding="utf-8",
    )

    assert remove_blend_state_keys(store, mods, "Old") == 1

    expected = {"Other/b.buf": {"hash": "11223344", "stamp": 2}}
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected
    assert state_path.read_text(encoding="utf-8") == (
        json.dumps(expected, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )


def test_remove_blend_state_keys_category_prefix(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"Cat/x/y.buf": {"hash": "aabbccdd"}, "Keep/z.buf": {}}),
        encoding="utf-8",
    )

    assert remove_blend_state_keys(store, mods, "Cat") == 1

    rewritten = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(rewritten) == {"Keep/z.buf"}
    assert rewritten["Keep/z.buf"] == {}


def test_remove_blend_state_keys_no_marker_file_writes_nothing(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"

    assert remove_blend_state_keys(store, mods, "Old") == 0

    assert not blend_state_path(store, mods).exists()
    assert not store.exists()


def test_remove_blend_state_keys_no_match_leaves_file_untouched(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    store = tmp_path / "store"
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"Old/a.buf": {"hash": "aabbccdd"}}), encoding="utf-8"
    )
    raw = state_path.read_bytes()

    assert remove_blend_state_keys(store, mods, "Ghost") == 0

    assert state_path.read_bytes() == raw
