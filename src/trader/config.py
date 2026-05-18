"""配置文件读写：device_id, agent_token, account_name, paired_at"""
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TraderConfig:
    device_id: str
    agent_token: Optional[str] = None
    account_name: Optional[str] = None
    paired_at: Optional[str] = None

    def has_paired(self) -> bool:
        """检查是否已配对"""
        return bool(self.agent_token)


def _get_config_dir() -> Path:
    """返回配置文件目录"""
    if platform.system() == "Windows":
        config_dir = Path(os.environ["APPDATA"]) / "guling-trader"
    else:
        config_dir = Path.home() / ".config" / "guling-trader"

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _get_config_path() -> Path:
    """返回配置文件路径"""
    return _get_config_dir() / "config.json"


def load() -> TraderConfig:
    """从本地加载配置，如果不存在返回空配置"""
    config_path = _get_config_path()

    if not config_path.exists():
        return TraderConfig(device_id="")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return TraderConfig(**data)
    except Exception:
        return TraderConfig(device_id="")


def save(config: TraderConfig) -> None:
    """保存配置到本地"""
    config_path = _get_config_path()

    data = {
        "device_id": config.device_id,
        "agent_token": config.agent_token,
        "account_name": config.account_name,
        "paired_at": config.paired_at,
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


