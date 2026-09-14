"""Tests for tools.importer: safe mod archive extraction for importing."""

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from tools import importer


def build_archive(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return path


def archive_with(exe: str, path: Path, members: dict[str, str]) -> Path:
    """Create a real rar/7z archive (plain files) via an external tool."""
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


def make_7z(path: Path, members: dict[str, str]) -> Path:
    """Build a real .7z archive, skipping when 7-Zip is not installed."""
    exe = importer.find_7z()
    if exe is None:
        pytest.skip("7-Zip not installed")
    return archive_with(exe, path, members)


def make_rar(path: Path, members: dict[str, str]) -> Path:
    """Build a real .rar archive, skipping when WinRAR's Rar.exe is missing."""
    exe = importer.find_rar_tool()
    if exe is None or Path(exe).name.lower() != "rar.exe":
        pytest.skip("WinRAR Rar.exe not installed")
    return archive_with(exe, path, members)


def test_nested_single_top_lands_in_destination(tmp_path):
    destination = tmp_path / "mods"
    archive = build_archive(
        tmp_path / "MyMod.zip",
        {"MyMod/ini/a.ini": "a", "MyMod/b.buf": "b", "MyMod/": ""},
    )

    count = importer.extract_zip(archive, destination)

    assert count == 2
    assert (destination / "MyMod" / "ini" / "a.ini").read_text() == "a"
    assert (destination / "MyMod" / "b.buf").read_text() == "b"
    assert (destination / "MyMod" / "ini").is_dir()


def test_flat_archive_wrapped_under_stem(tmp_path):
    destination = tmp_path / "mods"
    archive = build_archive(
        tmp_path / "CoolPack.zip", {"a.ini": "a", "sub/b.buf": "b"}
    )

    count = importer.extract_zip(archive, destination)

    assert count == 2
    assert (destination / "CoolPack" / "a.ini").read_text() == "a"
    assert (destination / "CoolPack" / "sub" / "b.buf").read_text() == "b"


def test_skip_conflict_excludes_and_logs(tmp_path):
    destination = tmp_path / "mods"
    expected = destination / "MyMod" / "a.ini"
    expected.parent.mkdir(parents=True)
    expected.write_text("original")
    archive = build_archive(
        tmp_path / "MyMod.zip", {"MyMod/a.ini": "new", "MyMod/b.buf": "b"}
    )
    log: list[str] = []

    count = importer.extract_zip(archive, destination, log=log.append)

    assert count == 1
    assert expected.read_text() == "original"
    assert len(log) == 1
    assert "skip existing" in log[0]
    assert (destination / "MyMod" / "b.buf").read_text() == "b"


def test_replace_overwrites_pre_created_file(tmp_path):
    destination = tmp_path / "mods"
    expected = destination / "MyMod" / "a.ini"
    expected.parent.mkdir(parents=True)
    expected.write_text("original")
    archive = build_archive(tmp_path / "MyMod.zip", {"MyMod/a.ini": "new"})

    count = importer.extract_zip(archive, destination, replace=True)

    assert count == 1
    assert expected.read_text() == "new"


def test_unsafe_members_dropped(tmp_path):
    destination = tmp_path / "mods"
    archive = build_archive(
        tmp_path / "pack.zip",
        {
            "../evil.txt": "e",
            "/abs.txt": "a",
            "C:/drive.txt": "c",
            "MyMod/ok.ini": "o",
        },
    )

    count = importer.extract_zip(archive, destination)

    assert count == 1
    assert (destination / "MyMod" / "ok.ini").read_text() == "o"
    for path in destination.rglob("*"):
        assert path.name not in {"evil.txt", "abs.txt", "drive.txt"}


def test_junk_members_dropped(tmp_path):
    destination = tmp_path / "mods"
    archive = build_archive(
        tmp_path / "pack.zip",
        {"__MACOSX/xyz": "x", "MyMod/.DS_Store": "d", "MyMod/ok.ini": "o"},
    )

    count = importer.extract_zip(archive, destination)

    assert count == 1
    assert (destination / "MyMod" / "ok.ini").read_text() == "o"
    for path in destination.rglob("*"):
        assert "__MACOSX" not in path.parts
        assert path.name != ".DS_Store"


def test_empty_archive_returns_zero(tmp_path):
    destination = tmp_path / "mods"
    junk_only = build_archive(
        tmp_path / "junkonly.zip", {"__MACOSX/x": "", ".DS_Store": ""}
    )
    dir_only = build_archive(tmp_path / "dironly.zip", {"MyMod/": ""})

    assert importer.extract_zip(junk_only, destination) == 0
    assert not destination.exists()
    assert importer.extract_zip(dir_only, destination) == 0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("MyMod\\sub\\a.ini", "MyMod/sub/a.ini"),
        ("MyMod/./sub//a.ini", "MyMod/sub/a.ini"),
        ("MyMod/", "MyMod"),
        (".", None),
        ("", None),
        ("/", None),
        ("/abs.txt", None),
        ("C:/drive.txt", None),
        ("C:\\drive.txt", None),
        ("../up.txt", None),
        ("a/../b.txt", None),
    ],
)
def test_clean_member_normalizes_and_rejects(name: str, expected: str | None):
    assert importer.clean_member(name) == expected


def test_extract_archive_dispatches_zip(tmp_path):
    destination = tmp_path / "mods"
    archive = build_archive(
        tmp_path / "MyMod.zip", {"MyMod/a.ini": "a", "MyMod/b.buf": "b"}
    )

    count = importer.extract_archive(archive, destination)

    assert count == 2
    assert (destination / "MyMod" / "a.ini").read_text() == "a"
    assert (destination / "MyMod" / "b.buf").read_text() == "b"


def test_extract_archive_7z_roundtrip(tmp_path):
    destination = tmp_path / "mods"
    archive = make_7z(tmp_path / "MyMod.7z", {"MyMod/a.ini": "a", "MyMod/b.buf": "b"})

    count = importer.extract_archive(archive, destination)

    assert count == 2
    assert (destination / "MyMod" / "a.ini").read_text() == "a"
    assert (destination / "MyMod" / "b.buf").read_text() == "b"


def test_extract_archive_rar_roundtrip(tmp_path):
    destination = tmp_path / "mods"
    archive = make_rar(tmp_path / "MyMod.rar", {"MyMod/a.ini": "a", "MyMod/b.buf": "b"})

    count = importer.extract_archive(archive, destination)

    assert count == 2
    assert (destination / "MyMod" / "a.ini").read_text() == "a"
    assert (destination / "MyMod" / "b.buf").read_text() == "b"


def test_extract_archive_flat_7z_wrapped_under_stem(tmp_path):
    destination = tmp_path / "mods"
    archive = make_7z(tmp_path / "CoolPack.7z", {"a.ini": "a", "sub/b.buf": "b"})

    count = importer.extract_archive(archive, destination)

    assert count == 2
    assert (destination / "CoolPack" / "a.ini").read_text() == "a"
    assert (destination / "CoolPack" / "sub" / "b.buf").read_text() == "b"


def test_extract_archive_7z_skips_then_replaces(tmp_path):
    destination = tmp_path / "mods"
    expected = destination / "MyMod" / "a.ini"
    expected.parent.mkdir(parents=True)
    expected.write_text("original")
    archive = make_7z(tmp_path / "MyMod.7z", {"MyMod/a.ini": "new"})

    assert importer.extract_archive(archive, destination) == 0
    assert expected.read_text() == "original"

    assert importer.extract_archive(archive, destination, replace=True) == 1
    assert expected.read_text() == "new"


def test_extract_archive_7z_drops_junk(tmp_path):
    destination = tmp_path / "mods"
    archive = make_7z(
        tmp_path / "pack.7z",
        {"__MACOSX/_x": "x", "MyMod/.DS_Store": "d", "MyMod/ok.ini": "o"},
    )

    count = importer.extract_archive(archive, destination)

    assert count == 1
    assert (destination / "MyMod" / "ok.ini").read_text() == "o"
    for path in destination.rglob("*"):
        assert "__MACOSX" not in path.parts
        assert path.name != ".DS_Store"


def test_extract_archive_junk_only_7z_returns_zero(tmp_path):
    destination = tmp_path / "mods"
    archive = make_7z(tmp_path / "junkonly.7z", {"__MACOSX/x": "", ".DS_Store": ""})

    assert importer.extract_archive(archive, destination) == 0
    assert not destination.exists()


def test_extract_archive_unsupported_suffix_raises(tmp_path):
    destination = tmp_path / "mods"
    archive = tmp_path / "pack.rar7"
    archive.write_text("not an archive")

    with pytest.raises(ValueError, match="unsupported archive type"):
        importer.extract_archive(archive, destination)


def test_extract_archive_missing_tool_raises(tmp_path, monkeypatch):
    destination = tmp_path / "mods"
    archive = make_7z(tmp_path / "MyMod.7z", {"MyMod/a.ini": "a"})
    monkeypatch.setattr(importer, "_find_extractor", lambda: None)

    with pytest.raises(RuntimeError, match="7-Zip or WinRAR"):
        importer.extract_archive(archive, destination)
