"""Installer download 模块测试"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


@pytest.mark.asyncio
async def test_resolve_redirect_with_location():
    """测试重定向解析"""
    from trader.installer import download

    class MockResponse:
        def __init__(self):
            self.status = 302
            self.headers = {"Location": "https://sp.thsi.cn/THS_v9.50.90_build.exe"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockSession:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def head(self, *args, **kwargs):
            return self.resp

    mock_resp = MockResponse()
    mock_session = MockSession(mock_resp)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download.resolve_redirect("https://download.10jqka.com.cn/index/download/id/7/")
        assert result == "https://sp.thsi.cn/THS_v9.50.90_build.exe"


@pytest.mark.asyncio
async def test_resolve_redirect_no_redirect():
    """测试无重定向情况"""
    from trader.installer import download

    class MockResponse:
        def __init__(self):
            self.status = 200
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockSession:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def head(self, *args, **kwargs):
            return self.resp

    mock_resp = MockResponse()
    mock_session = MockSession(mock_resp)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download.resolve_redirect("https://example.com/file.exe")
        assert result == "https://example.com/file.exe"


@pytest.mark.asyncio
async def test_download_with_progress():
    """测试下载进度回调"""
    from trader.installer import download

    dest = Path("/tmp/test-download.exe")
    progress_calls = []

    def on_progress(done, total):
        progress_calls.append((done, total))

    class MockResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Length": "100"}
            self.content = self

        async def iter_chunked(self, size):
            yield b"x" * 50
            yield b"x" * 50

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockSession:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def get(self, *args, **kwargs):
            return self.resp

    mock_resp = MockResponse()
    mock_session = MockSession(mock_resp)

    with patch("aiohttp.ClientSession", return_value=mock_session), \
         patch("builtins.open", create=True) as mock_open:

        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file

        result = await download.download_with_progress(
            "https://example.com/file.exe",
            dest,
            on_progress=on_progress,
        )

        # 验证进度回调被调用
        assert len(progress_calls) > 0

        # 验证目标路径被返回
        assert result == dest


@pytest.mark.asyncio
async def test_verify_sha256_match():
    """测试 SHA256 验证（匹配）"""
    from trader.installer import download

    test_file = Path("/tmp/test.exe")

    with patch("builtins.open", create=True) as mock_open, \
         patch("hashlib.sha256") as mock_hash:

        mock_file = MagicMock()
        mock_file.read.return_value = b""
        mock_open.return_value.__enter__.return_value = mock_file
        mock_open.return_value.__exit__.return_value = None

        mock_hash_obj = MagicMock()
        mock_hash_obj.hexdigest.return_value = "abc123"
        mock_hash.return_value = mock_hash_obj

        result = await download.verify_sha256(
            test_file,
            "abc123",
        )

        assert result is True


@pytest.mark.asyncio
async def test_verify_sha256_mismatch():
    """测试 SHA256 验证（不匹配）"""
    from trader.installer import download

    test_file = Path("/tmp/test.exe")

    with patch("builtins.open", create=True) as mock_open, \
         patch("hashlib.sha256") as mock_hash:

        mock_file = MagicMock()
        mock_file.read.return_value = b""
        mock_open.return_value.__enter__.return_value = mock_file
        mock_open.return_value.__exit__.return_value = None

        mock_hash_obj = MagicMock()
        mock_hash_obj.hexdigest.return_value = "abc123"
        mock_hash.return_value = mock_hash_obj

        result = await download.verify_sha256(
            test_file,
            "wrong_hash",
        )

        assert result is False


@pytest.mark.asyncio
async def test_download_retries_then_succeeds(tmp_path, monkeypatch):
    """首次尝试网络失败 → 自动重试 → 第二次成功（验证重试逻辑，不再一抖动就整体失败）"""
    from trader.installer import download

    dest = tmp_path / "f.exe"
    progress = []

    class GoodResp:
        status = 200
        headers = {"Content-Length": "4"}

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            yield b"abcd"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class FailSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, *a, **kw):
            raise ConnectionError("boom")

    class GoodSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, *a, **kw):
            return GoodResp()

    sessions = iter([FailSession(), GoodSession()])
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: next(sessions))
    monkeypatch.setattr(download.asyncio, "sleep", AsyncMock())  # 别真等退避

    result = await download.download_with_progress(
        "https://example.com/f.exe", dest, on_progress=lambda d, t: progress.append((d, t))
    )

    assert result == dest
    assert dest.read_bytes() == b"abcd"
    assert progress[-1] == (4, 4)


@pytest.mark.asyncio
async def test_download_resumes_with_range_header(tmp_path, monkeypatch):
    """已存在半成品 → 带 Range 头续传 → 拼接完整（验证断点续传，不从 0 重来）"""
    from trader.installer import download

    dest = tmp_path / "f.exe"
    dest.write_bytes(b"AB")  # 上次已下 2 字节

    captured = {}

    class Resp206:
        status = 206
        headers = {"Content-Length": "2"}  # 剩余 2 字节

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            yield b"CD"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, url, headers=None, timeout=None):
            captured["headers"] = headers or {}
            return Resp206()

    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **kw: Session())
    monkeypatch.setattr(download.asyncio, "sleep", AsyncMock())

    result = await download.download_with_progress("https://example.com/f.exe", dest)

    assert captured["headers"].get("Range") == "bytes=2-"
    assert dest.read_bytes() == b"ABCD"  # 续传拼接,未从头覆盖
