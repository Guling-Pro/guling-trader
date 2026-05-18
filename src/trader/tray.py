"""Windows Tray Icon 管理：状态色、菜单、消息气泡"""
import logging
import os
import platform
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .ws_client import ConnectionState

if TYPE_CHECKING:
    import pystray
    from PIL import Image

if platform.system() == "Windows":
    import pystray
    from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)


class TrayState(Enum):
    """Tray 图标状态"""

    UNPAIRED = "unpaired"
    DIALING = "dialing"
    AWAITING_BIND = "awaiting_bind"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FATAL = "fatal"


def _create_icon_image(state: TrayState) -> "Image.Image":
    """生成 tray 图标（16x16 像素的色块）"""
    colors = {
        TrayState.UNPAIRED: (128, 128, 128),
        TrayState.DIALING: (255, 200, 0),
        TrayState.AWAITING_BIND: (255, 200, 0),
        TrayState.CONNECTED: (0, 200, 0),
        TrayState.DISCONNECTED: (128, 128, 128),
        TrayState.FATAL: (255, 0, 0),
    }
    color = colors.get(state, (128, 128, 128))

    img = Image.new("RGB", (16, 16), color)
    return img


@dataclass
class TrayConfig:
    """Tray 配置"""

    xiadan_path: Optional[str] = None
    on_show_pairing_code: Optional[Callable[[str, int], None]] = None
    on_exit: Optional[Callable[[], None]] = None
    on_show_status: Optional[Callable[[str, str, str], None]] = None


class TrayManager:
    """Windows Tray Icon 管理"""

    def __init__(self, config: TrayConfig):
        self.config = config
        self.state = TrayState.UNPAIRED
        self.account_name = ""
        self.icon: Optional["pystray.Icon"] = None
        self.root: Optional[tk.Tk] = None
        self._thread: Optional[threading.Thread] = None
        self._should_exit = False

    def start(self) -> None:
        """启动 tray 线程"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_state(self, state: ConnectionState, account_name: str = "") -> None:
        """更新 tray 状态和账户名"""
        state_map = {
            ConnectionState.UNPAIRED: TrayState.UNPAIRED,
            ConnectionState.DIALING: TrayState.DIALING,
            ConnectionState.AWAITING_BIND: TrayState.AWAITING_BIND,
            ConnectionState.CONNECTED: TrayState.CONNECTED,
            ConnectionState.DISCONNECTED: TrayState.DISCONNECTED,
        }
        self.state = state_map.get(state, TrayState.UNPAIRED)
        self.account_name = account_name

        if self.icon:
            self._update_icon()

    def set_fatal(self, reason: str) -> None:
        """设置致命错误状态"""
        self.state = TrayState.FATAL
        if self.icon:
            self._update_icon()
            self.icon.notify(f"致命错误：{reason}", title="guling-trader")

    def show_notification(self, message: str, title: str = "guling-trader") -> None:
        """显示通知气泡"""
        if self.icon:
            self.icon.notify(message, title=title)

    def stop(self) -> None:
        """停止 tray"""
        self._should_exit = True
        if self.icon:
            self.icon.stop()

    def _run(self) -> None:
        """Tray 线程主循环"""
        if platform.system() != "Windows":
            logger.warning("Tray 仅支持 Windows")
            return

        try:
            self.root = tk.Tk()
            self.root.withdraw()

            menu = pystray.Menu(  # type: ignore
                pystray.MenuItem("配对码...", self._on_show_pairing_code),  # type: ignore
                pystray.MenuItem("连接状态", self._on_show_status),  # type: ignore
                pystray.MenuItem("打开 xiadan", self._on_open_xiadan),  # type: ignore
                pystray.MenuItem.SEPARATOR,  # type: ignore
                pystray.MenuItem("退出", self._on_exit),  # type: ignore
            )

            icon_image = _create_icon_image(self.state)
            self.icon = pystray.Icon("guling-trader", icon_image, menu=menu)  # type: ignore

            self.icon.run()
        except Exception as e:
            logger.error("Tray 运行出错：%s", e)
        finally:
            if self.root:
                self.root.destroy()

    def _update_icon(self) -> None:
        """更新 icon 图像"""
        if not self.icon:
            return
        icon_image = _create_icon_image(self.state)
        self.icon.icon = icon_image

    def _on_show_pairing_code(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:  # type: ignore
        """菜单：显示配对码"""
        if self.config.on_show_pairing_code:
            self.config.on_show_pairing_code("", 300)

    def _on_show_status(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:  # type: ignore
        """菜单：显示连接状态"""
        if self.config.on_show_status:
            self.config.on_show_status(self.state.value, self.account_name, "")

    def _on_open_xiadan(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:  # type: ignore
        """菜单：打开 xiadan"""
        if not self.config.xiadan_path:
            logger.warning("xiadan.exe 路径未设置")
            return

        try:
            if os.name == "nt":
                os.startfile(self.config.xiadan_path)
            else:
                subprocess.Popen([self.config.xiadan_path])
        except Exception as e:
            logger.error("启动 xiadan 失败：%s", e)

    def _on_exit(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:  # type: ignore
        """菜单：退出"""
        if self.config.on_exit:
            self.config.on_exit()
        self.stop()
