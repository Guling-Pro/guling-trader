"""配置文件读写：device_id, agent_token, account_name, paired_at"""
import json
import os
import platform
import shutil
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
    不再散落到系统目录或 exe 当前目录。源码运行时回退 %APPDATA%（Win）/ ~/.config。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent / "guling-trader-data"
    elif platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / "guling-trader"
    else:
        base = Path.home() / ".config" / "guling-trader"
    base.mkdir(parents=True, exist_ok=True)
    return base


def tmp_dir() -> Path:
    """临时文件目录（OCR 截图等），app_data_dir 下的 tmp/。"""
    d = app_data_dir() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _legacy_config_path() -> Optional[Path]:
    """旧版本散落在系统目录的 config.json，用于一次性迁移（保留已配对 token）。"""
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "guling-trader" / "config.json" if appdata else None
    return Path.home() / ".config" / "guling-trader" / "config.json"


def _get_config_dir() -> Path:
    """返回配置文件目录"""
    return app_data_dir()


def _get_config_path() -> Path:
    """返回配置文件路径"""
    return _get_config_dir() / "config.json"


def _maybe_migrate_legacy() -> None:
    """新位置无 config.json 但旧系统目录有 → 复制过来，避免升级后被迫重新配对。"""
    new_path = _get_config_path()
    if new_path.exists():
        return
    legacy = _legacy_config_path()
    if legacy and legacy.exists() and legacy.resolve() != new_path.resolve():
        try:
            shutil.copy2(legacy, new_path)
        except Exception:
            pass


def load() -> TraderConfig:
    """从本地加载配置，如果不存在返回空配置"""
    _maybe_migrate_legacy()
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


