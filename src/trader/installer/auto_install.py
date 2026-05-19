"""自动下载 + Silent install 同花顺到私有目录"""
import asyncio
import logging
import platform
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from . import detect, download, meta

if platform.system() == "Windows":
    import aiohttp

logger = logging.getLogger(__name__)

PRIVATE_INSTALL_DIR = Path("C:/guling-trader/同花顺")
DOWNLOAD_REDIRECT = "https://download.10jqka.com.cn/index/download/id/7/"
INSTALLER_TEMP = Path.home() / ".cache" / "guling-trader-installer"


class InstallerEventKind(Enum):
    """Installer 事件类型"""
    DOWNLOAD_PROGRESS = "download_progress"
    INSTALL_STARTED = "install_started"
    INSTALL_DONE = "install_done"
    DETECTED_EXISTING = "detected_existing"
    ERROR = "error"


@dataclass
class InstallerEvent:
    """Installer 事件"""
    kind: InstallerEventKind
    payload: dict  # kind 特定的数据


async def resolve_latest_version() -> Optional[str]:
    """
    从下载页面解析当前最新版本号（格式：9.50.90）。
    """
    try:
        real_url = await download.resolve_redirect(DOWNLOAD_REDIRECT)
        # 从 URL 提取版本号，如 THS_v9.50.90_*.exe
        match = re.search(r"THS_v([\d.]+)_", real_url)
        if match:
            version = match.group(1)
            logger.info("检测到最新 THS 版本：%s", version)
            return version
        else:
            logger.warning("无法从 URL 解析版本号：%s", real_url)
            return None
    except Exception as e:
        logger.error("解析最新版本失败：%s", e)
        return None


async def ensure_xiadan(
    on_event: Callable[[InstallerEvent], None]
) -> Optional[Path]:
    """
    确保 xiadan.exe 可用——纯检测，不自动下载。

    返回 None 时，调用方应该让用户：
    - 点「下载同花顺」按钮 → 看 README 手动装
    - 或点「指定路径...」→ 选 xiadan.exe 写到 config

    历史：早期版本 (v0.2.0-v0.2.6) 这里走 ensure_private_install 自动下载 214MB
    + silent install。实测 HTTP 403（CDN 屏蔽）+ 同花顺 EULA 不允许第三方分发，
    UX 反而比"手动装"更糟糕。v0.3.0 起改为纯检测，自动安装代码保留在文件下方
    但不再被调用——未来想换可靠源重启用时再串回来。
    """
    logger.info("开始 ensure_xiadan 流程（检测模式）")

    detected = detect.find_xiadan()
    if detected:
        logger.info("✓ 检测到 xiadan：%s", detected)
        on_event(InstallerEvent(
            kind=InstallerEventKind.DETECTED_EXISTING,
            payload={"detected_path": str(detected)},
        ))
        return detected

    logger.info("未检测到 xiadan——等用户在 UI 点「下载同花顺」或「指定路径」")
    return None


async def ensure_private_install(
    on_event: Callable[[InstallerEvent], None]
) -> None:
    """
    下载 + silent install 同花顺到私有目录。
    """
    if platform.system() != "Windows":
        raise RuntimeError("此功能仅支持 Windows")

    logger.info("开始私有安装流程")

    try:
        on_event(InstallerEvent(
            kind=InstallerEventKind.INSTALL_STARTED,
            payload={"message": "正在下载同花顺..."},
        ))

        # 1. 解析最新版本号和真实下载 URL
        latest_version = await resolve_latest_version()
        real_url = await download.resolve_redirect(DOWNLOAD_REDIRECT)

        # 2. 下载 installer
        INSTALLER_TEMP.mkdir(parents=True, exist_ok=True)
        installer_path = INSTALLER_TEMP / "THS-installer.exe"

        def on_progress(bytes_done: int, total: int):
            percent = int(100 * bytes_done / total) if total > 0 else 0
            speed_mb = bytes_done / (1024 * 1024)
            on_event(InstallerEvent(
                kind=InstallerEventKind.DOWNLOAD_PROGRESS,
                payload={
                    "percent": percent,
                    "bytes_done": bytes_done,
                    "total": total,
                    "speed_mb": f"{speed_mb:.1f}",
                },
            ))

        await download.download_with_progress(
            real_url,
            installer_path,
            on_progress=on_progress,
        )
        logger.info("下载完成：%s", installer_path)

        # 3. Silent install 到私有目录
        on_event(InstallerEvent(
            kind=InstallerEventKind.INSTALL_STARTED,
            payload={"message": "正在安装同花顺..."},
        ))

        PRIVATE_INSTALL_DIR.mkdir(parents=True, exist_ok=True)

        # Inno Setup 的 silent 参数
        # 注意：/DIR 不能加引号
        cmd = [
            str(installer_path),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={PRIVATE_INSTALL_DIR}",
            "/TASKS=!desktopicon,!quicklaunchicon",
        ]

        logger.info("执行 silent install：%s", cmd)
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("Silent install 完成（exit=%d）", result.returncode)

        # 4. 验证 xiadan.exe 生成
        xiadan_exe = PRIVATE_INSTALL_DIR / "xiadan.exe"
        if not xiadan_exe.exists():
            raise RuntimeError(
                f"安装完成但未在 {PRIVATE_INSTALL_DIR} 找到 xiadan.exe"
            )

        logger.info("✓ 安装验证通过：%s", xiadan_exe)

        # 5. 写 meta 文件
        meta.write_meta(installed_version=latest_version or "unknown")

        on_event(InstallerEvent(
            kind=InstallerEventKind.INSTALL_DONE,
            payload={"xiadan_path": str(xiadan_exe)},
        ))

    except subprocess.CalledProcessError as e:
        logger.error("Silent install 失败（exit=%d）：%s", e.returncode, e.stderr)
        on_event(InstallerEvent(
            kind=InstallerEventKind.ERROR,
            payload={"error": f"Silent install 失败：{e.stderr}"},
        ))
        raise
    except Exception as e:
        logger.error("私有安装出错：%s", e)
        on_event(InstallerEvent(
            kind=InstallerEventKind.ERROR,
            payload={"error": str(e)},
        ))
        raise


async def maybe_upgrade(
    on_event: Callable[[InstallerEvent], None]
) -> None:
    """
    启动期检测是否有新版 THS，需要时弹窗询问是否升级。
    """
    logger.info("检查升级")

    private_meta = meta.read_meta()
    if not private_meta:
        logger.info("未找到 meta 文件，跳过升级检查")
        return

    current_version = private_meta.get("installed_version")
    if not current_version:
        logger.info("meta 中无版本号，跳过升级检查")
        return

    try:
        latest_version = await resolve_latest_version()
        if not latest_version:
            logger.info("无法获取最新版本号，跳过升级检查")
            return

        if _version_greater(latest_version, current_version):
            logger.info(
                "检测到新版：%s → %s",
                current_version,
                latest_version,
            )
            on_event(InstallerEvent(
                kind=InstallerEventKind.DETECTED_EXISTING,
                payload={
                    "upgrade_available": True,
                    "current_version": current_version,
                    "latest_version": latest_version,
                },
            ))
            # 由上层 UI 决定是否继续升级
        else:
            logger.info("已是最新版本：%s", current_version)

    except Exception as e:
        logger.warning("升级检查出错：%s", e)


def _version_greater(v1: str, v2: str) -> bool:
    """
    简单的版本号比较（例如 "9.50.90" > "9.50.80"）。
    """
    try:
        parts1 = [int(x) for x in v1.split(".")]
        parts2 = [int(x) for x in v2.split(".")]
        return tuple(parts1) > tuple(parts2)
    except Exception:
        return False
