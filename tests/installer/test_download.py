"""Installer download 模块测试"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


@pytest.mark.asyncio
async def test_resolve_redirect_with_location():
    """测试重定向解析"""
    from trader.installer import download

    mock_resp = AsyncMock()
    mock_resp.status = 302
    mock_resp.headers = {"Location": "https://sp.thsi.cn/THS_v9.50.90_build.exe"}

    mock_session = AsyncMock()
    mock_session.head.return_value.__aenter__.return_value = mock_resp
    mock_session.head.return_value.__aexit__.return_value = None

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await download.resolve_redirect("https://download.10jqka.com.cn/index/download/id/7/")
        assert result == "https://sp.thsi.cn/THS_v9.50.90_build.exe"


@pytest.mark.asyncio
async def test_resolve_redirect_no_redirect():
    """测试无重定向情况"""
    from trader.installer import download

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {}

    mock_session = AsyncMock()
    mock_session.head.return_value.__aenter__.return_value = mock_resp
    mock_session.head.return_value.__aexit__.return_value = None

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

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Length": "100"}

    async def mock_iter_chunked(size):
        yield b"x" * 50
        yield b"x" * 50

    mock_resp.content = AsyncMock()
    mock_resp.content.iter_chunked = mock_iter_chunked

    mock_session = AsyncMock()
    mock_session.get.return_value.__aenter__.return_value = mock_resp
    mock_session.get.return_value.__aexit__.return_value = None

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

    with patch("pathlib.Path.open", create=True) as mock_open, \
         patch("hashlib.sha256") as mock_hash:

        mock_file = MagicMock()
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

    with patch("pathlib.Path.open", create=True), \
         patch("hashlib.sha256") as mock_hash:

        mock_hash_obj = MagicMock()
        mock_hash_obj.hexdigest.return_value = "abc123"
        mock_hash.return_value = mock_hash_obj

        result = await download.verify_sha256(
            test_file,
            "wrong_hash",
        )

        assert result is False
