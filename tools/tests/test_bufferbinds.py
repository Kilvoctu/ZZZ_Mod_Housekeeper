"""Tests for tools.bufferbinds: collecting a mod ini's buffer binds."""

from tools.bufferbinds import collect_buffer_binds

MAIN_INI = "\r\n".join(
    [
        "[TextureOverrideBody]",
        "hash = AAAA0001",
        "hash = bbbb0002",
        "vb0 = ResourceBodyPosition",
        "vb1 = ResourceBodyTexcoord",
        "vb2 = ResourceBodyBlend",
        "ib = ResourceBodyIndex",
        "",
        "[ResourceBodyPosition]",
        "type = Buffer",
        "stride = 48",
        "filename = .\\bufs\\p.buf",
        "",
        "[ResourceBodyTexcoord]",
        "type = Buffer",
        "stride = 36",
        "filename = t.buf",
        "",
        "[ResourceBodyBlend]",
        "type = Buffer",
        "stride = 32",
        "filename = b.buf",
        "",
        "[ResourceBodyIndex]",
        "type = Buffer",
        "filename = ib.buf",
    ]
) + "\r\n"


def test_collect_buffer_binds_collects_every_slot(tmp_path):
    (tmp_path / "bufs").mkdir()
    (tmp_path / "bufs" / "p.buf").write_bytes(b"\x00")
    for name in ("t.buf", "b.buf", "ib.buf"):
        (tmp_path / name).write_bytes(b"\x00")
    ini_path = tmp_path / "m.ini"
    ini_path.write_bytes(MAIN_INI.encode("utf-8"))

    binds = collect_buffer_binds(MAIN_INI, ini_path)

    assert [(bind.hash, bind.slot, bind.resource) for bind in binds] == [
        ("aaaa0001", "vb0", "ResourceBodyPosition"),
        ("aaaa0001", "vb1", "ResourceBodyTexcoord"),
        ("aaaa0001", "vb2", "ResourceBodyBlend"),
        ("aaaa0001", "ib", "ResourceBodyIndex"),
        ("bbbb0002", "vb0", "ResourceBodyPosition"),
        ("bbbb0002", "vb1", "ResourceBodyTexcoord"),
        ("bbbb0002", "vb2", "ResourceBodyBlend"),
        ("bbbb0002", "ib", "ResourceBodyIndex"),
    ]
    by_slot = {bind.slot: bind for bind in binds if bind.hash == "aaaa0001"}
    position = by_slot["vb0"]
    assert (position.stride, position.filename) == (48, ".\\bufs\\p.buf")
    assert position.path == tmp_path / "bufs" / "p.buf"
    assert position.exists is True
    assert (by_slot["vb1"].stride, by_slot["vb1"].exists) == (36, True)
    assert (by_slot["vb2"].stride, by_slot["vb2"].exists) == (32, True)
    index = by_slot["ib"]
    assert (index.stride, index.filename, index.exists) == (None, "ib.buf", True)


NO_FILENAME_INI = "\r\n".join(
    [
        "[TextureOverrideBody]",
        "hash = aaaa0001",
        "vb1 = ResourceNoFilename",
        "",
        "[ResourceNoFilename]",
        "type = Buffer",
        "stride = 36",
    ]
) + "\r\n"


def test_collect_buffer_binds_without_filename(tmp_path):
    ini_path = tmp_path / "m.ini"
    ini_path.write_bytes(NO_FILENAME_INI.encode("utf-8"))

    binds = collect_buffer_binds(NO_FILENAME_INI, ini_path)

    assert len(binds) == 1
    assert (binds[0].filename, binds[0].path, binds[0].exists) == ("", None, False)


ABSENT_INI = "\r\n".join(
    [
        "[TextureOverrideBody]",
        "hash = aaaa0001",
        "vb0 = ResourceGone",
        "",
        "[ResourceGone]",
        "type = Buffer",
        "stride = 48",
        "filename = gone.buf",
    ]
) + "\r\n"


def test_collect_buffer_binds_reports_absent_file(tmp_path):
    ini_path = tmp_path / "m.ini"
    ini_path.write_bytes(ABSENT_INI.encode("utf-8"))

    binds = collect_buffer_binds(ABSENT_INI, ini_path)

    assert len(binds) == 1
    assert binds[0].path == tmp_path / "gone.buf"
    assert binds[0].exists is False


SUFFIX_INI = "\r\n".join(
    [
        "[TextureOverrideBody]",
        "hash = aaaa0001",
        "vb1 = ResourceName.2",
        "",
        "[ResourceName]",
        "type = Buffer",
        "stride = 36",
        "filename = a.buf",
    ]
) + "\r\n"


def test_collect_buffer_binds_variant_suffix_matches_base_block(tmp_path):
    ini_path = tmp_path / "m.ini"
    ini_path.write_bytes(SUFFIX_INI.encode("utf-8"))
    (tmp_path / "a.buf").write_bytes(b"\x00")

    binds = collect_buffer_binds(SUFFIX_INI, ini_path)

    assert len(binds) == 1
    bind = binds[0]
    assert bind.resource == "ResourceName.2"
    assert (bind.stride, bind.filename, bind.path, bind.exists) == (
        36,
        "a.buf",
        tmp_path / "a.buf",
        True,
    )


IGNORED_INI = "\r\n".join(
    [
        "[ShaderOverrideS]",
        "hash = aaaa0001",
        "vb1 = ResourceS",
        "",
        "[Constants]",
        "hash = bbbb0002",
        "vb0 = ResourceC",
        "",
        "[TextureOverrideBody]",
        "hash = cccc0003",
        "vb0 = ResourceNotBuffer",
        "",
        "[ResourceS]",
        "type = Buffer",
        "stride = 36",
        "filename = s.buf",
        "",
        "[ResourceC]",
        "type = Buffer",
        "filename = c.buf",
        "",
        "[ResourceNotBuffer]",
        "stride = 48",
        "filename = n.buf",
    ]
) + "\r\n"


def test_collect_buffer_binds_ignores_non_buffer_and_other_sections(tmp_path):
    ini_path = tmp_path / "m.ini"
    ini_path.write_bytes(IGNORED_INI.encode("utf-8"))

    assert collect_buffer_binds(IGNORED_INI, ini_path) == []


DUP_INI = "\r\n".join(
    [
        "[TextureOverrideOne]",
        "hash = aaaa0001",
        "hash = aaaa0001",
        "vb1 = ResourceDup",
        "vb1 = ResourceDup",
        "",
        "[TextureOverrideTwo]",
        "hash = aaaa0001",
        "vb1 = ResourceDup",
        "",
        "[ResourceDup]",
        "type = Buffer",
        "stride = 36",
        "filename = d.buf",
    ]
) + "\r\n"


def test_collect_buffer_binds_dedupes_repeated_triples(tmp_path):
    ini_path = tmp_path / "m.ini"
    ini_path.write_bytes(DUP_INI.encode("utf-8"))

    binds = collect_buffer_binds(DUP_INI, ini_path)

    assert [(bind.hash, bind.slot, bind.resource) for bind in binds] == [
        ("aaaa0001", "vb1", "ResourceDup")
    ]
