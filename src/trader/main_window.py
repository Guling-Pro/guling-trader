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
    ths_steps_complete: int = 0  # [0..4] 已完成的 THS 步数
    ths_expanded: bool = True  # THS 区展开/折叠
    ths_refreshing: bool = False  # 配对码过期·正在刷新中
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
                "ths_steps_complete": self.ths_steps_complete,
                "ths_expanded": self.ths_expanded,
                "ths_refreshing": self.ths_refreshing,
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

# 中文状态标签
_STATE_LABELS = {
    "UNPAIRED": "未连接",
    "DIALING": "连接中",
    "AWAITING_BIND": "等待配对",
    "CONNECTED": "已连接",
    "DISCONNECTED": "已断开",
    "FATAL": "错误",
    "INSTALLING": "安装中",
}


class MainWindow:
    """主窗口。包含状态显示、配对码区、按钮、日志。"""

    def __init__(
        self,
        state: SharedState,
        on_open_xiadan: Optional[Callable[[], None]] = None,
        on_reset_pair: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
        on_redetect_xiadan: Optional[Callable[[], None]] = None,
        on_set_xiadan_path: Optional[Callable[[str], None]] = None,
        minimize_to_tray: bool = False,
    ):
        self.state = state
        self.on_open_xiadan = on_open_xiadan
        self.on_reset_pair = on_reset_pair
        self.on_exit_cb = on_exit
        self.on_redetect_xiadan = on_redetect_xiadan
        self.on_set_xiadan_path = on_set_xiadan_path
        self._minimize_to_tray = minimize_to_tray

        self.root = tk.Tk()
        self.root.title("guling-trader")
        # 显式 +200+200 位置防止 wine 把窗口扔到屏外
        self.root.geometry("520x540+200+200")
        self.root.minsize(420, 400)

        # Windows + tray 可用时：关闭按钮最小化到托盘；否则真退出
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
        self._status_frame = status_frame

        self.status_dot = tk.Canvas(
            status_frame, width=16, height=16, highlightthickness=0
        )
        self.status_dot.pack(side="left", padx=(0, 8))
        self._dot_id = self.status_dot.create_oval(2, 2, 14, 14, fill="#888888", outline="")

        self.status_label = ttk.Label(
            status_frame, text="状态：未连接", font=("Helvetica", 13, "bold")
        )
        self.status_label.pack(side="left")

        self.account_label = ttk.Label(status_frame, text="", foreground="#666")
        self.account_label.pack(side="left", padx=(20, 0))

        # 「解除绑定」按钮（仅 CONNECTED 时可见）
        self.btn_unbind = ttk.Button(
            status_frame, text="解除绑定", command=self._unbind_account
        )
        self.btn_unbind.pack(side="right")

        # ---- 配对码区（两个互斥视图）----
        # 容器 frame，用于 pack_forget/pack
        self.pair_frame = tk.Frame(self.root)
        self.pair_frame.pack(fill="x", padx=12, pady=4)

        # 视图 A：等待配对（黄底）
        self.pair_awaiting_frame = tk.Frame(self.pair_frame, bg="#fffbe6")
        self._build_pair_awaiting_view(self.pair_awaiting_frame)

        # 视图 B：刷新中（灰底）
        self.pair_refreshing_frame = tk.Frame(self.pair_frame, bg="#f0f0f0")
        self._build_pair_refreshing_view(self.pair_refreshing_frame)

        # ---- 同花顺状态区（两个互斥视图）----
        self.ths_frame = tk.Frame(self.root)
        self.ths_frame.pack(fill="x", padx=12, pady=4)

        # 视图 A：4 步进度卡片
        self.ths_wizard_frame = ttk.LabelFrame(self.ths_frame, text="同花顺配置", padding=(10, 6))
        self._build_ths_wizard_view(self.ths_wizard_frame)

        # 视图 B：已完成单行
        self.ths_done_frame = ttk.Frame(self.ths_frame)
        self._build_ths_done_view(self.ths_done_frame)

        # ---- 安装进度区（仅 INSTALLING 状态显示） ----
        self.install_frame = ttk.LabelFrame(self.root, text="安装进度", padding=(10, 6))
        # 默认不 pack，状态变 INSTALLING 时再 pack
        self.install_progress_var = tk.DoubleVar(master=self.root, value=0.0)
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
        ttk.Button(btn_frame, text="退出", command=self._on_close).pack(side="right")

    def _build_pair_awaiting_view(self, container: tk.Frame) -> None:
        """配对码区视图 A：等待配对（黄底）"""
        # 顶边线
        separator = tk.Frame(container, height=2, bg="#ffc800")
        separator.pack(side="top", fill="x")

        # 内容 frame
        content = tk.Frame(container, bg="#fffbe6", padx=10, pady=6)
        content.pack(fill="x")

        # 顶行：黄点 + 「等待配对」+ 倒计时
        top_row = tk.Frame(content, bg="#fffbe6")
        top_row.pack(fill="x")

        dot_canvas = tk.Canvas(top_row, width=10, height=10, bg="#fffbe6", highlightthickness=0)
        dot_canvas.pack(side="left", padx=(0, 8))
        dot_canvas.create_oval(2, 2, 8, 8, fill="#FFC800", outline="")

        ttk.Label(top_row, text="等待配对").pack(side="left")
        self.pair_await_countdown = ttk.Label(top_row, text="", foreground="#666")
        self.pair_await_countdown.pack(side="left", padx=(20, 0))

        # 中行：大字配对码
        self.pair_await_code_label = ttk.Label(
            content,
            text="（暂无）",
            font=("Consolas", 28, "bold"),
            foreground="#222",
        )
        self.pair_await_code_label.pack(pady=(6, 8))

        # 底行：提示 + 复制按钮
        bottom_row = tk.Frame(content, bg="#fffbe6")
        bottom_row.pack(fill="x")

        ttk.Label(
            bottom_row,
            text="前往股灵pro聊天窗口输入配对码完成绑定",
            foreground="#666",
        ).pack(side="left", padx=(4, 0))

        ttk.Button(
            bottom_row, text="复制", command=self._copy_pairing_code
        ).pack(side="right")

    def _build_pair_refreshing_view(self, container: tk.Frame) -> None:
        """配对码区视图 B：刷新中（灰底）"""
        # 顶边线
        separator = tk.Frame(container, height=2, bg="#4a90e2")
        separator.pack(side="top", fill="x")

        # 内容 frame
        content = tk.Frame(container, bg="#f0f0f0", padx=10, pady=6)
        content.pack(fill="x")

        # 顶行：spinner + 「等待配对」+ 「已过期」
        top_row = tk.Frame(content, bg="#f0f0f0")
        top_row.pack(fill="x")

        self.pair_refresh_spinner = ttk.Label(top_row, text="⟳", foreground="#4a90e2")
        self.pair_refresh_spinner.pack(side="left", padx=(0, 8))

        ttk.Label(top_row, text="等待配对").pack(side="left")
        ttk.Label(top_row, text="已过期", foreground="#E00000").pack(side="left", padx=(20, 0))

        # 中行：旧配对码 + strikethrough
        self.pair_refresh_old_code = ttk.Label(
            content,
            text="（暂无）",
            font=("Consolas", 28, "bold"),
            foreground="#999",
        )
        self.pair_refresh_old_code.pack(pady=(6, 8))

        # 底行：获取中提示
        ttk.Label(
            content,
            text="正在获取新配对码...",
            foreground="#4a90e2",
        ).pack()

    def _build_ths_wizard_view(self, container: ttk.LabelFrame) -> None:
        """THS 4 步进度卡片"""
        # 顶行：标题 + 轮询中提示
        title_row = tk.Frame(container)
        title_row.pack(fill="x", pady=(0, 8))

        self.ths_polling_indicator = ttk.Label(title_row, text="2 秒轮询中...", foreground="#666")
        self.ths_polling_indicator.pack(side="right")

        # 4 步行
        self.ths_steps_display = []
        for step_num in range(1, 5):
            step_frame = tk.Frame(container)
            step_frame.pack(fill="x", pady=4)

            # 圆圈（初始灰色）
            circle_label = tk.Label(step_frame, text="○", font=("Helvetica", 14), foreground="#999")
            circle_label.pack(side="left", padx=(0, 8))

            # 步骤文本
            step_text = [
                "hexin.exe 检测到",
                "xiadan.exe 进程",
                "「网上股票交易系统5.0」窗口",
                "xiadan 就绪",
            ][step_num - 1]
            label = ttk.Label(step_frame, text=f"Step {step_num}：{step_text}")
            label.pack(side="left")

            # 操作提示（初始隐藏）
            hint_label = ttk.Label(step_frame, text="", foreground="#FFC800")
            hint_label.pack(side="left", padx=(8, 0))

            self.ths_steps_display.append({
                "step_num": step_num,
                "circle": circle_label,
                "hint": hint_label,
                "frame": step_frame,
            })

    def _build_ths_done_view(self, container: tk.Frame) -> None:
        """THS 已完成单行视图"""
        content = tk.Frame(container)
        content.pack(fill="x", padx=10, pady=6)

        ttk.Label(content, text="✓").pack(side="left", padx=(0, 8))

        self.ths_done_path_label = ttk.Label(content, text="", foreground="#080")
        self.ths_done_path_label.pack(side="left")

        ttk.Button(content, text="更换", command=self._pick_xiadan_path).pack(side="right")

    def _schedule_poll(self) -> None:
        """tk after-loop 周期同步 SharedState → UI"""
        self._sync_state()
        self._drain_log_queue()
        self.root.after(100, self._schedule_poll)

    def _sync_state(self) -> None:
        snap = self.state.snapshot()

        # 状态 + 颜色 + 中文标签
        cs = snap["connection_state"]
        color = _STATE_COLORS.get(cs, "#888888")
        label_text = _STATE_LABELS.get(cs, cs)
        self.status_dot.itemconfig(self._dot_id, fill=color)
        self.status_label.config(text=f"状态：{label_text}")

        # 账户名
        self.account_label.config(text=f"账户：{snap['account_name']}" if snap["account_name"] else "")

        # 「解除绑定」仅 CONNECTED 时可见
        if cs == "CONNECTED" and not self.btn_unbind.winfo_ismapped():
            self.btn_unbind.pack(side="right")
        elif cs != "CONNECTED" and self.btn_unbind.winfo_ismapped():
            self.btn_unbind.pack_forget()

        # 配对码区显示/隐藏（用 winfo_ismapped 避免依赖 boolean flag）
        is_connected = (cs == "CONNECTED")
        if is_connected and self.pair_frame.winfo_ismapped():
            self.pair_frame.pack_forget()
        elif not is_connected and not self.pair_frame.winfo_ismapped():
            self.pair_frame.pack(fill="x", padx=12, pady=4, after=self._status_frame)

        # 配对码区视图切换（A/B）
        if not is_connected:
            is_refreshing = snap.get("ths_refreshing", False)
            exp_at = snap.get("pairing_expires_at")
            now = time.time()
            is_expired = exp_at is not None and now >= exp_at

            if is_refreshing or is_expired:
                # 视图 B：刷新中
                if self.pair_awaiting_frame.winfo_ismapped():
                    self.pair_awaiting_frame.pack_forget()
                if not self.pair_refreshing_frame.winfo_ismapped():
                    self.pair_refreshing_frame.pack(fill="x")
                # 更新旧配对码
                if snap["pairing_code"]:
                    self.pair_refresh_old_code.config(text=snap["pairing_code"])
            else:
                # 视图 A：等待配对
                if self.pair_refreshing_frame.winfo_ismapped():
                    self.pair_refreshing_frame.pack_forget()
                if not self.pair_awaiting_frame.winfo_ismapped():
                    self.pair_awaiting_frame.pack(fill="x")
                # 更新配对码和倒计时
                if snap["pairing_code"]:
                    self.pair_await_code_label.config(text=snap["pairing_code"])
                    if exp_at:
                        remaining = max(0, int(exp_at - now))
                        m, s = divmod(remaining, 60)
                        self.pair_await_countdown.config(text=f"{m}:{s:02d} 后失效")
                else:
                    self.pair_await_code_label.config(text="（暂无）")
                    self.pair_await_countdown.config(text="")

        # THS 区视图切换（wizard/done）+ 4 步更新
        ths_steps = snap.get("ths_steps_complete", 0)
        ths_expanded = snap.get("ths_expanded", True)

        if ths_expanded:
            if self.ths_done_frame.winfo_ismapped():
                self.ths_done_frame.pack_forget()
            if not self.ths_wizard_frame.winfo_ismapped():
                self.ths_wizard_frame.pack(fill="x", padx=12, pady=4)

            # 更新 4 步圆圈 + 提示
            hints = [
                "→ 请打开同花顺行情软件并登录",
                "→ 请点击同花顺右上角「委托」按钮",
                "→ 请在委托窗口内点击「切换旧版」",
                "（检测到即自动完成，无需手动操作）",
            ]
            for step_info in self.ths_steps_display:
                step_num = step_info["step_num"]
                if step_num <= ths_steps:
                    # 已完成
                    step_info["circle"].config(text="✓", foreground="#00C800")
                    step_info["hint"].config(text="")
                elif step_num == ths_steps + 1:
                    # 当前步骤（等待中）
                    step_info["circle"].config(text="⏳", foreground="#FFC800")
                    step_info["hint"].config(text=hints[step_num - 1], foreground="#FFC800")
                else:
                    # 未来步骤
                    step_info["circle"].config(text="○", foreground="#999")
                    step_info["hint"].config(text="")
        else:
            if self.ths_wizard_frame.winfo_ismapped():
                self.ths_wizard_frame.pack_forget()
            if not self.ths_done_frame.winfo_ismapped():
                self.ths_done_frame.pack(fill="x", padx=12, pady=4)

            # 更新已完成路径
            if snap["xiadan_path"]:
                self.ths_done_path_label.config(text=snap["xiadan_path"])

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

    def _unbind_account(self) -> None:
        """解除绑定按钮回调"""
        from tkinter import messagebox

        if messagebox.askokcancel("解除绑定", "确定要解除当前绑定？", parent=self.root):
            if self.on_reset_pair:
                self.on_reset_pair()
            else:
                self.state.log("⚠ 解除绑定：未注册回调")

    def _open_xiadan(self) -> None:
        if self.on_open_xiadan:
            self.on_open_xiadan()
        else:
            self.state.log("⚠ 打开同花顺：未注册回调")

    def _redetect_xiadan(self) -> None:
        if self.on_redetect_xiadan:
            self.on_redetect_xiadan()
        else:
            self.state.log("⚠ 重新检测：未注册回调")

    def _pick_xiadan_path(self) -> None:
        """打开文件对话框让用户选 xiadan.exe"""
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择 xiadan.exe（同花顺独立委托客户端）",
            filetypes=[("xiadan.exe", "xiadan.exe"), ("所有 exe", "*.exe"), ("所有文件", "*.*")],
        )
        if not path:
            return
        if self.on_set_xiadan_path:
            self.on_set_xiadan_path(path)
        else:
            self.state.log(f"⚠ 路径设置回调未注册（选了 {path}）")

    def _show_download_info(self) -> None:
        """显示同花顺下载链接弹窗（wine 下 webbrowser 不可靠，改用文字 + 复制按钮）"""
        from tkinter import Toplevel, messagebox

        url = "https://download.10jqka.com.cn/free/ths/"
        msg = (
            "请按以下步骤手动安装同花顺：\n\n"
            f"1. 浏览器打开：\n   {url}\n\n"
            "2. 下载「PC 端同花顺」(214MB)\n\n"
            "3. 双击 setup.exe 安装到 bottle\n   (CrossOver 会问选哪个 bottle — 选 guling-trader)\n\n"
            "4. 启动同花顺，登录券商账户，**切换到「旧版」交易客户端**\n\n"
            "5. 回到这里点「重新检测」或「指定路径...」"
        )
        # 弹窗里 URL 没法点击（tkinter 限制），但用户可复制
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        messagebox.showinfo(
            "下载同花顺",
            msg + f"\n\n（{url} 已复制到剪贴板）",
            parent=self.root,
        )

    def _on_close(self) -> None:
        """关闭按钮触发：Windows tray 模式下最小化到托盘，否则真退出"""
        if self._minimize_to_tray:
            self.root.withdraw()
        else:
            if self.on_exit_cb:
                self.on_exit_cb()
            self.root.destroy()

    def show_window(self) -> None:
        """从托盘恢复窗口（线程安全：用 after 调度到 tk 主线程）"""
        self.root.after(0, self._do_show_window)

    def _do_show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def run(self) -> None:
        """阻塞跑 tk mainloop。主线程调用。"""
        self.root.mainloop()
