"""Tesseract OCR 自动安装（winget 静默）。

下单验证码的自动识别依赖 Tesseract。Win11 自带 winget，UB-Mannheim 包已含
eng 语言数据（验证码是字母数字，只需 eng），所以"自动安装"一步到位，无需另外
下载模型。

设计要点：
- 不靠 winget 退出码判断成败（"已安装"也可能返回非 0），一律以安装后重新检测为准。
- UB-Mannheim 默认装到 ``C:\\Program Files\\Tesseract-OCR``，即使当前进程 PATH
  没刷新，detect 也能按固定路径命中。
- 任何失败都优雅降级为 None + 日志，绝不抛断启动。
"""
from __future__ import annotations

import asyncio
import logging
import platform
import shutil
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WINGET_PACKAGE_ID = "UB-Mannheim.TesseractOCR"

TESSERACT_COMMON_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def detect_tesseract() -> Optional[str]:
    """查找 tesseract。

    返回：
    - ``""``  → 在 PATH 上（pytesseract 用默认命令名即可）
    - 具体路径 → 在固定安装目录命中
    - ``None`` → 未安装
    """
    try:
        from .. import config

        manual = config.load().tesseract_path_manual
        if manual and Path(manual).is_file():
            return manual
    except Exception:
        logger.debug("读取手动 Tesseract 路径失败", exc_info=True)

    if shutil.which("tesseract"):
        return ""
    for p in TESSERACT_COMMON_PATHS:
        if Path(p).exists():
            return p
    return None


def verify_ocr_runnable() -> tuple[bool, str]:
    """启动自检：真跑一次 ``tesseract --version``，确认 OCR 可用（而非只是文件在）。

    捕获"装坏 / 缺 DLL / 路径没接通"等只会在下单那一刻才暴露的问题。
    需在 ``win.setup()`` 之后调用（那时 pytesseract.tesseract_cmd 已配好）。
    返回 ``(ok, info)``：ok=True 时 info 是版本号；否则 info 是错误原因。
    """
    try:
        import pytesseract
        return True, str(pytesseract.get_tesseract_version())
    except Exception as e:
        return False, str(e)


async def ensure_tesseract(
    on_log: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """确保 Tesseract 可用：已装→返回 ``""``/路径；未装→winget 静默安装后重检测。

    返回 ``None`` 表示不可用（非 Windows / 无 winget / 安装失败）。
    """
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    if platform.system() != "Windows":
        return None

    existing = detect_tesseract()
    if existing is not None:
        return existing

    if not shutil.which("winget"):
        log("⚠ 未找到 winget，无法自动安装 Tesseract。请选择本地 tesseract.exe，"
            f"或手动运行：winget install -e --id {WINGET_PACKAGE_ID}")
        return None

    log("正在通过 winget 后台尝试安装 Tesseract OCR；不影响连接和窗口检测...")
    cmd = [
        "winget", "install", "-e", "--id", WINGET_PACKAGE_ID,
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    rc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        rc = proc.returncode
        out = (stdout or b"").decode("utf-8", errors="ignore")
        logger.info("winget 退出码=%s\n%s", rc, out[-1500:])
    except Exception as e:
        logger.exception("winget 安装 Tesseract 失败")
        log(f"⚠ Tesseract 自动安装失败：{e}")
        return None

    # 重试检测：winget 可能装到固定目录但 PATH 还没刷新，或安装收尾稍慢。
    for _ in range(5):
        found = detect_tesseract()
        if found is not None:
            log("✓ Tesseract OCR 安装完成")
            return found
        await asyncio.sleep(1.5)

    unsigned_rc = (rc or 0) & 0xFFFFFFFF
    code = f"0x{unsigned_rc:08X}"
    if "InternetOpenUrl() failed" in out or unsigned_rc == 0x80072EE2:
        reason = "下载 GitHub 安装包超时，请检查虚拟机网络，或选择本地 tesseract.exe"
    elif unsigned_rc == 0xC000013A:
        reason = "安装进程已中断"
    else:
        reason = "winget 未完成安装"
    log(f"⚠ Tesseract 自动安装失败（{code}）：{reason}。"
        "OCR 已跳过，不影响连接和交易窗口检测。")
    return None
