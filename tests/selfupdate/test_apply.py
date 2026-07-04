"""selfupdate.apply 模块测试"""
import os

import pytest


def test_parse_sha256_file_standard_format():
    from trader.selfupdate.apply import _parse_sha256_file

    assert _parse_sha256_file("deadbeef  guling-trader.exe\n") == "deadbeef"


def test_parse_sha256_file_single_space():
    from trader.selfupdate.apply import _parse_sha256_file

    assert _parse_sha256_file("deadbeef guling-trader.exe") == "deadbeef"


def test_parse_sha256_file_empty_raises():
    from trader.selfupdate.apply import _parse_sha256_file, SelfUpdateError

    with pytest.raises(SelfUpdateError):
        _parse_sha256_file("   \n")


def test_swap_files_success(tmp_path):
    from trader.selfupdate.apply import _swap_files

    exe = tmp_path / "guling-trader.exe"
    new = tmp_path / "guling-trader.exe.new"
    old = tmp_path / "guling-trader.exe.old"
    exe.write_text("old-content")
    new.write_text("new-content")

    _swap_files(exe, new, old)

    assert exe.read_text() == "new-content"
    assert old.read_text() == "old-content"
    assert not new.exists()


def test_swap_files_rollback_on_second_rename_failure(tmp_path, monkeypatch):
    from trader.selfupdate import apply

    exe = tmp_path / "guling-trader.exe"
    new = tmp_path / "guling-trader.exe.new"
    old = tmp_path / "guling-trader.exe.old"
    exe.write_text("old-content")
    new.write_text("new-content")

    real_rename = os.rename
    call_count = {"n": 0}

    def flaky_rename(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated failure")
        real_rename(src, dst)

    monkeypatch.setattr(apply.os, "rename", flaky_rename)

    with pytest.raises(OSError):
        apply._swap_files(exe, new, old)

    # 回滚：exe_path 内容恢复成旧的，old_path 不再单独存在
    assert exe.read_text() == "old-content"
    assert not old.exists()


def test_cleanup_orphan_files_removes_existing(tmp_path):
    from trader.selfupdate.apply import cleanup_orphan_files, EXPECTED_EXE_NAME

    old = tmp_path / (EXPECTED_EXE_NAME + ".old")
    new = tmp_path / (EXPECTED_EXE_NAME + ".new")
    old.write_text("x")
    new.write_text("y")

    cleanup_orphan_files(tmp_path)

    assert not old.exists()
    assert not new.exists()


def test_cleanup_orphan_files_noop_when_absent(tmp_path):
    from trader.selfupdate.apply import cleanup_orphan_files

    cleanup_orphan_files(tmp_path)  # 不应抛异常
