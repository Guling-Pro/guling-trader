"""异步下载 THS installer：重定向解析 + Range 续传 + 进度回调"""
import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)


async def resolve_redirect(url: str) -> str:
    """
    解析重定向 URL（HEAD 请求拿 Location header）。
    返回最终实际下载 URL。
    """
    logger.info("解析重定向：%s", url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("Location")
                    if location:
                        logger.info("重定向到：%s", location)
                        return location
                else:
                    logger.info("无重定向（status=%d），返回原 URL", resp.status)
                    return url
    except Exception as e:
        logger.warning("解析重定向出错：%s，返回原 URL", e)
        return url


async def download_with_progress(
    url: str,
    dest: Path | str,
    on_progress: Optional[Callable[[int, int], None]] = None,
    chunk_size: int = 65536,
) -> Path:
    """
    异步下载文件，支持 Range 续传和进度回调。

    Args:
        url: 下载 URL
        dest: 目标路径
        on_progress: 进度回调，接收 (bytes_downloaded, total_bytes)
        chunk_size: 单次读取大小

    Returns:
        目标文件 Path
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # 检查断点续传条件
    resume_from = 0
    if dest_path.exists():
        resume_from = dest_path.stat().st_size
        logger.info("断点续传：从 %d 字节继续", resume_from)

    try:
        async with aiohttp.ClientSession() as session:
            headers = {}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"

            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3600)) as resp:
                # 检查 Content-Length
                total_size = int(resp.headers.get("Content-Length", 0))
                if resume_from > 0 and resp.status == 206:  # Partial Content
                    # Range 续传时，加上已下载的大小
                    total_size = resume_from + total_size
                elif resp.status == 200:
                    resume_from = 0  # 服务器不支持 Range，重新开始
                    dest_path.unlink(missing_ok=True)
                else:
                    raise Exception(f"HTTP {resp.status}")

                logger.info("开始下载：url=%s total=%d bytes", url, total_size)

                bytes_downloaded = resume_from
                async with aiohttp.ClientSession() as session2:
                    async with session2.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3600)) as resp2:
                        mode = "ab" if resume_from > 0 else "wb"
                        with open(dest_path, mode) as f:
                            async for chunk in resp2.content.iter_chunked(chunk_size):
                                f.write(chunk)
                                bytes_downloaded += len(chunk)
                                if on_progress:
                                    on_progress(bytes_downloaded, total_size)

                logger.info("下载完成：%s（%d bytes）", dest_path, bytes_downloaded)
                return dest_path

    except Exception as e:
        logger.error("下载失败：%s", e)
        dest_path.unlink(missing_ok=True)
        raise


async def verify_sha256(file_path: Path, expected_sha256: str) -> bool:
    """
    异步验证文件 SHA256。
    """
    logger.info("验证 SHA256：%s", file_path)
    try:
        loop = asyncio.get_event_loop()
        def compute_hash():
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    sha256.update(chunk)
            return sha256.hexdigest()

        actual_sha256 = await loop.run_in_executor(None, compute_hash)
        if actual_sha256 == expected_sha256:
            logger.info("✓ SHA256 验证通过")
            return True
        else:
            logger.warning("✗ SHA256 不匹配：expected=%s actual=%s", expected_sha256, actual_sha256)
            return False
    except Exception as e:
        logger.warning("SHA256 验证出错：%s", e)
        return False
