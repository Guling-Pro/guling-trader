"""启动入口：bootstrap → 自动安装 xiadan → tray + ws_client 协程"""
import argparse
import asyncio
import logging
import os
import platform
import sys
from typing import Optional

from . import bootstrap, ws_client, tray, ui_dialogs
from .installer import auto_install
from .ths.win import WinThsBackend

if platform.system() == "Windows":
    import psutil

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _on_installer_event(event: auto_install.InstallerEvent) -> None:
    """处理 installer 事件"""
    logger.info("Installer event: kind=%s payload=%s", event.kind, event.payload)
    # 这里可以用 tray_manager 更新状态，暂时只记日志


async def _main_async(
    bootstrap_result: bootstrap.BootstrapResult,
    tray_manager: tray.TrayManager,
) -> None:
    """异步主循环：xiadan ensure → ws_client 连接"""
    # Step 1: 确保 xiadan 可用（检测 / 下载 / 安装）
    xiadan_path = await bootstrap.ensure_xiadan_async(
        on_event=_on_installer_event
    )

    if not xiadan_path:
        logger.warning("无法获取 xiadan，继续启动（功能受限）")
    else:
        logger.info("✓ xiadan 已就绪：%s", xiadan_path)
        bootstrap_result.found_xiadan_path = str(xiadan_path)

    # Step 2: 检查升级
    await bootstrap.maybe_upgrade_async(on_event=_on_installer_event)

    # Step 3: 启动 xiadan 前检查冲突
    if platform.system() == "Windows" and bootstrap_result.found_xiadan_path:
        _check_xiadan_conflict()

    # Step 4: WS 连接
    client = ws_client.WsClient(
        dev_url=os.environ.get("YU_TRADER_DEV_URL"),
        on_state_change=lambda state: tray_manager.set_state(state),
    )
    await client.run()


def _check_xiadan_conflict() -> None:
    """检查是否有手动启动的 xiadan 进程"""
    if platform.system() != "Windows":
        return

    try:
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name", "").lower()
                if name in {"xiadan.exe", "hexin.exe", "ths.exe"}:
                    logger.warning(
                        "检测到运行中的同花顺进程：%s，请先关闭再启动 trader",
                        name,
                    )
                    import tkinter as tk
                    root = tk.Tk()
                    root.withdraw()
                    ui_dialogs.messagebox.showwarning(
                        "冲突检测",
                        "检测到运行中的同花顺进程，请先关闭再启动 trader，以避免操作冲突。",
                    )
                    root.destroy()
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        logger.warning("冲突检测出错：%s", e)


async def _diagnose() -> None:
    """诊断模式：验证环境是否可用"""
    print("\n=== 诊断报告 ===\n")

    result = bootstrap.bootstrap()

    print(f"设备 ID: {result.config.device_id}")
    print()

    if result.errors:
        print("⚠ 启动警告：")
        for err in result.errors:
            print(f"  - {err}")
        print()

    if result.found_xiadan_path:
        print(f"✓ xiadan.exe 找到：{result.found_xiadan_path}")
    else:
        print("✗ xiadan.exe 未找到（需要打开同花顺）")

    if result.found_tesseract_cmd is not None:
        print(f"✓ Tesseract 找到：{result.found_tesseract_cmd or 'PATH'}")
    else:
        print("✗ Tesseract 未找到（可选，OCR 功能需要）")

    print()
    print("=== 测试只读操作 ===\n")

    backend = WinThsBackend()
    backend.set_tesseract_cmd(result.found_tesseract_cmd or "")

    try:
        diagnose_result = await backend.diagnose()
        for key, value in diagnose_result.items():
            print(f"{key}: {value}")
    except Exception as e:
        print(f"✗ 诊断异常: {e}")

    print()
    print("=== 诊断完成 ===\n")
    print("提示：wine 用户请参考 README.md 中的兼容性说明")
    print()


def run() -> None:
    """主启动函数"""
    parser = argparse.ArgumentParser(description="guling-trader — Windows 交易终端")
    parser.add_argument("--diagnose", action="store_true", help="诊断模式：验证环境")
    args = parser.parse_args()

    if args.diagnose:
        try:
            asyncio.run(_diagnose())
        except Exception as e:
            logger.exception("诊断失败：%s", e)
            sys.exit(1)
        return

    try:
        result = bootstrap.bootstrap()

        if result.errors:
            logger.warning("启动警告：")
            for err in result.errors:
                logger.warning("  - %s", err)

        logger.info("设备 ID：%s", result.config.device_id)
        logger.info("已配对：%s", result.config.has_paired())

        if result.found_xiadan_path:
            logger.info("已找到 xiadan：%s", result.found_xiadan_path)
        else:
            logger.warning("未找到 xiadan.exe，交易功能将不可用")

        if result.found_tesseract_cmd is not None:
            logger.info("已找到 Tesseract：%s", result.found_tesseract_cmd or "PATH")
        else:
            logger.warning("未找到 Tesseract，OCR 功能将不可用")

        def on_show_pairing_code(code: str, expires: int) -> None:
            """显示配对码弹窗"""
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            dialog = ui_dialogs.PairingCodeDialog(root, code, expires)
            dialog.show()
            root.mainloop()

        def on_show_status(state: str, account: str, last_seen: str) -> None:
            """显示状态窗"""
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            status_win = ui_dialogs.StatusWindow(root)
            status_win.show(state, account, last_seen)
            root.mainloop()

        def on_exit() -> None:
            """退出应用"""
            sys.exit(0)

        tray_config = tray.TrayConfig(
            xiadan_path=result.found_xiadan_path,
            on_show_pairing_code=on_show_pairing_code,
            on_exit=on_exit,
            on_show_status=on_show_status,
        )
        tray_mgr = tray.TrayManager(tray_config)
        tray_mgr.start()

        asyncio.run(_main_async(result, tray_mgr))

    except KeyboardInterrupt:
        logger.info("已中断")
        sys.exit(0)
    except Exception as e:
        logger.exception("致命错误：%s", e)
        sys.exit(1)
