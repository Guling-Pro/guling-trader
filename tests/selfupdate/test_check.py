"""selfupdate.check 模块测试"""
from unittest.mock import patch

import pytest


class MockResponse:
    def __init__(self, status, json_data):
        self.status = status
        self._json_data = json_data

    async def json(self):
        return self._json_data

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


def _release_json(tag_name, has_exe=True, has_sha256=True):
    assets = []
    if has_exe:
        assets.append({
            "name": "guling-trader.exe",
            "browser_download_url": f"https://github.com/x/releases/download/{tag_name}/guling-trader.exe",
        })
    if has_sha256:
        assets.append({
            "name": "guling-trader.exe.sha256",
            "browser_download_url": f"https://github.com/x/releases/download/{tag_name}/guling-trader.exe.sha256",
        })
    return {"tag_name": tag_name, "assets": assets}


@pytest.mark.asyncio
async def test_new_version_available_returns_update_info():
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.6.0"))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is not None
    assert info.tag == "v0.6.0"
    assert info.current_version == "0.5.0"
    assert info.latest_version == "0.6.0"
    assert info.exe_url.endswith("guling-trader.exe")
    assert info.sha256_url.endswith("guling-trader.exe.sha256")


@pytest.mark.asyncio
async def test_same_version_returns_none():
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.5.0"))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_current_version_newer_returns_none():
    """防御性：当前版本比 Release 还新（比如本地跑的是未发布分支），不应回退提示"""
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.4.0"))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_missing_assets_returns_none():
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.6.0", has_sha256=False))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_non_200_status_returns_none():
    from trader.selfupdate import check

    mock_resp = MockResponse(403, {})  # 例如限流
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_network_exception_returns_none_not_raises():
    from trader.selfupdate import check

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", side_effect=RuntimeError("network down")):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_non_windows_skips_without_http_call():
    from trader.selfupdate import check

    def _boom(*args, **kwargs):
        raise AssertionError("非 Windows 不应发起 HTTP 请求")

    with patch("trader.selfupdate.check.platform.system", return_value="Darwin"), \
         patch("aiohttp.ClientSession", side_effect=_boom):
        info = await check.check_for_update("0.5.0")

    assert info is None


def test_version_tuple_ordering():
    from trader.selfupdate.check import _is_newer

    assert _is_newer("0.6.0", "0.5.0")
    assert not _is_newer("0.5.0", "0.5.0")
    assert not _is_newer("0.4.0", "0.5.0")
    assert _is_newer("1.0.0", "0.9.9")
