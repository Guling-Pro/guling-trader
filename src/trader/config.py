"""配置文件读写：device_id, agent_token, account_name, paired_at"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TraderConfig:
    device_id: str
    agent_token: Optional[str] = None
    account_name: Optional[str] = None
    paired_at: Optional[str] = None
    xiadan_path_manual: Optional[str] = None  # 用户手动指定的 xiadan.exe 路径
    ws_endpoint: Optional[str] = None  # 自定义中转地址：只填域名或 IP[:端口]，协议和路径自动补全

    def has_paired(self) -> bool:
        """检查是否已配对"""
        return bool(self.agent_token)


def app_data_dir() -> Path:
    """便携式数据根目录（config / log / tmp 都放这里，统一管理、便于删除）。

    打包成 exe（PyInstaller frozen）时放在 **exe 同级** 的 ``guling-trader-data/``，
    源码运行时锁定在项目根目录同级的 ``guling-trader-data/``。
    完全不进入 Windows 全局 %APPDATA% 或 ~/.config，保证极佳的可调试性与单一真相源。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent / "guling-trader-data"
    else:
        # 锁定在项目根目录同级（config.py 文件的上三级，即 /Users/sunyang/Documents/Projects/guling-trader/guling-trader-data）
        base = Path(__file__).resolve().parents[2] / "guling-trader-data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def tmp_dir() -> Path:
    """临时文件目录（OCR 截图等），app_data_dir 下的 tmp/。"""
    d = app_data_dir() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_config_dir() -> Path:
    """返回配置文件目录"""
    return app_data_dir()


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
        "xiadan_path_manual": config.xiadan_path_manual,
        "ws_endpoint": config.ws_endpoint,
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


