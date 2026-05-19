"""Installer auto_install 模块测试"""
import platform
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


@pytest.mark.skipif(platform.system() != "Windows", reason="需要 Windows")
@pytest.mark.asyncio
async def test_resolve_latest_version():
    """测试从下载页解析版本号"""
    from trader.installer import auto_install

    # Mock resolve_redirect 返回一个包含版本号的 URL
    with patch("trader.installer.auto_install.download.resolve_redirect") as mock_redirect:
        mock_redirect.return_value = "https://sp.thsi.cn/THS_v9.50.90_build123.exe"

        version = await auto_install.resolve_latest_version()
        assert version == "9.50.90"


@pytest.mark.skipif(platform.system() != "Windows", reason="需要 Windows")
@pytest.mark.asyncio
async def test_resolve_latest_version_fallback():
    """版本号解析失败的 fallback"""
    from trader.installer import auto_install

    with patch("trader.installer.auto_install.download.resolve_redirect") as mock_redirect:
        mock_redirect.return_value = "https://sp.thsi.cn/some_random_url"

        version = await auto_install.resolve_latest_version()
        assert version is None


@pytest.mark.skipif(platform.system() != "Windows", reason="需要 Windows")
@pytest.mark.asyncio
async def test_ensure_xiadan_private_exists():
    """私有目录已有 xiadan 时直接返回"""
    from trader.installer import auto_install

    xiadan_exe = auto_install.PRIVATE_INSTALL_DIR / "xiadan.exe"

    with patch("pathlib.Path.exists") as mock_exists:
        mock_exists.return_value = True

        on_event = Mock()
        result = await auto_install.ensure_xiadan(on_event=on_event)

        assert result == xiadan_exe
        # 不应该触发任何事件
        assert not on_event.called


@pytest.mark.skipif(platform.system() != "Windows", reason="需要 Windows")
@pytest.mark.asyncio
async def test_ensure_xiadan_detected_existing():
    """检测到已有 xiadan 时触发事件"""
    from trader.installer import auto_install

    detected_path = Path("C:/Program Files/Hexin/xiadan.exe")

    with patch("pathlib.Path.exists") as mock_exists, \
         patch("trader.installer.auto_install.detect.find_xiadan") as mock_find:

        # 私有不存在，但检测到已有的
        mock_exists.return_value = False
        mock_find.return_value = detected_path

        on_event = Mock()
        result = await auto_install.ensure_xiadan(on_event=on_event)

        # 应该返回检测到的位置
        assert result == detected_path

        # 应该触发 DETECTED_EXISTING 事件
        on_event.assert_called()
        event = on_event.call_args[0][0]
        assert event.kind == auto_install.InstallerEventKind.DETECTED_EXISTING


@pytest.mark.skipif(platform.system() != "Windows", reason="需要 Windows")
def test_version_comparison():
    """版本号比较"""
    from trader.installer import auto_install

    assert auto_install._version_greater("9.50.90", "9.50.80")
    assert auto_install._version_greater("10.0.0", "9.99.99")
    assert not auto_install._version_greater("9.50.80", "9.50.90")
    assert not auto_install._version_greater("9.50.90", "9.50.90")


@pytest.mark.skipif(platform.system() != "Windows", reason="需要 Windows")
@pytest.mark.asyncio
async def test_ensure_private_install_flow():
    """Private install 流程（mock 版）"""
    from trader.installer import auto_install

    with patch("trader.installer.auto_install.resolve_latest_version") as mock_version, \
         patch("trader.installer.auto_install.download.resolve_redirect") as mock_redirect, \
         patch("trader.installer.auto_install.download.download_with_progress") as mock_download, \
         patch("subprocess.run") as mock_subprocess, \
         patch("pathlib.Path.mkdir"), \
         patch("pathlib.Path.exists") as mock_exists:

        mock_version.return_value = "9.50.90"
        mock_redirect.return_value = "https://sp.thsi.cn/THS_v9.50.90_build.exe"
        mock_download.return_value = Path("/tmp/THS-installer.exe")
        mock_subprocess.return_value = Mock(returncode=0)

        # 模拟 xiadan.exe 最终存在
        def exists_side_effect(p):
            return str(p).endswith("xiadan.exe")

        mock_exists.side_effect = exists_side_effect

        on_event = Mock()
        await auto_install.ensure_private_install(on_event=on_event)

        # 验证 silent install 被调用
        mock_subprocess.assert_called()
        cmd = mock_subprocess.call_args[0][0]
        assert "/VERYSILENT" in cmd
        assert "/SUPPRESSMSGBOXES" in cmd
        assert "/NORESTART" in cmd

        # 验证事件被触发
        assert on_event.call_count >= 2  # INSTALL_STARTED 和 INSTALL_DONE
