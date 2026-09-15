"""Tests for tools.repo: variant table, cache dirs, HTTP archive fetching."""

import io
import sys
import zipfile
from pathlib import Path

import pytest

from tools import repo


def test_project_root_unfrozen_is_package_parent():
    file_path = repo.__file__
    assert file_path is not None
    assert repo.project_root() == Path(file_path).resolve().parent.parent


def test_project_root_frozen_resolves_next_to_executable(tmp_path, monkeypatch):
    exe = tmp_path / "ZZZModKeeper.exe"
    monkeypatch.setitem(vars(sys), "frozen", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert repo.project_root() == tmp_path


def test_repo_variant_table():
    assert set(repo.REPO_VARIANTS) == {"2048p", "1024p", "fix_tool"}
    assert repo.DEFAULT_VARIANT == "2048p"
    assert repo.hash_variants() == ("2048p", "1024p")
    high = repo.REPO_VARIANTS["2048p"]
    low = repo.REPO_VARIANTS["1024p"]
    dump = repo.REPO_VARIANTS["fix_tool"]
    assert (
        high.key,
        high.repo_url,
        high.dir_name,
        high.repo_name,
        high.branch,
        high.kind,
    ) == ("2048p", repo.REPO_URL, "zzz-model-hash", "ZZZ-Model-Hash", "master", "hash")
    assert (
        low.key,
        low.repo_url,
        low.dir_name,
        low.repo_name,
        low.branch,
        low.kind,
    ) == (
        "1024p",
        repo.LOWVARM_REPO_URL,
        "zzz-model-hash_lowvarm",
        "ZZZ-Model-Hash_LowVarm",
        "main",
        "hash",
    )
    assert (
        dump.key,
        dump.repo_url,
        dump.dir_name,
        dump.repo_name,
        dump.branch,
        dump.kind,
    ) == (
        "fix_tool",
        repo.FIX_TOOL_REPO_URL,
        "zzz-model-fix-tool",
        "ZZZ-Model-Fix-Tool",
        "main",
        "dump",
    )
    assert repo.LOWVARM_REPO_URL.endswith("ZZZ-Model-Hash_LowVarm.git")
    assert repo.FIX_TOOL_REPO_URL.endswith("ZZZ-Model-Fix-Tool.git")


def test_default_cache_dir_per_variant():
    high = repo.default_cache_dir("2048p")
    low = repo.default_cache_dir("1024p")
    dump = repo.default_cache_dir("fix_tool")
    assert str(high).endswith(str(Path("data") / "zzz-model-hash"))
    assert str(low).endswith(str(Path("data") / "zzz-model-hash_lowvarm"))
    assert str(dump).endswith(str(Path("data") / "zzz-model-fix-tool"))
    assert high.parent == low.parent == dump.parent
    assert high == repo.default_cache_dir()
    with pytest.raises(KeyError):
        repo.default_cache_dir("999p")


def test_archive_url_fix_tool_points_at_main_branch():
    url = repo._archive_url("fix_tool")
    assert "codeload.github.com" in url
    assert "ZZZ-Model-Fix-Tool/zip/refs/heads/main" in url


def test_characters_dir_autodetect(tmp_path):
    standard = tmp_path / repo.CHARACTERS_DIR_NAME
    standard.mkdir()
    assert repo.characters_dir(tmp_path) == standard

    lowvarm_root = tmp_path / "lowvarm"
    lowvarm = lowvarm_root / repo.LOWVARM_CHARACTERS_DIR_NAME
    lowvarm.mkdir(parents=True)
    assert repo.characters_dir(lowvarm_root) == lowvarm

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        repo.characters_dir(empty)


def _archive_bytes(top: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(f"{top}/{name}", content)
    return buffer.getvalue()


def test_ensure_repo_fresh_download_populates_variant_cache(tmp_path, monkeypatch):
    calls: list[tuple[str, str | None]] = []

    def fake_fetch(url, timeout=repo._TIMEOUT_SECONDS):
        assert timeout == repo._TIMEOUT_SECONDS
        calls.append((str(url), None))
        return _archive_bytes(
            "ZZZ-Model-Hash-master",
            {repo.CHANGELOG_NAME: b"logs", "角色hash表/a.json": b"{}"},
        ), '"etag-v1"'

    monkeypatch.setattr(repo, "_fetch_url", fake_fetch)
    monkeypatch.setattr(repo, "_head_sha", lambda variant: "abc123def")

    high = tmp_path / "high"
    assert repo.ensure_repo("2048p", cache_dir=high) == high
    assert (high / repo.CHANGELOG_NAME).read_bytes() == b"logs"
    assert (high / "角色hash表" / "a.json").read_bytes() == b"{}"
    assert repo.repo_head(high) == "abc123def"
    assert repo._read_marker(high).get("etag") == '"etag-v1"'
    assert len(calls) == 1
    assert "codeload.github.com" in calls[0][0]
    assert "ZZZ-Model-Hash" in calls[0][0]
    assert "refs/heads/master" in calls[0][0]
    assert not list(tmp_path.glob("*-staging-*"))


def test_ensure_repo_up_to_date_skips_download(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    repo._write_marker(target, etag="etag-v1")
    (target / repo.CHANGELOG_NAME).write_text("x", encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(repo, "_head_etag", lambda url: "etag-v1")

    def fail_fetch(url, timeout=repo._TIMEOUT_SECONDS):
        raise AssertionError(
            f"no download expected when up to date: {url} (timeout={timeout})"
        )

    monkeypatch.setattr(repo, "_fetch_url", fail_fetch)
    assert repo.ensure_repo("2048p", cache_dir=target) == target
    assert calls == []


def test_ensure_repo_refreshes_when_etag_changes(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    repo._write_marker(target, etag="etag-v1")
    (target / repo.CHANGELOG_NAME).write_text("old", encoding="utf-8")
    downloaded: list[tuple[str, str]] = []

    monkeypatch.setattr(repo, "_head_etag", lambda url: "etag-v2")

    def fake_download(variant, target_path):
        downloaded.append((variant, str(target_path)))
        (target_path / repo.CHANGELOG_NAME).write_text("new", encoding="utf-8")
        repo._write_marker(target_path, etag="etag-v2", sha="def456")

    monkeypatch.setattr(repo, "_download_archive", fake_download)
    assert repo.ensure_repo("1024p", cache_dir=target) == target
    assert downloaded == [("1024p", str(target))]
    assert (target / repo.CHANGELOG_NAME).read_text(encoding="utf-8") == "new"
    assert repo.repo_head(target) == "def456"


def test_ensure_repo_keeps_existing_when_refresh_fails(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    repo._write_marker(target, etag="etag-v1")
    (target / repo.CHANGELOG_NAME).write_text("x", encoding="utf-8")
    logs: list[str] = []

    def fail_etag(url):
        raise repo.RepoError(f"offline: {url}")

    monkeypatch.setattr(repo, "_head_etag", fail_etag)
    assert repo.ensure_repo("2048p", cache_dir=target, log=logs.append) == target
    assert (target / repo.CHANGELOG_NAME).read_text(encoding="utf-8") == "x"
    assert any("keeping existing copy" in line for line in logs)


def test_ensure_repo_fresh_download_failure_raises_and_cleans(tmp_path, monkeypatch):
    target = tmp_path / "cache"

    def fail_fetch(url, timeout=repo._TIMEOUT_SECONDS):
        raise repo.RepoError(f"offline: {url} (timeout={timeout})")

    monkeypatch.setattr(repo, "_fetch_url", fail_fetch)
    with pytest.raises(repo.RepoError):
        repo.ensure_repo("2048p", cache_dir=target)
    assert not target.exists()
    assert not list(tmp_path.glob("*-staging-*"))


def test_repo_update_available_without_marker(tmp_path, monkeypatch):
    def fail(url):
        raise AssertionError(f"no network call expected: {url}")

    monkeypatch.setattr(repo, "_head_etag", fail)
    assert repo.repo_update_available("2048p", cache_dir=tmp_path) is True
    partial = tmp_path / "partial"
    partial.mkdir()
    assert repo.repo_update_available("2048p", cache_dir=partial) is True


def test_repo_update_available_pins_etag_calls(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    repo._write_marker(target, etag="etag-v1")
    calls: list[str] = []

    def fake_etag(url):
        calls.append(str(url))
        return current

    monkeypatch.setattr(repo, "_head_etag", fake_etag)
    current = "etag-v1"
    assert repo.repo_update_available("1024p", cache_dir=target) is False
    assert len(calls) == 1

    current = "etag-v2"
    assert repo.repo_update_available("1024p", cache_dir=target) is True

    calls.clear()
    assert repo.repo_update_available("1024p", cache_dir=target) is True
    assert len(calls) == 1
    assert "ZZZ-Model-Hash_LowVarm" in calls[0]
    assert "refs/heads/main" in calls[0]


def test_repo_update_available_unknown_when_check_fails(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    repo._write_marker(target, etag="etag-v1")

    monkeypatch.setattr(repo, "_head_etag", lambda url: _raise(repo.RepoError("offline")))
    assert repo.repo_update_available(cache_dir=target) is None

    monkeypatch.setattr(repo, "_head_etag", lambda url: None)
    assert repo.repo_update_available(cache_dir=target) is None

    repo._write_marker(target)
    monkeypatch.setattr(repo, "_head_etag", lambda url: "etag-v1")
    assert repo.repo_update_available(cache_dir=target) is True


def test_extract_archive_drops_top_folder(tmp_path):
    archive = _archive_bytes(
        "ZZZ-Model-Hash-master",
        {
            "Hash变动日志.txt": b"logs",
            "角色hash表/a.json": b"{}",
            "角色hash表/子目录/b.ini": b"ini",
        },
    )
    destination = tmp_path / "out"
    repo._extract_archive(archive, destination)
    assert (destination / "Hash变动日志.txt").read_bytes() == b"logs"
    assert (destination / "角色hash表" / "a.json").read_bytes() == b"{}"
    assert (
        destination / "角色hash表" / "子目录" / "b.ini"
    ).read_bytes() == b"ini"
    assert not (destination / "ZZZ-Model-Hash-master").exists()


def test_extract_archive_rejects_escaping_members(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("ZZZ-Model-Hash-master/../../evil.txt", b"boom")
    with pytest.raises(repo.RepoError, match="escapes"):
        repo._extract_archive(buffer.getvalue(), tmp_path / "out")


def test_extract_archive_prunes_subfolder(tmp_path):
    archive = _archive_bytes(
        "ZZZ-Model-Fix-Tool-main",
        {
            "版本修复工具/dump/珂蕾妲-脸/珂蕾妲-脸.json": b"{}",
            "版本修复工具/dump/说明.txt": b"readme",
            "版本修复工具/zzz_fix.中文版.exe": b"exe",
            "RabbitFX修复工具/other.exe": b"junk",
        },
    )
    destination = tmp_path / "out"
    repo._extract_archive(archive, destination, subfolder="版本修复工具/dump")
    assert (destination / "珂蕾妲-脸" / "珂蕾妲-脸.json").read_bytes() == b"{}"
    assert (destination / "说明.txt").read_bytes() == b"readme"
    assert not (destination / "版本修复工具").exists()
    assert not any(member.suffix == ".exe" for member in destination.rglob("*"))


def test_is_index_dump_member_matches_only_ib_txt_files():
    assert repo._is_index_dump_member("珂蕾妲-身体/DialynFaceA-ib=9a9780a7.txt")
    assert repo._is_index_dump_member("DialynFaceA-ib=9a9780a7.txt")
    assert not repo._is_index_dump_member("珂蕾妲-身体/DialynFaceA-vb0=c44d2531.txt")
    assert not repo._is_index_dump_member("珂蕾妲-身体/DialynFaceA.json")
    assert not repo._is_index_dump_member("说明.txt")


def test_extract_archive_exclude_skips_v2_index_dumps(tmp_path):
    archive = _archive_bytes(
        "ZZZ_Index_Vertex_Fix_Tool_v2-main",
        {
            "ZZZ_Index_Vertex_Fix_Tool_v2/Dump/珂蕾妲-身体/"
            "DialynFaceA-ib=9a9780a7.txt": b"index",
            "ZZZ_Index_Vertex_Fix_Tool_v2/Dump/珂蕾妲-身体/"
            "DialynFaceA-vb0=c44d2531.txt": b"vertex",
            "ZZZ_Index_Vertex_Fix_Tool_v2/Dump/珂蕾妲-身体/DialynFaceA.json": b"{}",
        },
    )
    destination = tmp_path / "out"
    repo._extract_archive(
        archive,
        destination,
        subfolder=repo._V2_DUMP_SUBFOLDER,
        exclude=repo._is_index_dump_member,
    )
    assert not (
        destination / "珂蕾妲-身体" / "DialynFaceA-ib=9a9780a7.txt"
    ).exists()
    assert (
        destination / "珂蕾妲-身体" / "DialynFaceA-vb0=c44d2531.txt"
    ).read_bytes() == b"vertex"
    assert (destination / "珂蕾妲-身体" / "DialynFaceA.json").read_bytes() == b"{}"


def test_ensure_repo_fix_tool_extracts_only_dump_subfolder(tmp_path, monkeypatch):
    def fake_fetch(url, timeout=repo._TIMEOUT_SECONDS):
        assert "ZZZ-Model-Fix-Tool" in str(url)
        assert "refs/heads/main" in str(url)
        assert timeout == repo._TIMEOUT_SECONDS
        return _archive_bytes(
            "ZZZ-Model-Fix-Tool-main",
            {
                "版本修复工具/dump/扳机-脸/扳机-脸.json": b"{}",
                "版本修复工具/zzz_fix.中文版.exe": b"exe",
                "版本修复工具/依赖包勿删/junk.dll": b"dll",
            },
        ), '"etag-dump"'

    monkeypatch.setattr(repo, "_fetch_url", fake_fetch)
    monkeypatch.setattr(repo, "_head_sha", lambda variant: "feedbeef")

    target = tmp_path / "dump"
    assert repo.ensure_repo("fix_tool", cache_dir=target) == target
    assert (target / "扳机-脸" / "扳机-脸.json").read_bytes() == b"{}"
    assert not (target / "版本修复工具").exists()
    assert repo.repo_head(target) == "feedbeef"
    assert repo._read_marker(target).get("etag") == '"etag-dump"'
    assert not list(tmp_path.glob("*-staging-*"))


def test_ensure_repo_fix_tool_extracts_v2_dump_subfolder(tmp_path, monkeypatch):
    def fake_fetch(url, timeout=repo._TIMEOUT_SECONDS):
        assert "ZZZ-Model-Fix-Tool" in str(url)
        assert "refs/heads/main" in str(url)
        assert timeout == repo._TIMEOUT_SECONDS
        return _archive_bytes(
            "ZZZ-Model-Fix-Tool-main",
            {
                "版本修复工具/dump/扳机-脸/扳机-脸.json": b"{}",
                "版本修复工具/zzz_fix.中文版.exe": b"exe",
                "ZZZ_Index_Vertex_Fix_Tool_v2/Dump/珂蕾妲-身体/"
                "DialynFaceA-vb0=c44d2531.txt": b"vertex",
                "ZZZ_Index_Vertex_Fix_Tool_v2/Dump/珂蕾妲-身体/"
                "DialynFaceA-ib=9a9780a7.txt": b"index",
            },
        ), '"etag-dump-v2"'

    monkeypatch.setattr(repo, "_fetch_url", fake_fetch)
    monkeypatch.setattr(repo, "_head_sha", lambda variant: "feedbeef")

    target = tmp_path / "dump"
    assert repo.ensure_repo("fix_tool", cache_dir=target) == target
    assert (target / "扳机-脸" / "扳机-脸.json").read_bytes() == b"{}"
    v2_dump = target / "v2" / "珂蕾妲-身体" / "DialynFaceA-vb0=c44d2531.txt"
    assert v2_dump.read_bytes() == b"vertex"
    assert not (target / "v2" / "ZZZ_Index_Vertex_Fix_Tool_v2").exists()
    assert not any("-ib=" in member.name for member in target.rglob("*"))
    assert not (target / "版本修复工具").exists()
    assert repo._read_marker(target).get("etag") == '"etag-dump-v2"'
    assert not list(tmp_path.glob("*-staging-*"))


def test_repo_head_defaults_to_empty(tmp_path):
    assert repo.repo_head(tmp_path) == ""


def _raise(exc):
    raise exc