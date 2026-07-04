"""selfupdate.apply 模块测试"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_run_update_success(tmp_path, monkeypatch):
    from trader.selfupdate import apply
    from trader.selfupdate.check import UpdateInfo

    exe_path = tmp_path / "guling-trader.exe"
    exe_path.write_text("old-binary")
    monkeypatch.setattr(apply.sys, "executable", str(exe_path))

    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    async def fake_download(url, dest, on_progress=None, chunk_size=65536):
        Path(dest).write_text("new-binary")
        if on_progress:
            on_progress(10, 10)
        return Path(dest)

    async def fake_verify(file_path, expected):
        assert expected == "deadbeef"
        return True

    class MockShaResp:
        status = 200

        async def text(self):
            return "deadbeef  guling-trader.exe\n"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class MockShaSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, *a, **kw):
            return MockShaResp()

    monkeypatch.setattr(apply, "download_with_progress", fake_download)
    monkeypatch.setattr(apply, "verify_sha256", fake_verify)
    monkeypatch.setattr(apply.aiohttp, "ClientSession", lambda: MockShaSession())

    popen_calls = []
    monkeypatch.setattr(apply.subprocess, "Popen", lambda *a, **kw: popen_calls.append((a, kw)))
    exit_calls = []
    monkeypatch.setattr(apply.os, "_exit", lambda code: exit_calls.append(code))

    release_calls = []
    progress_calls = []

    await apply.run_update(
        info,
        on_progress=lambda d, t: progress_calls.append((d, t)),
        release_singleton_mutex=lambda: release_calls.append(True),
    )

    assert exe_path.read_text() == "new-binary"
    assert (tmp_path / "guling-trader.exe.old").read_text() == "old-binary"
    assert not (tmp_path / "guling-trader.exe.new").exists()
    assert release_calls == [True]
    assert len(popen_calls) == 1
    assert exit_calls == [0]
    assert progress_calls == [(10, 10)]


@pytest.mark.asyncio
async def test_run_update_sha256_mismatch_leaves_exe_untouched(tmp_path, monkeypatch):
    from trader.selfupdate import apply
    from trader.selfupdate.check import UpdateInfo

    exe_path = tmp_path / "guling-trader.exe"
    exe_path.write_text("old-binary")
    monkeypatch.setattr(apply.sys, "executable", str(exe_path))

    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    async def fake_download(url, dest, on_progress=None, chunk_size=65536):
        Path(dest).write_text("new-binary")
        return Path(dest)

    async def fake_verify(file_path, expected):
        return False  # 校验失败

    class MockShaResp:
        status = 200

        async def text(self):
            return "deadbeef  guling-trader.exe\n"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class MockShaSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, *a, **kw):
            return MockShaResp()

    monkeypatch.setattr(apply, "download_with_progress", fake_download)
    monkeypatch.setattr(apply, "verify_sha256", fake_verify)
    monkeypatch.setattr(apply.aiohttp, "ClientSession", lambda: MockShaSession())

    with pytest.raises(apply.SelfUpdateError, match="SHA256"):
        await apply.run_update(
            info, on_progress=lambda d, t: None, release_singleton_mutex=lambda: None
        )

    assert exe_path.read_text() == "old-binary"
    assert not (tmp_path / "guling-trader.exe.new").exists()
    assert not (tmp_path / "guling-trader.exe.old").exists()


@pytest.mark.asyncio
async def test_run_update_wrong_exe_name_aborts_before_download(tmp_path, monkeypatch):
    from trader.selfupdate import apply
    from trader.selfupdate.check import UpdateInfo

    wrong_exe = tmp_path / "python.exe"
    wrong_exe.write_text("dev-env")
    monkeypatch.setattr(apply.sys, "executable", str(wrong_exe))

    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    download_calls = []

    async def fake_download(*a, **kw):
        download_calls.append(True)

    monkeypatch.setattr(apply, "download_with_progress", fake_download)

    with pytest.raises(apply.SelfUpdateError, match="guling-trader.exe"):
        await apply.run_update(
            info, on_progress=lambda d, t: None, release_singleton_mutex=lambda: None
        )

    assert download_calls == []
