"""Tkinter 弹窗：配对码、状态窗"""
import asyncio
import logging
import threading
import tkinter as tk
from tkinter import messagebox

logger = logging.getLogger(__name__)


class PairingCodeDialog:
    """配对码弹窗：显示码、倒计时、一键复制"""

    def __init__(self, root: tk.Tk, pairing_code: str, expires_seconds: int = 300):
        self.root = root
        self.pairing_code = pairing_code
        self.expires_seconds = expires_seconds
        self.window: tk.Toplevel | None = None
        self.remaining = expires_seconds
        self.closed = False

    def show(self) -> None:
        """显示弹窗"""
        if self.window is not None:
            return

        self.window = tk.Toplevel(self.root)
        self.window.title("配对码")
        self.window.geometry("300x150")
        self.window.resizable(False, False)

        self.window.bind("<Destroy>", lambda _: setattr(self, "closed", True))

        frame = tk.Frame(self.window, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        label = tk.Label(frame, text="请在 AI 对话中输入以下配对码：", font=("微软雅黑", 10))
        label.pack()

        code_frame = tk.Frame(frame)
        code_frame.pack(pady=10)

        code_label = tk.Label(
            code_frame, text=self.pairing_code, font=("Courier New", 16, "bold")
        )
        code_label.pack(side=tk.LEFT, padx=5)

        copy_btn = tk.Button(
            code_frame,
            text="复制",
            command=self._copy_code,
        )
        copy_btn.pack(side=tk.LEFT, padx=5)

        self.timer_label = tk.Label(frame, text=f"剩余时间：{self.remaining}s", font=("微软雅黑", 10))
        self.timer_label.pack()

        close_btn = tk.Button(frame, text="关闭", command=self._close)
        close_btn.pack(pady=10)

        self._update_timer()

    def _copy_code(self) -> None:
        """复制配对码到剪贴板"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.pairing_code)
            self.root.update()
            messagebox.showinfo("已复制", f"配对码已复制：{self.pairing_code}")
        except Exception as e:
            logger.error("复制失败：%s", e)
            messagebox.showerror("复制失败", str(e))

    def _update_timer(self) -> None:
        """更新倒计时"""
        if self.closed or self.window is None:
            return

        if self.remaining <= 0:
            self._close()
            return

        self.remaining -= 1
        self.timer_label.config(text=f"剩余时间：{self.remaining}s")
        if self.window:
            self.window.after(1000, self._update_timer)

    def _close(self) -> None:
        """关闭弹窗"""
        if self.window is not None:
            self.window.destroy()
            self.window = None
        self.closed = True


class StatusWindow:
    """状态窗：显示连接状态和账户信息"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.window: tk.Toplevel | None = None

    def show(self, state: str, account_name: str = "", last_seen: str = "") -> None:
        """显示状态窗"""
        if self.window is not None:
            self.window.destroy()

        self.window = tk.Toplevel(self.root)
        self.window.title("连接状态")
        self.window.geometry("300x150")
        self.window.resizable(False, False)

        frame = tk.Frame(self.window, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        state_label = tk.Label(frame, text=f"状态：{state}", font=("微软雅黑", 10))
        state_label.pack(anchor=tk.W)

        if account_name:
            account_label = tk.Label(frame, text=f"账户：{account_name}", font=("微软雅黑", 10))
            account_label.pack(anchor=tk.W, pady=5)

        if last_seen:
            last_label = tk.Label(frame, text=f"最后心跳：{last_seen}", font=("微软雅黑", 10))
            last_label.pack(anchor=tk.W, pady=5)

        close_btn = tk.Button(frame, text="关闭", command=self.window.destroy)
        close_btn.pack(pady=10)


class ExistingInstallDialog:
    """已有 xiadan 检测弹窗"""

    def __init__(self, root: tk.Tk, detected_path: str):
        self.root = root
        self.detected_path = detected_path
        self.choice: str | None = None
        self.window: tk.Toplevel | None = None

    def show(self) -> str | None:
        """显示弹窗，返回 "use_existing" 或 "install_private" 或 None（关闭）"""
        self.window = tk.Toplevel(self.root)
        self.window.title("检测到已有同花顺安装")
        self.window.geometry("550x400")
        self.window.resizable(False, False)

        # 标题
        title_label = tk.Label(
            self.window,
            text="检测到已有同花顺安装",
            font=("微软雅黑", 14, "bold"),
            fg="darkblue",
        )
        title_label.pack(pady=10)

        # 路径显示
        path_frame = tk.Frame(self.window)
        path_frame.pack(fill=tk.X, padx=20, pady=5)
        path_label = tk.Label(
            path_frame,
            text=f"路径：{self.detected_path}",
            font=("微软雅黑", 9),
            wraplength=500,
            justify=tk.LEFT,
        )
        path_label.pack(anchor=tk.W)

        # 说明文本
        text_frame = tk.Frame(self.window)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        text = tk.Text(text_frame, height=15, width=60, font=("微软雅黑", 9), wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True)

        content = """guling-trader 推荐安装一份独立的同花顺到私有目录，跟你已有的完全隔离。这样可以：

✓ 你的程序文件、自选股、方案库、账户配置 不会被改动
✓ trader 升级、卸载都在自己目录里
✓ trader 在操作时不会干扰你正在用的同花顺

但安装过程中可能改动的（Windows installer 标准行为）：

⚠ 开始菜单 "同花顺" 快捷方式指向新版本
⚠ 桌面图标（如果原来有）被新版本覆盖
⚠ 文件关联（.tnc 等）被新版本接管

这些都可以在卸载后手动恢复，不影响数据。"""
        text.insert("1.0", content)
        text.config(state=tk.DISABLED)

        # 按钮
        btn_frame = tk.Frame(self.window)
        btn_frame.pack(fill=tk.X, padx=20, pady=10)

        use_existing_btn = tk.Button(
            btn_frame,
            text="使用已有的（共享设置）",
            command=lambda: self._on_choice("use_existing"),
            width=20,
            bg="#FFA500",
            fg="white",
            font=("微软雅黑", 10),
        )
        use_existing_btn.pack(side=tk.LEFT, padx=5)

        install_private_btn = tk.Button(
            btn_frame,
            text="安装私有副本（推荐）",
            command=lambda: self._on_choice("install_private"),
            width=20,
            bg="#4CAF50",
            fg="white",
            font=("微软雅黑", 10),
        )
        install_private_btn.pack(side=tk.LEFT, padx=5)

        self.window.transient(self.root)
        self.window.grab_set()
        self.root.wait_window(self.window)

        return self.choice

    def _on_choice(self, choice: str) -> None:
        """处理选择"""
        self.choice = choice
        self.window.destroy()


class InstallProgressWindow:
    """下载安装进度窗"""

    def __init__(self, root: tk.Tk, title: str = "正在安装同花顺"):
        self.root = root
        self.title = title
        self.window: tk.Toplevel | None = None
        self.progress_var: tk.DoubleVar | None = None
        self.status_label: tk.Label | None = None
        self.percent_label: tk.Label | None = None
        self.cancel_event = threading.Event()

    def show(self) -> None:
        """显示进度窗"""
        self.window = tk.Toplevel(self.root)
        self.window.title(self.title)
        self.window.geometry("400x150")
        self.window.resizable(False, False)

        frame = tk.Frame(self.window, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        self.status_label = tk.Label(
            frame,
            text="正在下载...",
            font=("微软雅黑", 10),
        )
        self.status_label.pack(anchor=tk.W, pady=5)

        self.progress_var = tk.DoubleVar()
        progress_bar = tk.Canvas(frame, height=20, bg="#E0E0E0", highlightthickness=0)
        progress_bar.pack(fill=tk.X, pady=5)

        self._progress_canvas = progress_bar

        self.percent_label = tk.Label(
            frame,
            text="0%",
            font=("微软雅黑", 10),
        )
        self.percent_label.pack(anchor=tk.W)

        # Cancel 按钮（可选）
        # cancel_btn = tk.Button(frame, text="取消", command=self.cancel)
        # cancel_btn.pack(pady=10)

        self.window.transient(self.root)

    def update_progress(self, percent: int, message: str = "") -> None:
        """更新进度"""
        if not self.window or not self._progress_canvas:
            return

        self.percent_label.config(text=f"{percent}%")

        if message:
            self.status_label.config(text=message)

        # 绘制进度条
        w = self._progress_canvas.winfo_width()
        if w <= 1:
            w = 360
        filled_width = int(w * percent / 100)
        self._progress_canvas.delete("all")
        self._progress_canvas.create_rectangle(0, 0, filled_width, 20, fill="#4CAF50", outline="")

        self.window.update()

    def close(self) -> None:
        """关闭窗口"""
        if self.window:
            self.window.destroy()
            self.window = None

    def cancel(self) -> None:
        """取消操作"""
        self.cancel_event.set()
        self.close()


class UpgradeAvailableDialog:
    """升级可用弹窗"""

    def __init__(self, root: tk.Tk, current_ver: str, latest_ver: str):
        self.root = root
        self.current_ver = current_ver
        self.latest_ver = latest_ver
        self.choice: bool | None = None
        self.window: tk.Toplevel | None = None

    def show(self) -> bool:
        """显示弹窗，返回 True=升级 或 False=跳过"""
        self.window = tk.Toplevel(self.root)
        self.window.title("同花顺升级")
        self.window.geometry("400x180")
        self.window.resizable(False, False)

        frame = tk.Frame(self.window, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        title_label = tk.Label(
            frame,
            text="检测到同花顺新版本",
            font=("微软雅黑", 12, "bold"),
            fg="darkblue",
        )
        title_label.pack(pady=10)

        message = tk.Label(
            frame,
            text=f"当前版本：{self.current_ver}\n最新版本：{self.latest_ver}\n\n是否升级？",
            font=("微软雅黑", 10),
            justify=tk.CENTER,
        )
        message.pack(pady=10)

        btn_frame = tk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=10)

        yes_btn = tk.Button(
            btn_frame,
            text="升级",
            command=lambda: self._on_choice(True),
            width=10,
            bg="#4CAF50",
            fg="white",
            font=("微软雅黑", 10),
        )
        yes_btn.pack(side=tk.LEFT, padx=5)

        no_btn = tk.Button(
            btn_frame,
            text="跳过",
            command=lambda: self._on_choice(False),
            width=10,
            bg="#666666",
            fg="white",
            font=("微软雅黑", 10),
        )
        no_btn.pack(side=tk.LEFT, padx=5)

        self.window.transient(self.root)
        self.window.grab_set()
        self.root.wait_window(self.window)

        return self.choice if self.choice is not None else False

    def _on_choice(self, choice: bool) -> None:
        """处理选择"""
        self.choice = choice
        self.window.destroy()
