"""Installer metadata 管理：版本号、安装时间"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PRIVATE_INSTALL_DIR = Path("C:/guling-trader/同花顺")
META_FILE = PRIVATE_INSTALL_DIR / ".guling-meta.json"


def read_meta() -> Optional[dict]:
    """读取 .guling-meta.json，无法解析返回 None"""
    if not META_FILE.exists():
        return None
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("读取 meta 文件失败：%s", e)
        return None


def write_meta(installed_version: str, installed_at: Optional[str] = None) -> None:
    """写入 .guling-meta.json"""
    PRIVATE_INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    meta = {
        "installed_version": installed_version,
        "installed_at": installed_at or datetime.now().isoformat(),
    }

    try:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        logger.info("已写入 meta 文件：%s", meta)
    except Exception as e:
        logger.error("写入 meta 文件失败：%s", e)
        raise
