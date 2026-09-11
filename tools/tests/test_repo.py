"""Tests for tools.repo: variant table, cache dirs and character-dir detection."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import repo


def test_project_root_unfrozen_is_package_parent():
    file_path = repo.__file__
    assert file_path is not None
    assert repo.project_root() == Path(file_path).resolve().parent.parent


def test_project_root_frozen_resolves_next_to_executable(tmp_path, monkeypatch):
    exe = tmp_path / "ZZZHashFix.exe"
    monkeypatch.setitem(vars(sys), "frozen", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert repo.project_root() == tmp_path


def test_repo_variant_table():
    assert set(repo.REPO_VARIANTS) == {"2048p", "1024p"}
    assert repo.DEFAULT_VARIANT == "2048p"
    high = repo.REPO_VARIANTS["2048p"]
    low = repo.REPO_VARIANTS["1024p"]
    assert (high.key, high.repo_url, high.dir_name) == (
        "2048p",
        repo.REPO_URL,
        "zzz-model-hash",
    )
    assert (low.key, low.repo_url, low.dir_name) == (
        "1024p",
        repo.LOWVARM_REPO_URL,
        "zzz-model-hash_lowvarm",
    )
    assert repo.LOWVARM_REPO_URL.endswith("ZZZ-Model-Hash_LowVarm.git")


def test_default_cache_dir_per_variant():
    high = repo.default_cache_dir("2048p")
    low = repo.default_cache_dir("1024p")
    assert str(high).endswith(str(Path("data") / "zzz-model-hash"))
    assert str(low).endswith(str(Path("data") / "zzz-model-hash_lowvarm"))
    assert high.parent == low.parent
    assert high == repo.default_cache_dir()
    with pytest.raises(KeyError):
        repo.default_cache_dir("999p")


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


def test_ensure_repo_variant_url_selection(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run_git(args, check=True):
        assert check
        calls.append(args)
        return ""

    monkeypatch.setattr(repo, "_run_git", fake_run_git)

    assert repo.ensure_repo("1024p", cache_dir=tmp_path) == tmp_path
    assert calls == [
        ["clone", "--depth", "1", repo.LOWVARM_REPO_URL, str(tmp_path)]
    ]

    calls.clear()
    high = tmp_path / "high"
    assert repo.ensure_repo("2048p", cache_dir=high) == high
    assert calls == [["clone", "--depth", "1", repo.REPO_URL, str(high)]]


def test_repo_update_available_without_clone(tmp_path, monkeypatch):
    def fail(args, check=True):
        raise AssertionError(
            f"no git call expected without a local clone: {args}, check={check}"
        )

    monkeypatch.setattr(repo, "_run_git", fail)
    assert repo.repo_update_available("2048p", cache_dir=tmp_path) is True
    target = tmp_path / "partial"
    target.mkdir()
    assert repo.repo_update_available("2048p", cache_dir=target) is True


def test_repo_update_available_pinned_git_calls(tmp_path, monkeypatch):
    target = tmp_path / "clone"
    (target / ".git").mkdir(parents=True)
    calls: list[list[str]] = []
    canned = {"remote": "", "local": ""}

    def fake_run_git(args, check=True):
        calls.append(list(args))
        if "ls-remote" in args:
            return canned["remote"]
        assert not check
        return canned["local"]

    monkeypatch.setattr(repo, "_run_git", fake_run_git)

    canned["remote"] = "abc123def\tHEAD\n"
    canned["local"] = "abc123def\n"
    assert repo.repo_update_available("1024p", cache_dir=target) is False
    assert calls == [
        ["-C", str(target), "ls-remote", "origin", "HEAD"],
        ["-C", str(target), "rev-parse", "HEAD"],
    ]

    calls.clear()
    canned["remote"] = "def456abc\tHEAD\n"
    assert repo.repo_update_available("1024p", cache_dir=target) is True

    calls.clear()
    canned["remote"] = "ABC123DEF\tHEAD\n"
    canned["local"] = "abc123def\n"
    assert repo.repo_update_available("1024p", cache_dir=target) is False


def test_repo_update_available_unknown_when_check_fails(tmp_path, monkeypatch):
    target = tmp_path / "clone"
    (target / ".git").mkdir(parents=True)

    def raise_repo_error(args, check=True):
        raise repo.RepoError(f"offline: {args} (check={check})")

    monkeypatch.setattr(repo, "_run_git", raise_repo_error)
    assert repo.repo_update_available(cache_dir=target) is None

    monkeypatch.setattr(repo, "_run_git", lambda args, check=True: "")
    assert repo.repo_update_available(cache_dir=target) is None

    def empty_local(args, check=True):
        if "ls-remote" in args:
            assert check
            return "abc123def\tHEAD\n"
        assert not check
        return ""

    monkeypatch.setattr(repo, "_run_git", empty_local)
    assert repo.repo_update_available(cache_dir=target) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git executable not available")
def test_repo_update_available_real_git(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True
        )

    origin = tmp_path / "origin.git"
    git("init", "--bare", str(origin))
    seed = tmp_path / "seed"
    git("clone", str(origin), str(seed))
    (seed / "data.txt").write_text("one", encoding="utf-8")
    git("-C", str(seed), "add", ".")
    git(
        "-C", str(seed), "-c", "user.email=t@example", "-c", "user.name=t",
        "commit", "-m", "one",
    )
    git("-C", str(seed), "push", "origin", "HEAD")

    clone = tmp_path / "clone"
    git("clone", str(origin), str(clone))
    assert repo.repo_update_available(cache_dir=clone) is False

    (seed / "data.txt").write_text("two", encoding="utf-8")
    git("-C", str(seed), "add", ".")
    git(
        "-C", str(seed), "-c", "user.email=t@example", "-c", "user.name=t",
        "commit", "-m", "two",
    )
    git("-C", str(seed), "push", "origin", "HEAD")
    assert repo.repo_update_available(cache_dir=clone) is True
