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
    max_retries: int = 4,
) -> Path:
    """
    异步下载文件：单次 GET + 自动重试 + 断点续传 + 进度回调。

    针对慢/不稳的链路（如国内访问 GitHub Release CDN）优化：
    - **单 GET**：一次请求既拿大小又下数据（旧实现会先发一个 GET 只读 Content-Length
      再发第二个 GET 真正下载，在慢链路上等于翻倍建连与失败面）。
    - **自动重试 + 续传**：单次尝试失败时**保留已下的半成品**，下次尝试带 `Range` 头接着下，
      指数退避；只有重试全部耗尽才向上抛异常。失败时不删半成品，好让上层"重试"按钮也能续传。
    - **慢链路友好超时**：不设总时长上限（大文件慢下也不该被总时长砍掉），只在"连不上"
      （connect）和"卡住不再有数据"（sock_read）时超时失败。

    Args:
        url: 下载 URL
        dest: 目标路径
        on_progress: 进度回调，接收 (bytes_downloaded, total_bytes)
        chunk_size: 单次读取大小
        max_retries: 最大尝试次数（含首次）

    Returns:
        目标文件 Path
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # 连不上快速失败；下载中只要还有数据就耐心等，不给总时长封顶。
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=60)

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        resume_from = dest_path.stat().st_size if dest_path.exists() else 0
        headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    content_len = int(resp.headers.get("Content-Length", 0))
                    if resp.status == 206:  # Partial Content：服务器接受续传
                        total_size = resume_from + content_len
                        mode = "ab"
                    elif resp.status == 200:  # 全新下载或服务器不支持 Range
                        resume_from = 0
                        total_size = content_len
                        mode = "wb"
                    else:
                        raise Exception(f"HTTP {resp.status}")

                    logger.info(
                        "下载(第%d/%d次) url=%s resume=%d total=%d",
                        attempt, max_retries, url, resume_from, total_size,
                    )
                    bytes_downloaded = resume_from
                    with open(dest_path, mode) as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            f.write(chunk)
                            bytes_downloaded += len(chunk)
                            if on_progress:
                                on_progress(bytes_downloaded, total_size)

            logger.info("下载完成：%s（%d bytes）", dest_path, bytes_downloaded)
            return dest_path

        except Exception as e:
            last_err = e
            have = dest_path.stat().st_size if dest_path.exists() else 0
            logger.warning(
                "下载第 %d/%d 次失败：%s（保留已下 %d 字节，重试将续传）",
                attempt, max_retries, e, have,
            )
            if attempt < max_retries:
                await asyncio.sleep(min(2 ** attempt, 10))  # 指数退避，封顶 10s

    logger.error("下载最终失败（%d 次尝试后）：%s", max_retries, last_err)
    raise last_err if last_err else Exception("下载失败")


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
