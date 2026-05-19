"""Xiadan.exe 检测：registry / process / shortcut / walking 四层 fallback"""
import logging
import platform
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 仅 Windows 平台可用
if platform.system() == "Windows":
    import psutil
    import winreg
    from win32com.client import GetObject


def find_via_registry() -> Optional[Path]:
    """
    从 Windows 注册表查找 xiadan.exe。
    扫描 HKLM / Wow6432Node / HKCU 的 Uninstall 目录，筛 DisplayName 含"同花顺"。
    """
    if platform.system() != "Windows":
        return None

    try:
        hives = [
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]

        for hkey, path in hives:
            try:
                key = winreg.OpenKey(hkey, path)
                idx = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, idx)
                        subkey = winreg.OpenKey(key, subkey_name)

                        try:
                            display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                            if "同花顺" in display_name:
                                # 尝试读 InstallLocation
                                try:
                                    install_loc = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                                    xiadan_path = Path(install_loc) / "xiadan.exe"
                                    if xiadan_path.exists():
                                        logger.info("registry: 检测到 xiadan at %s", xiadan_path)
                                        return xiadan_path
                                except winreg.error:
                                    pass
                        finally:
                            winreg.CloseKey(subkey)

                        idx += 1
                    except winreg.error:
                        break
                winreg.CloseKey(key)
            except winreg.error:
                continue
    except Exception as e:
        logger.debug("registry 检测出错：%s", e)

    return None


def find_via_process() -> Optional[Path]:
    """
    从运行中的进程找 xiadan.exe。
    检查是否有进程名含 xiadan，或进程名在 {hexin.exe, ths.exe} 并同目录有 xiadan。
    """
    if platform.system() != "Windows":
        return None

    try:
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = proc.info.get("name", "").lower()
                exe = proc.info.get("exe", "")

                if name == "xiadan.exe" and exe:
                    xiadan_path = Path(exe)
                    if xiadan_path.exists():
                        logger.info("process: 检测到 xiadan at %s", xiadan_path)
                        return xiadan_path

                # 如果是 hexin.exe 或 ths.exe，尝试找同目录 xiadan
                if name in {"hexin.exe", "ths.exe"} and exe:
                    exe_dir = Path(exe).parent
                    xiadan_path = exe_dir / "xiadan.exe"
                    if xiadan_path.exists():
                        logger.info("process (via hexin/ths): 检测到 xiadan at %s", xiadan_path)
                        return xiadan_path
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception) as e:
                logger.debug("扫进程出错：%s", e)
                continue
    except Exception as e:
        logger.debug("process 检测出错：%s", e)

    return None


def find_via_shortcut() -> Optional[Path]:
    r"""
    从快捷方式（.lnk）查找 xiadan。
    扫描 4 个 .lnk 目录：
    - APPDATA\Microsoft\Windows\Start Menu\Programs
    - PROGRAMDATA\Microsoft\Windows\Start Menu\Programs
    - Desktop
    - Public Desktop
    """
    if platform.system() != "Windows":
        return None

    try:
        import os
        from pathlib import Path

        shell = GetObject("new:13C4127B-542A-4280-AB1F-053910534456")

        lnk_dirs = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            Path(os.environ.get("USERPROFILE", "")) / "Desktop",
            Path(os.environ.get("PUBLIC", "")) / "Desktop" if "PUBLIC" in os.environ else None,
        ]

        for lnk_dir in [d for d in lnk_dirs if d]:
            if not lnk_dir.exists():
                continue

            for lnk_file in lnk_dir.rglob("*.lnk"):
                try:
                    shortcut = shell.CreateShortCut(str(lnk_file))
                    target = shortcut.TargetPath
                    if target and "同花顺" in str(target).lower():
                        target_dir = Path(target).parent
                        xiadan_path = target_dir / "xiadan.exe"
                        if xiadan_path.exists():
                            logger.info("shortcut: 检测到 xiadan at %s", xiadan_path)
                            return xiadan_path
                except Exception as e:
                    logger.debug("解析快捷方式出错：%s", e)
                    continue
    except Exception as e:
        logger.debug("shortcut 检测出错：%s", e)

    return None


def find_via_walking() -> Optional[Path]:
    """
    文件系统遍历（最后的 fallback）。
    扫描常见路径：PROGRAMFILES, PROGRAMFILES(X86), LOCALAPPDATA，限深度 2。
    以及 D:/ E:/ F: 根目录下的 *同花顺*/xiadan.exe（不限深度但中止频繁）。
    """
    if platform.system() != "Windows":
        return None

    try:
        import os

        search_bases = [
            Path(os.environ.get("PROGRAMFILES", "")),
            Path(os.environ.get("PROGRAMFILES(X86)", "")),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        ]

        for base in search_bases:
            if not base.exists():
                continue
            try:
                for xiadan_path in base.rglob("xiadan.exe"):
                    # 限深度：不超过 base 下 3 层
                    rel_parts = xiadan_path.relative_to(base).parts
                    if len(rel_parts) <= 3:
                        logger.info("walking (standard): 检测到 xiadan at %s", xiadan_path)
                        return xiadan_path
            except Exception as e:
                logger.debug("扫 %s 出错：%s", base, e)

        # 扫 D: E: F: 根目录，限深度更浅
        for drive in ["D", "E", "F"]:
            drive_root = Path(f"{drive}:/")
            if not drive_root.exists():
                continue
            try:
                for xiadan_path in drive_root.glob("*同花顺*"):
                    if xiadan_path.is_dir():
                        xiadan_exe = xiadan_path / "xiadan.exe"
                        if xiadan_exe.exists():
                            logger.info("walking (drive): 检测到 xiadan at %s", xiadan_exe)
                            return xiadan_exe
            except Exception as e:
                logger.debug("扫 %s: 出错：%s", drive, e)
    except Exception as e:
        logger.debug("walking 检测出错：%s", e)

    return None


def find_via_manual_path() -> Optional[Path]:
    """用户在 config.xiadan_path_manual 显式指定的路径——优先级最高"""
    try:
        from .. import config as trader_config

        cfg = trader_config.load()
        manual = getattr(cfg, "xiadan_path_manual", None)
        if manual:
            p = Path(manual)
            if p.exists() and p.is_file():
                return p
            logger.warning("config.xiadan_path_manual 指定路径不存在：%s", manual)
    except Exception as e:
        logger.debug("find_via_manual_path failed: %s", e)
    return None


def find_xiadan() -> Optional[Path]:
    """
    五层 fallback 查找 xiadan.exe。
    优先级：manual > registry > process > shortcut > walking
    """
    return (
        find_via_manual_path()
        or find_via_registry()
        or find_via_process()
        or find_via_shortcut()
        or find_via_walking()
    )
