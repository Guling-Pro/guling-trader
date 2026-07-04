"""guling-trader 自更新检查：查 GitHub Releases 最新版本，跟当前版本比较。"""
from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_REPO = "Guling-Pro/guling-trader"
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_EXE_ASSET_NAME = "guling-trader.exe"
_SHA256_ASSET_NAME = "guling-trader.exe.sha256"


@dataclass
class UpdateInfo:
    tag: str
    current_version: str
    latest_version: str
    exe_url: str
    sha256_url: str


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


async def check_for_update(current_version: str) -> Optional[UpdateInfo]:
    """查 GitHub 最新 Release，跟 current_version 比较。有新版本返回 UpdateInfo，否则 None。

    任何网络/解析异常都吞掉返回 None——升级检查从不是关键路径，不能因为它影响启动。
    """
    if platform.system() != "Windows":
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _RELEASES_LATEST_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning("GitHub Release API 返回非 200：%s", resp.status)
                    return None
                data = await resp.json()
    except Exception as e:
        logger.warning("检查 guling-trader 更新失败：%s", e)
        return None

    tag = data.get("tag_name", "")
    latest_version = tag.lstrip("v")
    if not latest_version or not _is_newer(latest_version, current_version):
        return None

    exe_url = None
    sha256_url = None
    for asset in data.get("assets", []):
        name = asset.get("name")
        if name == _EXE_ASSET_NAME:
            exe_url = asset.get("browser_download_url")
        elif name == _SHA256_ASSET_NAME:
            sha256_url = asset.get("browser_download_url")

    if not exe_url or not sha256_url:
        logger.warning("Release %s 缺少 %s 或 %s 资产，跳过", tag, _EXE_ASSET_NAME, _SHA256_ASSET_NAME)
        return None

    return UpdateInfo(
        tag=tag,
        current_version=current_version,
        latest_version=latest_version,
        exe_url=exe_url,
        sha256_url=sha256_url,
    )
