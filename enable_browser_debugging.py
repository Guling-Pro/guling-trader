# -*- coding: utf-8 -*-

"""
guling-trader Browser RPA Debugging Enabler
一键修复 Windows 桌面 Edge/Chrome 快捷方式，静默开启 9222 调试端口，实现零登录阻碍。
"""

import os
import sys
import platform
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger(__name__)

def patch_windows_shortcuts():
    """扫描 Windows 桌面，一键为 Edge 和 Chrome 的快捷方式 (.lnk) 注入调试端口参数"""
    try:
        import win32com.client
    except ImportError:
        logger.error("未检测到 pywin32 库，请先在当前虚拟环境中运行: pip install pywin32")
        return

    # 1. 扫描当前用户桌面及公共桌面
    user_profile = os.environ.get("USERPROFILE", "")
    public_desktop = os.environ.get("PUBLIC", r"C:\Users\Public")
    
    desktops = []
    if user_profile:
        desktops.append(os.path.join(user_profile, "Desktop"))
    if public_desktop:
        desktops.append(os.path.join(public_desktop, "Desktop"))

    target_shortcuts = ["Microsoft Edge.lnk", "Google Chrome.lnk"]
    shell = win32com.client.Dispatch("WScript.Shell")
    patched_count = 0

    logger.info("开始扫描 Windows 桌面快捷方式...")

    for desktop in desktops:
        if not os.path.exists(desktop):
            continue
        
        for item in target_shortcuts:
            shortcut_path = os.path.join(desktop, item)
            if not os.path.exists(shortcut_path):
                continue

            logger.info("判定命中快捷方式: %s", shortcut_path)
            try:
                shortcut = shell.CreateShortCut(shortcut_path)
                target = shortcut.TargetPath
                args = shortcut.Arguments or ""

                # 检查是否已经注入了该调试端口参数
                if "--remote-debugging-port=9222" not in args:
                    new_args = f"{args} --remote-debugging-port=9222".strip()
                    shortcut.Arguments = new_args
                    shortcut.Save()
                    logger.info("✓ 成功修复快捷方式: %s -> 已追加参数 '%s'", item, new_args)
                    patched_count += 1
                else:
                    logger.info("[-] 快捷方式 %s 之前已修复过，无需重复操作", item)
            except Exception as e:
                logger.error("✗ 修复快捷方式 %s 失败: %s", item, e)

    if patched_count > 0:
        logger.info("=" * 60)
        logger.info(" 恭喜！一键修复成功！🎉 ")
        logger.info("=" * 60)
        logger.info(" 提示: 请关闭当前所有已打开的 Edge/Chrome 窗口，并使用刚才修复的桌面快捷方式重新打开浏览器。")
        logger.info(" 之后 guling-trader 将能够无缝、零二次登录地直接接管您的日常网页发帖！")
        logger.info("=" * 60)
    else:
        logger.warning("未能在您的桌面上找到 Microsoft Edge 或 Google Chrome 的快捷方式。")
        logger.warning("请确认您的桌面上确实存在这两个浏览器的官方快捷图标。")


def show_mac_instructions():
    """macOS 端的终端别名与快捷配置指引"""
    logger.info("=" * 70)
    logger.info(" macOS 端快捷配置指南 ")
    logger.info("=" * 70)
    logger.info("由于 macOS 采用沙箱 App 机制，无法直接修改桌面快捷方式，但您可以通过在终端配置别名 (Alias) 一键启动：")
    logger.info("")
    logger.info("  * 对于 Microsoft Edge (推荐):")
    logger.info('    open -a "Microsoft Edge" --args --remote-debugging-port=9222')
    logger.info("")
    logger.info("  * 对于 Google Chrome:")
    logger.info('    open -a "Google Chrome" --args --remote-debugging-port=9222')
    logger.info("")
    logger.info("提示: 建议将其加入您的 ~/.zshrc 中：")
    logger.info('  alias edge-debug=\'open -a "Microsoft Edge" --args --remote-debugging-port=9222\'')
    logger.info("=" * 70)


def main():
    sys_type = platform.system()
    if sys_type == "Windows":
        patch_windows_shortcuts()
    elif sys_type == "Darwin":
        show_mac_instructions()
    else:
        logger.error("该一键配置工具目前仅支持 Windows 和 macOS 平台。")


if __name__ == "__main__":
    main()
