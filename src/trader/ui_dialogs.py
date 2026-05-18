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
