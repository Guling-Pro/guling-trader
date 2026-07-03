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
    enable_ths_plugin: bool = True  # 是否启用同花顺实盘交易插件，默认启用
    enable_rpa_suite: bool = False  # 是否启用可选的桌面 RPA 扩展模块
    chrome_cdp_port: int = 9222  # 本地 Edge/Chrome CDP 端口，默认 9222
    order_watch_idle_secs: int = 300   # order_watch 空闲轮询周期（秒）：默认 5 分钟
    order_watch_active_secs: int = 60  # 有未完成委托时的提速周期（秒）：默认 1 分钟
    enable_watchlist_watch: bool = True  # 是否定时同步自选股并推变化事件（新版 xiadan）
    watchlist_sync_hours: str = "8,12,16,20"  # 自选股定时同步的整点（避开交易时段）

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
        return TraderConfig(
            device_id=data.get("device_id", ""),
            agent_token=data.get("agent_token"),
            account_name=data.get("account_name"),
            paired_at=data.get("paired_at"),
            xiadan_path_manual=data.get("xiadan_path_manual"),
            ws_endpoint=data.get("ws_endpoint"),
            enable_ths_plugin=data.get("enable_ths_plugin", True),
            enable_rpa_suite=data.get("enable_rpa_suite", False),
            chrome_cdp_port=data.get("chrome_cdp_port", 9222),
            order_watch_idle_secs=data.get("order_watch_idle_secs", 300),
            order_watch_active_secs=data.get("order_watch_active_secs", 60),
            enable_watchlist_watch=data.get("enable_watchlist_watch", True),
            watchlist_sync_hours=data.get("watchlist_sync_hours", "8,12,16,20"),
        )
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
        "enable_ths_plugin": config.enable_ths_plugin,
        "enable_rpa_suite": config.enable_rpa_suite,
        "chrome_cdp_port": config.chrome_cdp_port,
        "order_watch_idle_secs": config.order_watch_idle_secs,
        "order_watch_active_secs": config.order_watch_active_secs,
        "enable_watchlist_watch": config.enable_watchlist_watch,
        "watchlist_sync_hours": config.watchlist_sync_hours,
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


