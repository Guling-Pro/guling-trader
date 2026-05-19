"""首次启动：生成 device_id、自动查找 xiadan.exe 和 tesseract、installer 流程"""
import asyncio
import logging
import platform
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import config
from .installer import auto_install

if platform.system() == "Windows":
    import win32api
    import win32con
    import win32gui
    import win32process

logger = logging.getLogger(__name__)

DEFAULT_XIADAN_WINDOW_TITLE = "网上股票交易系统5.0"

TESSERACT_COMMON_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


@dataclass
class BootstrapResult:
    found_xiadan_path: Optional[str]
    found_tesseract_cmd: Optional[str]
    config: config.TraderConfig
    errors: list[str]


def _detect_xiadan_window(prefix: str) -> Optional[tuple[str, str, int]]:
    """查找第一个运行的 xiadan.exe 窗口，返回 (全标题, exe 路径, hwnd)"""
    matches: list[tuple[str, str, int]] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.startswith(prefix):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                pid,
            )
            try:
                path = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                win32api.CloseHandle(handle)
            if "xiadan" in path.lower():
                matches.append((title, path, hwnd))
        except Exception as e:
            logger.debug("检查窗口 hwnd=%s 出错：%s", hwnd, e)

    win32gui.EnumWindows(cb, None)
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "检测到多个 xiadan 窗口，使用第一个：%s",
            [t for t, _, _ in matches],
        )
    return matches[0]


def _detect_tesseract_path() -> Optional[str]:
    """查找 tesseract，返回路径或 None"""
    if shutil.which("tesseract"):
        return ""
    for p in TESSERACT_COMMON_PATHS:
        if Path(p).exists():
            return p
    return None


def bootstrap() -> BootstrapResult:
    """首次启动逻辑：生成 device_id、查找 xiadan 和 tesseract"""
    errors: list[str] = []

    cfg = config.load()

    if not cfg.device_id:
        cfg.device_id = str(uuid.uuid4())
        config.save(cfg)
        logger.info("已生成新 device_id：%s", cfg.device_id)

    xiadan_path: Optional[str] = None
    try:
        if platform.system() == "Windows":
            detected = _detect_xiadan_window(DEFAULT_XIADAN_WINDOW_TITLE)
            if detected:
                _, exe_path, _ = detected
                xiadan_path = exe_path
            else:
                errors.append("未检测到运行中的 xiadan.exe（需要打开同花顺并登录）")
        else:
            errors.append("此工具仅支持 Windows（wine 用户请查看 --diagnose 模式）")
    except Exception as e:
        errors.append(f"查找 xiadan 出错：{e}")

    tesseract_cmd: Optional[str] = None
    try:
        if platform.system() == "Windows":
            tesseract_cmd = _detect_tesseract_path()
            if tesseract_cmd is None:
                errors.append(
                    "未找到 Tesseract OCR（winget install UB-Mannheim.TesseractOCR）"
                )
    except Exception as e:
        errors.append(f"查找 Tesseract 出错：{e}")

    return BootstrapResult(
        found_xiadan_path=xiadan_path,
        found_tesseract_cmd=tesseract_cmd,
        config=cfg,
        errors=errors,
    )


async def ensure_xiadan_async(
    on_event: Callable[[auto_install.InstallerEvent], None],
) -> Optional[Path]:
    """
    异步 ensure_xiadan 流程：检测 / 下载 / 安装。
    返回 xiadan.exe 路径，或 None 如果用户取消。
    """
    if platform.system() != "Windows":
        logger.warning("ensure_xiadan 仅支持 Windows")
        return None

    try:
        xiadan_path = await auto_install.ensure_xiadan(on_event=on_event)
        return xiadan_path
    except Exception as e:
        logger.error("ensure_xiadan 出错：%s", e)
        on_event(auto_install.InstallerEvent(
            kind=auto_install.InstallerEventKind.ERROR,
            payload={"error": str(e)},
        ))
        return None


async def maybe_upgrade_async(
    on_event: Callable[[auto_install.InstallerEvent], None],
) -> None:
    """异步升级检查"""
    if platform.system() != "Windows":
        return

    try:
        await auto_install.maybe_upgrade(on_event=on_event)
    except Exception as e:
        logger.error("升级检查出错：%s", e)
