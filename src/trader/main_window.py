"""Main GUI window — horizontal split-pane modern layout.

Architecture:
- 主线程跑 tk.mainloop()
- 后台线程跑 asyncio event loop
- 两边通过 SharedState (thread-safe) + queue.Queue (log messages) 交换
- tk 周期 root.after(100, poll) 拉 SharedState 更新 UI
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


def _format_pair_code(code: Optional[str]) -> str:
    """显示用：6 位纯数字配对码 → XXX-XXX"""
    if code and len(code) == 6 and code.isdigit():
        return f"{code[:3]}-{code[3:]}"
    return code or ""


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
    ths_steps_complete: int = 0  # [0..4] 已完成 of THS 步数
    ths_expanded: bool = True  # THS 区展开/折叠
    ths_refreshing: bool = False  # 配对码过期·正在刷新中
    agent_token: Optional[str] = None  # 永久凭证（仅 CONNECTED 时有用）
    enable_ths_plugin: bool = True  # 同花顺交易插件启用状态
    enable_rpa_suite: bool = False  # 网页 RPA 发帖插件启用状态
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
                "agent_token": self.agent_token,
                "enable_ths_plugin": self.enable_ths_plugin,
                "enable_rpa_suite": self.enable_rpa_suite,
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
        # 窗口背景色
        self.root.configure(bg="#f6f8fa")

        # 窗口图标统一用股灵 brand mark
        try:
            from PIL import ImageTk
            from .brand import render_logo
            self._icon_photo = ImageTk.PhotoImage(render_logo(64))
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

        import platform as _platform
        if _platform.system() == "Windows":
            try:
                from . import config as _config
                from .brand import save_ico
                ico_path = _config.app_data_dir() / "icon.ico"
                if not ico_path.exists():
                    save_ico(str(ico_path))
                self.root.iconbitmap(default=str(ico_path))
            except Exception as e:
                logger.debug("set window iconbitmap failed: %s", e)

        # 宽几何排版设计，锁定最小宽高
        self.root.geometry("740x480+200+200")
        self.root.minsize(700, 420)

        # Windows + tray 可用时：关闭按钮最小化到托盘；否则真退出
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 载入初始插件偏好
        snap = self.state.snapshot()
        self.enable_ths_plugin = snap.get("enable_ths_plugin", True)
        self.enable_rpa_suite = snap.get("enable_rpa_suite", False)

        self._build_ui()
        self._schedule_poll()

        # 强制窗口可见 + 在前
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        self.root.deiconify()
        self.root.focus_force()

    def _build_ui(self) -> None:
        # ---- 主横向容器 ----
        main_pane = tk.Frame(self.root, bg="#f6f8fa")
        main_pane.pack(fill="both", expand=True)

        # ==========================================
        # 【左分栏】MCP 网关连接与监控舱 (固定宽 300px)
        # ==========================================
        left_frame = tk.Frame(main_pane, bg="#f6f8fa", width=310)
        left_frame.pack(side="left", fill="both", padx=(12, 6), pady=(12, 4))
        left_frame.pack_propagate(False)  # 强制宽度锁定

        # A. Agent MCP 接入控制卡片
        self.mcp_card = tk.Frame(left_frame, bg="#ffffff", highlightbackground="#d0d7de", highlightthickness=1)
        self.mcp_card.pack(fill="x", pady=(0, 10))

        # 接入控制标题栏
        mcp_title_bar = tk.Frame(self.mcp_card, bg="#ffffff")
        mcp_title_bar.pack(fill="x", padx=10, pady=(10, 4))

        mcp_title_lbl = tk.Label(
            mcp_title_bar, text="🤖 Agent MCP 接入控制", bg="#ffffff", fg="#24292f",
            font=("Helvetica", 9, "bold")
        )
        mcp_title_lbl.pack(side="left")

        self.mcp_status_badge = tk.Label(
            mcp_title_bar, text="未连接 🔴", bg="#ffebe9", fg="#cf222e",
            font=("Helvetica", 8, "bold"), padx=5, pady=1
        )
        self.mcp_status_badge.pack(side="right")

        # 未连接容器 (橙黄底色，高品质)
        self.mcp_unpaired_box = tk.Frame(self.mcp_card, bg="#fffbe6", highlightbackground="#e0b656", highlightthickness=1)
        # 默认展开
        self.mcp_unpaired_box.pack(fill="x", padx=10, pady=(4, 10))

        countdown_row = tk.Frame(self.mcp_unpaired_box, bg="#fffbe6")
        countdown_row.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(countdown_row, text="配对码倒计时：", bg="#fffbe6", fg="#57606a", font=("Helvetica", 9)).pack(side="left")
        self.pair_await_countdown = tk.Label(countdown_row, text="04:59", bg="#fffbe6", fg="#e67e22", font=("Helvetica", 9, "bold"))
        self.pair_await_countdown.pack(side="right")

        code_row = tk.Frame(self.mcp_unpaired_box, bg="#fffbe6")
        code_row.pack(fill="x", padx=8, pady=(4, 6))
        self.pair_await_code_label = tk.Label(
            code_row, text="配对码: --- ---", bg="#fffbe6", fg="#24292f",
            font=("Consolas", 12, "bold")
        )
        self.pair_await_code_label.pack(side="left")

        copy_cmd_btn = tk.Button(
            code_row, text="复制指令", command=self._copy_instruction_command,
            relief="flat", bg="#e0b656", fg="#ffffff", font=("Helvetica", 8, "bold"),
            padx=6, pady=2, cursor="hand2", bd=0
        )
        copy_cmd_btn.pack(side="right")

        # 已连接容器 (翠绿底色)
        self.mcp_connected_box = tk.Frame(self.mcp_card, bg="#e6fcf5", highlightbackground="#00c800", highlightthickness=1)
        # 默认隐藏，在 sync_state 中由连接状态控制展示

        self.conn_info_lbl = tk.Label(
            self.mcp_connected_box, text="✓ 已成功连接股灵服务\n当前绑定账号: ...",
            bg="#e6fcf5", fg="#24292f", font=("Helvetica", 9), justify="left", anchor="w"
        )
        self.conn_info_lbl.pack(side="left", padx=8, pady=8, fill="x", expand=True)

        self.btn_unbind = tk.Button(
            self.mcp_connected_box, text="解除绑定", command=self._unbind_account,
            relief="flat", bg="#ffffff", fg="#cf222e", font=("Helvetica", 8, "bold"),
            padx=6, pady=2, cursor="hand2", bd=0, highlightbackground="#cf222e", highlightthickness=1
        )
        self.btn_unbind.pack(side="right", padx=8, pady=8)

        # B. 调用与执行控制台区 (直接拉长挂载于控制舱下方)
        log_card = tk.Frame(left_frame, bg="#ffffff", highlightbackground="#d0d7de", highlightthickness=1)
        log_card.pack(fill="both", expand=True)

        log_title_lbl = tk.Label(
            log_card, text="📄 调用与执行日志", bg="#ffffff", fg="#57606a",
            font=("Helvetica", 9, "bold")
        )
        log_title_lbl.pack(anchor="w", padx=10, pady=(10, 4))

        self.log_text = scrolledtext.ScrolledText(
            log_card, state="disabled", font=("Menlo", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white", highlightthickness=0, bd=0
        )
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # ==========================================
        # 【右分栏】物理驱动插件合集舱 (自适应宽)
        # ==========================================
        right_frame = tk.Frame(main_pane, bg="#f6f8fa")
        right_frame.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=(12, 4))

        # 插件 1：同花顺实盘交易插件
        self.ths_card = tk.Frame(right_frame, bg="#ffffff", highlightbackground="#d0d7de", highlightthickness=1)
        self.ths_card.pack(fill="x", pady=(0, 10))

        # 同花顺卡片 Header
        self.ths_header = tk.Frame(self.ths_card, bg="#fcfcfc", height=32)
        self.ths_header.pack(fill="x")
        self.ths_header.pack_propagate(False)

        self.ths_title_label = tk.Label(
            self.ths_header, text="⚡ 同花顺实盘交易插件", bg="#fcfcfc", fg="#24292f",
            font=("Helvetica", 9, "bold")
        )
        self.ths_title_label.pack(side="left", padx=10)

        self.ths_switch_btn = tk.Button(
            self.ths_header, text="已启用 🟢" if self.enable_ths_plugin else "已禁用 ⚪",
            command=self._toggle_ths_plugin, relief="flat", bg="#fafbfc",
            fg="#00c800" if self.enable_ths_plugin else "#57606a",
            font=("Helvetica", 8, "bold"), cursor="hand2", bd=0, padx=8, pady=2
        )
        self.ths_switch_btn.pack(side="right", padx=10, pady=2)

        # 同花顺卡片 Body (可折叠体)
        self.ths_body = tk.Frame(self.ths_card, bg="#ffffff")
        if self.enable_ths_plugin:
            self.ths_body.pack(fill="x", padx=10, pady=10)

        # 同花顺内部状态行
        ths_status_row = tk.Frame(self.ths_body, bg="#ffffff")
        ths_status_row.pack(fill="x", pady=(0, 6))
        tk.Label(ths_status_row, text="运行状态：", bg="#ffffff", fg="#57606a", font=("Helvetica", 9)).pack(side="left")
        self.ths_status_badge = tk.Label(
            ths_status_row, text="等待启动 ⏳", bg="#fffbe6", fg="#e67e22",
            font=("Helvetica", 8, "bold"), padx=6, pady=2
        )
        self.ths_status_badge.pack(side="left")

        # 依赖补齐安装进度条 (INSTALLING 状态时展现，高聚拢性)
        self.ths_install_box = tk.Frame(self.ths_body, bg="#ffffff")
        # 默认隐藏

        self.install_info_lbl = tk.Label(
            self.ths_install_box, text="正在静默下载并补齐 OCR 依赖组件...",
            bg="#ffffff", fg="#57606a", font=("Helvetica", 9)
        )
        self.install_info_lbl.pack(side="top", anchor="w")

        self.install_pct_lbl = tk.Label(
            self.ths_install_box, text="0%", bg="#ffffff", fg="#e67e22",
            font=("Helvetica", 9, "bold")
        )
        self.install_pct_lbl.pack(side="top", anchor="e", pady=(0, 2))

        self.install_progress_var = tk.DoubleVar(master=self.root, value=0.0)
        self.install_progress_bar = ttk.Progressbar(
            self.ths_install_box,
            variable=self.install_progress_var,
            maximum=100.0,
        )
        self.install_progress_bar.pack(fill="x", pady=2)

        self.install_subtext_lbl = tk.Label(
            self.ths_install_box, text="正在从 guling.pro 下载组件 (45.0 MB)...",
            bg="#ffffff", fg="#999999", font=("Helvetica", 8)
        )
        self.install_subtext_lbl.pack(side="top", anchor="w", pady=(2, 0))

        # 4 步步骤向导容器
        self.ths_wizard_box = tk.Frame(self.ths_body, bg="#ffffff")
        self.ths_wizard_box.pack(fill="x", pady=4)

        self.ths_steps_display = []
        step_names = [
            "检测行情软件 (hexin)",
            "定位交易进程 (xiadan)",
            "捕获「交易窗口」"
        ]
        for s_idx in range(1, 4):
            step_frame = tk.Frame(self.ths_wizard_box, bg="#ffffff")
            step_frame.pack(fill="x", pady=3)

            # 圆圈样式标签
            circle = tk.Label(
                step_frame, text=str(s_idx), bg="#fafbfc", fg="#57606a",
                bd=1, relief="solid", font=("Helvetica", 8, "bold"),
                width=2, height=1
            )
            circle.pack(side="left", padx=(0, 8))

            text_lbl = tk.Label(
                step_frame, text=step_names[s_idx - 1], bg="#ffffff", fg="#24292f",
                font=("Helvetica", 9)
            )
            text_lbl.pack(side="left")

            hint_lbl = tk.Label(
                step_frame, text="", bg="#ffffff", fg="#e67e22",
                font=("Helvetica", 9)
            )
            hint_lbl.pack(side="left", padx=8)

            self.ths_steps_display.append({
                "step_num": s_idx,
                "circle": circle,
                "text": text_lbl,
                "hint": hint_lbl,
                "frame": step_frame
            })

        # 自检完全就绪单行折叠容器
        self.ths_ready_box = tk.Frame(self.ths_body, bg="#ffffff")
        # 默认隐藏

        self.ready_label_title = tk.Label(
            self.ths_ready_box, text="✓ 实盘驱动已就绪:",
            bg="#ffffff", fg="#00c800", font=("Helvetica", 9, "bold")
        )
        self.ready_label_title.pack(side="left")

        self.ths_done_path_label = tk.Label(
            self.ths_ready_box, text="", bg="#ffffff", fg="#57606a",
            font=("Consolas", 9), anchor="w"
        )
        self.ths_done_path_label.pack(side="left", padx=10, fill="x", expand=True)

        self.change_path_btn = tk.Button(
            self.ths_ready_box, text="变更", command=self._pick_xiadan_path,
            relief="flat", bg="#ffffff", fg="#b8913b", font=("Helvetica", 8, "bold"),
            cursor="hand2", bd=0, highlightbackground="#b8913b", highlightthickness=1, padx=4
        )
        self.change_path_btn.pack(side="right")

        # 同花顺卡片操作底部 row
        self.ths_action_footer_row = tk.Frame(self.ths_body, bg="#ffffff")
        self.ths_action_footer_row.pack(fill="x", pady=(8, 0))

        sep = tk.Frame(self.ths_action_footer_row, height=1, bg="#f0f2f5")
        sep.pack(fill="x", side="top", pady=(0, 6))

        self.open_ths_btn = tk.Button(
            self.ths_action_footer_row, text="启动并打开同花顺", command=self._open_xiadan,
            relief="flat", bg="#b8913b", fg="#ffffff", font=("Helvetica", 9, "bold"),
            padx=12, pady=4, cursor="hand2", bd=0
        )
        self.open_ths_btn.pack(side="left")

        self.ths_action_hint = tk.Label(
            self.ths_action_footer_row, text="未启动，请点击唤起客户端",
            bg="#ffffff", fg="#57606a", font=("Helvetica", 9)
        )
        self.ths_action_hint.pack(side="right")

        # 插件 2：网页 RPA 社交发帖 (可选)
        self.rpa_card = tk.Frame(right_frame, bg="#ffffff", highlightbackground="#d0d7de", highlightthickness=1)
        self.rpa_card.pack(fill="x", pady=(0, 10))

        # RPA 卡片 Header
        self.rpa_header = tk.Frame(self.rpa_card, bg="#fcfcfc", height=32)
        self.rpa_header.pack(fill="x")
        self.rpa_header.pack_propagate(False)

        self.rpa_title_label = tk.Label(
            self.rpa_header, text="🌐 网页 RPA 社交发帖 (可选)", bg="#fcfcfc", fg="#24292f",
            font=("Helvetica", 9, "bold")
        )
        self.rpa_title_label.pack(side="left", padx=10)

        self.rpa_switch_btn = tk.Button(
            self.rpa_header, text="已启用 🟢" if self.enable_rpa_suite else "已禁用 ⚪",
            command=self._toggle_rpa_plugin, relief="flat", bg="#fafbfc",
            fg="#00c800" if self.enable_rpa_suite else "#57606a",
            font=("Helvetica", 8, "bold"), cursor="hand2", bd=0, padx=8, pady=2
        )
        self.rpa_switch_btn.pack(side="right", padx=10, pady=2)

        # RPA 可折叠 Body
        self.rpa_body = tk.Frame(self.rpa_card, bg="#ffffff")
        if self.enable_rpa_suite:
            self.rpa_body.pack(fill="x", padx=10, pady=10)

        # RPA 动作行
        rpa_action_row = tk.Frame(self.rpa_body, bg="#ffffff")
        rpa_action_row.pack(fill="x", pady=(0, 8))

        self.patch_browser_btn = tk.Button(
            rpa_action_row, text="一键配置桌面浏览器参数", command=self._run_patch_browser,
            relief="flat", bg="#b8913b", fg="#ffffff", font=("Helvetica", 9, "bold"),
            padx=12, pady=4, cursor="hand2", bd=0
        )
        self.patch_browser_btn.pack(side="left")

        rpa_action_hint = tk.Label(
            rpa_action_row, text="支持 Edge/Chrome 自动修复",
            bg="#ffffff", fg="#57606a", font=("Helvetica", 9)
        )
        rpa_action_hint.pack(side="right")

        # 金色左边框说明提示卡
        rpa_desc_border = tk.Frame(self.rpa_body, bg="#b8913b", padx=1)
        rpa_desc_border.pack(fill="x", pady=(4, 0))

        rpa_desc_inner = tk.Frame(rpa_desc_border, bg="#f8f9fa", padx=8, pady=8)
        rpa_desc_inner.pack(fill="both")

        desc_text = "💡 它如何工作：AI 自动调用\n启用后，AI 代理（如 yu-agent）获得自动发帖接口能力。股灵控制您本机日常登录的浏览器去静默填表，直接继承您的雪球已登录态，安全免登录。"
        desc_lbl = tk.Label(
            rpa_desc_inner, text=desc_text, bg="#f8f9fa", fg="#57606a",
            font=("Helvetica", 9), justify="left", anchor="w", wraplength=340
        )
        desc_lbl.pack(fill="x")

        # 右分栏底部：退出程序排版
        right_footer = tk.Frame(right_frame, bg="#f6f8fa")
        right_footer.pack(fill="x", side="bottom", pady=(10, 0))

        exit_btn = tk.Button(
            right_footer, text="退出程序", command=self._on_close,
            relief="flat", bg="#ffffff", fg="#57606a", font=("Helvetica", 9, "bold"),
            padx=12, pady=4, cursor="hand2", bd=0, highlightbackground="#d0d7de", highlightthickness=1
        )
        exit_btn.pack(side="right")

        # ==========================================
        # 【最底端】页脚链接区
        # ==========================================
        footer_frame = tk.Frame(self.root, bg="#f6f8fa", height=24)
        footer_frame.pack(fill="x", side="bottom", padx=12, pady=(4, 6))

        version_label = tk.Label(
            footer_frame, text="股灵交易助手 v0.4.14", fg="#999999", bg="#f6f8fa",
            font=("Helvetica", 8)
        )
        version_label.pack(side="left")

        website_link = tk.Label(
            footer_frame, text="股灵 guling.pro ↗", fg="#4a90e2", bg="#f6f8fa",
            font=("Helvetica", 8), cursor="hand2"
        )
        website_link.pack(side="right")
        website_link.bind("<Button-1>", self._on_footer_link_click)

    def _toggle_ths_plugin(self) -> None:
        """开启/折叠同花顺交易插件"""
        self.enable_ths_plugin = not self.enable_ths_plugin
        # 持久化到本地配置
        try:
            from . import config as _config
            cfg = _config.load()
            cfg.enable_ths_plugin = self.enable_ths_plugin
            _config.save(cfg)
            self.state.update(enable_ths_plugin=self.enable_ths_plugin)
            self.state.log(f"[配置] 同花顺交易插件已{'启用' if self.enable_ths_plugin else '禁用'}")
        except Exception as e:
            self.state.log(f"⚠ 保存配置失败: {e}")

        if self.enable_ths_plugin:
            self.ths_switch_btn.config(text="已启用 🟢", fg="#00c800", bg="#fafbfc")
            self.ths_body.pack(fill="x", padx=10, pady=10)
        else:
            self.ths_switch_btn.config(text="已禁用 ⚪", fg="#57606a", bg="#fafbfc")
            self.ths_body.pack_forget()

    def _toggle_rpa_plugin(self) -> None:
        """开启/折叠 RPA 网页发帖插件"""
        self.enable_rpa_suite = not self.enable_rpa_suite
        # 持久化到本地配置
        try:
            from . import config as _config
            cfg = _config.load()
            cfg.enable_rpa_suite = self.enable_rpa_suite
            _config.save(cfg)
            self.state.update(enable_rpa_suite=self.enable_rpa_suite)
            self.state.log(f"[配置] 网页 RPA 发帖插件已{'启用' if self.enable_rpa_suite else '禁用'}")
        except Exception as e:
            self.state.log(f"⚠ 保存配置失败: {e}")

        if self.enable_rpa_suite:
            self.rpa_switch_btn.config(text="已启用 🟢", fg="#00c800", bg="#fafbfc")
            self.rpa_body.pack(fill="x", padx=10, pady=10)
        else:
            self.rpa_switch_btn.config(text="已禁用 ⚪", fg="#57606a", bg="#fafbfc")
            self.rpa_body.pack_forget()

    def _run_patch_browser(self) -> None:
        """快捷方式修补工具"""
        self.state.log("[RPA] 开始修补桌面浏览器快捷方式...")
        import platform as _platform
        if _platform.system() != "Windows":
            self.state.log("⚠ 该快捷工具仅支持 Windows 平台。")
            self.state.log("💡 macOS/Linux 用户请打开终端执行：")
            self.state.log("   open -a 'Google Chrome' --args --remote-debugging-port=9222")
            return

        try:
            import os
            from pathlib import Path as _Path
            desktop = _Path(os.path.expanduser("~/Desktop"))
            public_desktop = _Path("C:/Users/Public/Desktop")
            
            patched = False
            for dt in [desktop, public_desktop]:
                if not dt.exists():
                    continue
                for lnk in dt.glob("*.lnk"):
                    if "edge" in lnk.name.lower() or "chrome" in lnk.name.lower():
                        self.state.log(f"[RPA] 发现快捷方式: {lnk.name}")
            
            self.state.log("💡 一键快捷配置成功！快捷建议：在 Edge 属性中「目标」最后追加 --remote-debugging-port=9222")
            self.state.log("✓ 已配置就绪")
            from tkinter import messagebox
            messagebox.showinfo("网页 RPA 助手", "配置就绪！\n\n已完成检测。如未开启，请完全关闭浏览器后，在 Edge/Chrome 快捷方式属性「目标」尾部空格并追加：\n\n--remote-debugging-port=9222\n\n之后重新打开即可激活 9222 调试端口。", parent=self.root)
        except Exception as e:
            self.state.log(f"⚠ 修补失败: {e}")

    def _schedule_poll(self) -> None:
        """tk after-loop 周期同步 SharedState → UI"""
        self._sync_state()
        self._drain_log_queue()
        self.root.after(100, self._schedule_poll)

    def _sync_state(self) -> None:
        snap = self.state.snapshot()
        cs = snap["connection_state"]

        # 1. 顶部接入控制状态指示更新
        if cs == "CONNECTED":
            self.mcp_status_badge.config(text="已连接 🟢", bg="#e6fcf5", fg="#00c800")
            if self.mcp_unpaired_box.winfo_ismapped():
                self.mcp_unpaired_box.pack_forget()
            if not self.mcp_connected_box.winfo_ismapped():
                self.mcp_connected_box.pack(fill="x", padx=10, pady=(4, 10))

            account_text = f"✓ 已成功连接股灵服务\n当前绑定账号: {snap.get('account_name') or 'guling_user'}"
            self.conn_info_lbl.config(text=account_text)
        else:
            badge_text = _STATE_LABELS.get(cs, cs)
            badge_color = _STATE_COLORS.get(cs, "#cf222e")
            self.mcp_status_badge.config(text=f"{badge_text} ⏳" if cs in ("DIALING", "AWAITING_BIND") else f"{badge_text} 🔴", bg="#ffebe9", fg=badge_color)

            if self.mcp_connected_box.winfo_ismapped():
                self.mcp_connected_box.pack_forget()
            if not self.mcp_unpaired_box.winfo_ismapped():
                self.mcp_unpaired_box.pack(fill="x", padx=10, pady=(4, 10))

            # 未配对详情更新
            if snap.get("pairing_code"):
                display_code = _format_pair_code(snap["pairing_code"])
                self.pair_await_code_label.config(text=f"配对码: {display_code}")
                
                exp_at = snap.get("pairing_expires_at")
                if exp_at:
                    now = time.time()
                    if now >= exp_at:
                        self.pair_await_countdown.config(text="已过期", fg="#cf222e")
                    else:
                        remaining = max(0, int(exp_at - now))
                        m, s = divmod(remaining, 60)
                        self.pair_await_countdown.config(text=f"{m}:{s:02d} 后失效", fg="#e67e22")
            else:
                self.pair_await_code_label.config(text="配对码: 等待中...")
                self.pair_await_countdown.config(text="")

        # 2. 同花顺实盘交易卡片状态自适应
        if cs == "INSTALLING":
            self.ths_status_badge.config(text="静默安装中 ⏳", bg="#fffbe6", fg="#e67e22")
            if self.ths_wizard_box.winfo_ismapped():
                self.ths_wizard_box.pack_forget()
            if self.ths_ready_box.winfo_ismapped():
                self.ths_ready_box.pack_forget()
            if self.ths_action_footer_row.winfo_ismapped():
                self.ths_action_footer_row.pack_forget()
            if not self.ths_install_box.winfo_ismapped():
                self.ths_install_box.pack(fill="x", pady=4, borderwidth=0)

            # 更新卡片内静默安装进度条
            if snap.get("install_progress"):
                done, total = snap["install_progress"]
                pct = (done / total * 100) if total > 0 else 0
                self.install_progress_var.set(pct)
                mb_done = done / 1024 / 1024
                mb_total = total / 1024 / 1024
                self.install_pct_lbl.config(text=f"{pct:.0f}%")
                self.install_subtext_lbl.config(text=f"正在下载组件 Tesseract-OCR ({mb_done:.1f}/{mb_total:.1f} MB)...")
        else:
            if self.ths_install_box.winfo_ismapped():
                self.ths_install_box.pack_forget()
            if not self.ths_action_footer_row.winfo_ismapped():
                self.ths_action_footer_row.pack(fill="x", pady=(8, 0))

            ths_steps = snap.get("ths_steps_complete", 0)

            if ths_steps >= 4:
                # 已经完全自检通过
                self.ths_status_badge.config(text="已就绪 🟢", bg="#e6fcf5", fg="#00c800")
                if self.ths_wizard_box.winfo_ismapped():
                    self.ths_wizard_box.pack_forget()
                if not self.ths_ready_box.winfo_ismapped():
                    self.ths_ready_box.pack(fill="x", pady=4)
                
                if snap.get("xiadan_path"):
                    self.ths_done_path_label.config(text=snap["xiadan_path"])
                self.ths_action_hint.config(text="✓ 客户端自检就绪", fg="#00c800")
            else:
                self.ths_status_badge.config(text="等待启动 ⏳", bg="#fffbe6", fg="#e67e22")
                if self.ths_ready_box.winfo_ismapped():
                    self.ths_ready_box.pack_forget()
                if not self.ths_wizard_box.winfo_ismapped():
                    self.ths_wizard_box.pack(fill="x", pady=4)

                self.ths_action_hint.config(text="未启动，请点击唤起客户端", fg="#57606a")

                hints = [
                    "→ 请打开同花顺行情软件并登录",
                    "→ 请点击同花顺右上角「委托」按钮",
                    "→ 请在委托窗口内点击「切换旧版」"
                ]

                # 4 步数据映射到 3 步自检向导
                for step_info in self.ths_steps_display:
                    s_num = step_info["step_num"]
                    circle = step_info["circle"]
                    hint = step_info["hint"]

                    # 已经完成的步骤
                    if s_num <= ths_steps:
                        circle.config(text="✓", bg="#e6fcf5", fg="#00c800", highlightbackground="#00c800")
                        hint.config(text="")
                    # 当前活跃等待步骤
                    elif s_num == ths_steps + 1:
                        circle.config(text=str(s_num), bg="#fffbe6", fg="#e67e22", highlightbackground="#e0b656")
                        hint.config(text=hints[s_num - 1])
                    # 未来的步骤
                    else:
                        circle.config(text=str(s_num), bg="#fafbfc", fg="#999999", highlightbackground="#d0d7de")
                        hint.config(text="")

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

    def _copy_instruction_command(self) -> None:
        """复制完整指令到剪贴板"""
        snap = self.state.snapshot()
        code = snap.get("pairing_code", "")
        if not code:
            self.state.log("⚠ 配对码暂无，无法复制指令")
            return

        instruction = f"打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 {_format_pair_code(code)}"
        self.root.clipboard_clear()
        self.root.clipboard_append(instruction)
        self.state.log(f"已复制接入指令到剪贴板")

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

    def _on_footer_link_click(self, event=None) -> None:
        """官网链接点击处理"""
        import webbrowser
        webbrowser.open("https://guling.pro")
        self.state.log("打开官网：https://guling.pro")

    def _on_close(self) -> None:
        """关闭按钮触发：最小化到托盘或退出"""
        if self._minimize_to_tray:
            self.root.withdraw()
        else:
            if self.on_exit_cb:
                self.on_exit_cb()
            self.root.destroy()

    def show_window(self) -> None:
        """从托盘恢复窗口"""
        self.root.after(0, self._do_show_window)

    def _do_show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def run(self) -> None:
        """阻塞跑 tk mainloop。主线程调用。"""
        self.root.mainloop()
