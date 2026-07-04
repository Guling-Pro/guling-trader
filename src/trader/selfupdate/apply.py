"""guling-trader 自更新执行：下载新 exe → 校验 SHA256 → Windows 重命名自替换 → 拉起新进程 → 退出。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

EXPECTED_EXE_NAME = "guling-trader.exe"


class SelfUpdateError(Exception):
    """自更新过程中的可恢复错误（下载/校验/替换失败），调用方捕获后走错误提示分支。"""


def _parse_sha256_file(content: str) -> str:
    """解析 sha256sum 格式：'<hash>  guling-trader.exe' → 取首个空白分隔字段。"""
    stripped = content.strip()
    if not stripped:
        raise SelfUpdateError("sha256 文件内容为空")
    return stripped.split()[0]


def _swap_files(exe_path: Path, new_path: Path, old_path: Path) -> None:
    """把 exe_path 换成 new_path 的内容：exe_path→old_path，new_path→exe_path。

    第二步失败时自动把 old_path 改回 exe_path（回滚），不留半成品；异常继续上抛给调用方。
    """
    os.rename(exe_path, old_path)
    try:
        os.rename(new_path, exe_path)
    except Exception:
        os.rename(old_path, exe_path)
        raise


def cleanup_orphan_files(exe_dir: Path) -> None:
    """启动时清理上一次更新可能留下的 .old/.new 孤儿文件（尽力而为，失败静默跳过）。"""
    for suffix in (".old", ".new"):
        path = exe_dir / (EXPECTED_EXE_NAME + suffix)
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("清理孤儿文件 %s 失败（下次再试）：%s", path, e)
