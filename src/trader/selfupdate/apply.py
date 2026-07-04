"""guling-trader 自更新执行：下载新 exe → 校验 SHA256 → Windows 重命名自替换 → 拉起新进程 → 退出。"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import aiohttp

from .check import UpdateInfo
from ..installer.download import download_with_progress, verify_sha256

logger = logging.getLogger(__name__)

# Windows-only 常量；非 Windows 平台 subprocess 模块没有这两个属性，getattr 兜底避免 import 时炸
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

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


async def run_update(
    info: UpdateInfo,
    on_progress: Callable[[int, int], None],
    release_singleton_mutex: Callable[[], None],
) -> None:
    """执行一次完整的自更新。失败抛 SelfUpdateError，调用方负责捕获并反馈 UI。

    成功路径末尾调用 os._exit(0)——正常情况下这个协程不会返回。
    """
    exe_path = Path(sys.executable).resolve()
    if exe_path.name != EXPECTED_EXE_NAME:
        raise SelfUpdateError(
            f"当前可执行文件名 {exe_path.name!r} 与预期 {EXPECTED_EXE_NAME!r} 不符，"
            "可能是开发环境或打包方式变更，放弃自更新"
        )

    new_path = exe_path.parent / (EXPECTED_EXE_NAME + ".new")
    old_path = exe_path.parent / (EXPECTED_EXE_NAME + ".old")

    try:
        await download_with_progress(info.exe_url, new_path, on_progress=on_progress)

        async with aiohttp.ClientSession() as session:
            async with session.get(
                info.sha256_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise SelfUpdateError(f"下载 sha256 校验文件失败：HTTP {resp.status}")
                sha_text = await resp.text()
        expected_sha256 = _parse_sha256_file(sha_text)

        if not await verify_sha256(new_path, expected_sha256):
            # 校验不过 = 下到的字节不可信，删掉半成品，下次从头下载
            new_path.unlink(missing_ok=True)
            raise SelfUpdateError("下载文件 SHA256 校验不匹配，可能下载不完整或被篡改")

        _swap_files(exe_path, new_path, old_path)

    except SelfUpdateError:
        raise
    except Exception as e:
        # 下载/网络类失败：保留 .new 半成品，用户点"重试更新"时可断点续传，不从 0 重来。
        # （下次启动时 cleanup_orphan_files 会兜底清掉遗留的 .new，不会长期堆积。）
        raise SelfUpdateError(f"下载或替换过程出错：{e}") from e

    release_singleton_mutex()

    subprocess.Popen(
        [str(exe_path)],
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    os._exit(0)
