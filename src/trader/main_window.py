"""Main GUI window — always visible primary UI.

Background：pystray system tray icon 在 wine/CrossOver 下不渲染。为了让 trader
在 macOS+CrossOver / Linux+wine 用户可用，主 UI 入口改成永远可见的 tkinter Tk()
window。tray icon 在真 Windows 上仍然可用，但只作为辅助（最小化收纳）。

Architecture:
- 主线程跑 tk.mainloop()
- 后台线程跑 asyncio event loop
- 两边通过 SharedState (thread-safe) + queue.Queue (log messages) 交换
- tk 周期 root.after(50, poll) 拉 SharedState 更新 UI
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import scrolledtext, ttk
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class SharedState:
    """主线程 + asyncio 线程共享状态。需要 lock 才能安全修改。"""

    connection_state: str = "UNPAIRED"
    account_name: str = ""
    pairing_code: Optional[str] = None
    pairing_expires_at: Optional[float] = None  # unix timestamp
    xiadan_path: Optional[str] = None
    last_pong_at: Optional[float] = None
    fatal_reason: Optional[str] = None
    install_progress: Optional[tuple[int, int]] = None  # (done, total)
    log_messages: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=500))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connection_state": self.connection_state,
                "account_name": self.account_name,
                "pairing_code": self.pairing_code,
                "pairing_expires_at": self.pairing_expires_at,
                "xiadan_path": self.xiadan_path,
                "last_pong_at": self.last_pong_at,
                "fatal_reason": self.fatal_reason,
                "install_progress": self.install_progress,
            }

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        try:
            self.log_messages.put_nowait(line)
        except queue.Full:
            try:
                self.log_messages.get_nowait()
                self.log_messages.put_nowait(line)
            except queue.Empty:
                pass


# 状态色 (绿/黄/红/灰/橙)
_STATE_COLORS = {
    "UNPAIRED": "#888888",
    "DIALING": "#FFC800",
    "AWAITING_BIND": "#FFC800",
    "CONNECTED": "#00C800",
    "DISCONNECTED": "#888888",
    "FATAL": "#E00000",
    "INSTALLING": "#FF9500",
}


class MainWindow:
    """主窗口。包含状态显示、配对码区、按钮、日志。"""

    def __init__(
        self,
        state: SharedState,
        on_open_xiadan: Optional[Callable[[], None]] = None,
        on_reset_pair: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
    ):
        self.state = state
        self.on_open_xiadan = on_open_xiadan
        self.on_reset_pair = on_reset_pair
        self.on_exit_cb = on_exit

        self.root = tk.Tk()
        self.root.title("guling-trader")
        # 显式 +200+200 位置防止 wine 把窗口扔到屏外
        self.root.geometry("520x540+200+200")
        self.root.minsize(420, 400)

        # 关闭按钮 → 触发真退出（不最小化到 tray，因为 tray 在 wine 下不可见）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._schedule_poll()

        # 强制窗口可见 + 在前——wine 下 tk 窗口默认有时被埋
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        self.root.deiconify()
        self.root.focus_force()

    def _build_ui(self) -> None:
        # ---- 状态条 ----
        status_frame = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        status_frame.pack(fill="x")

        self.status_dot = tk.Canvas(
            status_frame, width=16, height=16, highlightthickness=0
        )
        self.status_dot.pack(side="left", padx=(0, 8))
        self._dot_id = self.status_dot.create_oval(2, 2, 14, 14, fill="#888888", outline="")

        self.status_label = ttk.Label(
            status_frame, text="状态：UNPAIRED", font=("Helvetica", 13, "bold")
        )
        self.status_label.pack(side="left")

        self.account_label = ttk.Label(status_frame, text="", foreground="#666")
        self.account_label.pack(side="right")

        # ---- 配对码区 ----
        pair_frame = ttk.LabelFrame(self.root, text="配对码", padding=(10, 6))
        pair_frame.pack(fill="x", padx=12, pady=4)

        self.pair_code_label = ttk.Label(
            pair_frame,
            text="（暂无）",
            font=("Helvetica", 22, "bold"),
            foreground="#222",
        )
        self.pair_code_label.pack(side="left", padx=(4, 12))

        self.pair_countdown_label = ttk.Label(pair_frame, text="", foreground="#888")
        self.pair_countdown_label.pack(side="left")

        self.copy_btn = ttk.Button(
            pair_frame, text="复制", command=self._copy_pairing_code, state="disabled"
        )
        self.copy_btn.pack(side="right")

        # ---- 同花顺状态区 ----
        ths_frame = ttk.LabelFrame(self.root, text="同花顺", padding=(10, 6))
        ths_frame.pack(fill="x", padx=12, pady=4)

        self.xiadan_label = ttk.Label(ths_frame, text="未检测", foreground="#888")
        self.xiadan_label.pack(side="left")

        # ---- 安装进度区（仅 INSTALLING 状态显示） ----
        self.install_frame = ttk.LabelFrame(self.root, text="安装进度", padding=(10, 6))
        # 默认不 pack，状态变 INSTALLING 时再 pack
        self.install_progress_var = tk.DoubleVar(value=0.0)
        self.install_progress_bar = ttk.Progressbar(
            self.install_frame,
            variable=self.install_progress_var,
            maximum=100.0,
            length=300,
        )
        self.install_progress_bar.pack(side="left", padx=(0, 8))
        self.install_progress_label = ttk.Label(self.install_frame, text="")
        self.install_progress_label.pack(side="left")

        # ---- 日志区 ----
        log_frame = ttk.LabelFrame(self.root, text="日志", padding=(8, 4))
        log_frame.pack(fill="both", expand=True, padx=12, pady=4)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, state="disabled", font=("Menlo", 10)
        )
        self.log_text.pack(fill="both", expand=True)

        # ---- 按钮区 ----
        btn_frame = ttk.Frame(self.root, padding=(12, 6, 12, 12))
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="打开同花顺", command=self._open_xiadan).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btn_frame, text="重新配对", command=self._reset_pair).pack(
            side="left", padx=6
        )
        ttk.Button(btn_frame, text="退出", command=self._on_close).pack(side="right")

    def _schedule_poll(self) -> None:
        """tk after-loop 周期同步 SharedState → UI"""
        self._sync_state()
        self._drain_log_queue()
        self.root.after(100, self._schedule_poll)

    def _sync_state(self) -> None:
        snap = self.state.snapshot()

        # 状态 + 颜色
        cs = snap["connection_state"]
        color = _STATE_COLORS.get(cs, "#888888")
        self.status_dot.itemconfig(self._dot_id, fill=color)
        self.status_label.config(text=f"状态：{cs}")

        # 账户名
        if snap["account_name"]:
            self.account_label.config(text=f"账户：{snap['account_name']}")
        else:
            self.account_label.config(text="")

        # 配对码
        if snap["pairing_code"]:
            self.pair_code_label.config(text=snap["pairing_code"])
            self.copy_btn.config(state="normal")
            if snap["pairing_expires_at"]:
                remaining = max(0, int(snap["pairing_expires_at"] - time.time()))
                if remaining > 0:
                    m, s = divmod(remaining, 60)
                    self.pair_countdown_label.config(text=f"{m}:{s:02d} 后失效")
                else:
                    self.pair_countdown_label.config(text="已过期", foreground="#E00000")
        else:
            self.pair_code_label.config(text="（暂无）")
            self.pair_countdown_label.config(text="", foreground="#888")
            self.copy_btn.config(state="disabled")

        # xiadan
        if snap["xiadan_path"]:
            self.xiadan_label.config(text=f"✓ {snap['xiadan_path']}", foreground="#080")
        else:
            self.xiadan_label.config(text="未检测", foreground="#888")

        # 安装进度
        if snap["install_progress"]:
            done, total = snap["install_progress"]
            pct = (done / total * 100) if total > 0 else 0
            if not self.install_frame.winfo_ismapped():
                self.install_frame.pack(fill="x", padx=12, pady=4)
            self.install_progress_var.set(pct)
            mb_done = done / 1024 / 1024
            mb_total = total / 1024 / 1024
            self.install_progress_label.config(
                text=f"{mb_done:.1f} / {mb_total:.1f} MB ({pct:.0f}%)"
            )
        elif self.install_frame.winfo_ismapped() and cs != "INSTALLING":
            self.install_frame.pack_forget()

    def _drain_log_queue(self) -> None:
        """把 SharedState.log_messages 队列里的内容刷到 log_text 区"""
        new_lines = []
        try:
            while True:
                new_lines.append(self.state.log_messages.get_nowait())
        except queue.Empty:
            pass

        if not new_lines:
            return

        self.log_text.config(state="normal")
        for line in new_lines:
            self.log_text.insert("end", line + "\n")
        # 限制总行数
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 500:
            self.log_text.delete("1.0", f"{line_count - 500}.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _copy_pairing_code(self) -> None:
        snap = self.state.snapshot()
        if not snap["pairing_code"]:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(snap["pairing_code"])
        self.state.log(f"已复制配对码 {snap['pairing_code']} 到剪贴板")

    def _open_xiadan(self) -> None:
        if self.on_open_xiadan:
            self.on_open_xiadan()
        else:
            self.state.log("⚠ 打开同花顺：未注册回调")

    def _reset_pair(self) -> None:
        if self.on_reset_pair:
            self.on_reset_pair()
        else:
            self.state.log("⚠ 重新配对：未注册回调")

    def _on_close(self) -> None:
        """退出按钮 / 关闭按钮触发"""
        if self.on_exit_cb:
            self.on_exit_cb()
        self.root.destroy()

    def run(self) -> None:
        """阻塞跑 tk mainloop。主线程调用。"""
        self.root.mainloop()
