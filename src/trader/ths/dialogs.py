"""交易弹窗看门人（DialogSentry）：结构化发现-处置-记录 xiadan 弹窗。

设计（docs/superpowers/specs/2026-07-13-ths-dialog-handling-design.md）：
**不读正文猜语义**。决策只依赖两个结构信号：

1. 弹窗含 ``Edit`` 输入框 → 验证码/身份验证类，必须输入内容才能通过 →
   交给 backend.input_ocr()，绝不盲点按钮；
2. 否则按按钮标签的肯定优先级（是 > 确定 > 确认 > 同意 > 唯一按钮）
   ``PostMessage(BM_CLICK)`` 点掉；找不到可点按钮时退而给弹窗发 Enter，
   再不行 ``WM_CLOSE``。

安全性不靠读懂弹窗，靠两条：处置窗口仅限我们自己发起的动作前后；
以及点完后照旧走成交表/委托表核实回执。每个被处置的弹窗的
标题 + 全文 + 所采取动作都记录进返回值（进而进回执与日志）——
调用方永远知道流程中间发生过什么。

本模块 Windows-only 部分全部惰性引用 win32 模块；纯决策函数
（choose_button / extract_entrust_no）无平台依赖，可在任意平台单测。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .const import DIALOG_AFFIRM_LABELS, DIALOG_ENTRUST_NO_RE

if platform.system() == "Windows":
    import win32api
    import win32con
    import win32gui
    import win32process

logger = logging.getLogger(__name__)


def normalize_button_label(raw: str) -> str:
    """按钮文本归一化："是(Y)" / "确 定" / "确定(&O)" → "是" / "确定"。"""
    s = (raw or "").strip()
    s = re.sub(r"[\(（]\s*&?[A-Za-z]\s*[\)）]\s*$", "", s)
    return s.replace("&", "").replace(" ", "").strip()


def choose_button(labels: list[str]) -> Optional[str]:
    """从归一化按钮标签中选出要点击的肯定项。

    优先级 DIALOG_AFFIRM_LABELS（是 > 确定 > 确认 > 同意）；都没有但
    只有一个按钮时点它（信息框的唯一按钮无论叫什么都等价于关闭）；
    多个按钮且无肯定项 → None（交给 Enter/WM_CLOSE 兜底）。
    """
    for want in DIALOG_AFFIRM_LABELS:
        if want in labels:
            return want
    if len(labels) == 1:
        return labels[0]
    return None


def extract_entrust_no(text: str) -> Optional[str]:
    """从弹窗全文机会性提取合同编号（拿不到不算失败，回查兜底）。"""
    m = re.search(DIALOG_ENTRUST_NO_RE, text or "")
    return m.group(1) if m else None


@dataclass
class DialogFingerprint:
    """一个弹窗的结构指纹：定位 + 存证所需的全部信息。"""

    hwnd: int
    title: str
    text: str                       # 所有 Static 文本按行合并
    buttons: dict[str, int] = field(default_factory=dict)  # 归一化标签 → hwnd
    has_edit: bool = False          # 含输入框 = 验证码类，不可盲点


@dataclass
class PumpResult:
    """pump() 的汇总结果，供下单/撤单路径充实回执。"""

    dialogs: list[dict] = field(default_factory=list)  # {title, text, action}
    entrust_no: Optional[str] = None

    def attach_to(self, receipt: dict) -> dict:
        """把弹窗存证挂到回执上（无弹窗则不加字段，保持回执干净）。"""
        if self.dialogs:
            receipt["dialogs"] = self.dialogs
        return receipt

    @property
    def texts(self) -> list[str]:
        return [d["text"] or d["title"] for d in self.dialogs if d.get("text") or d.get("title")]


class DialogSentry:
    """围绕一个 WinThsBackend 的弹窗发现与处置。"""

    def __init__(self, backend):
        self.backend = backend

    # ---- 发现 -----------------------------------------------------------

    def scan(self) -> list[DialogFingerprint]:
        """枚举 xiadan 主窗口线程的顶层可见 enabled 弹窗（排除主窗口）。"""
        hwnd_main = getattr(self.backend, "hwnd_main", None)
        if not hwnd_main:
            return []
        tid, _pid = win32process.GetWindowThreadProcessId(hwnd_main)
        tops: list[int] = []
        try:
            win32gui.EnumThreadWindows(tid, lambda h, acc: acc.append(h) or True, tops)
        except Exception:
            logger.exception("EnumThreadWindows failed")
            return []
        out = []
        for h in tops:
            try:
                if h == hwnd_main:
                    continue
                if not (win32gui.IsWindowVisible(h) and win32gui.IsWindowEnabled(h)):
                    continue
                out.append(self._fingerprint(h))
            except Exception:
                logger.exception("fingerprint failed hwnd=%s", hex(h))
        return out

    def _fingerprint(self, hwnd: int) -> DialogFingerprint:
        title = win32gui.GetWindowText(hwnd) or ""
        texts: list[str] = []
        buttons: dict[str, int] = {}
        has_edit = False

        def walker(h, _):
            nonlocal has_edit
            try:
                cls = win32gui.GetClassName(h)
                if cls == "Static":
                    t = _get_text(h).strip()
                    if t:
                        texts.append(t)
                elif cls == "Button":
                    label = normalize_button_label(win32gui.GetWindowText(h))
                    if label and label not in buttons:
                        buttons[label] = h
                elif cls == "Edit":
                    has_edit = True
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(hwnd, walker, None)
        except Exception:
            pass  # 无子控件的自绘弹窗：指纹只有标题，动作走 Enter/WM_CLOSE 兜底
        return DialogFingerprint(hwnd=hwnd, title=title, text="\n".join(texts),
                                 buttons=buttons, has_edit=has_edit)

    # ---- 处置 -----------------------------------------------------------

    def dismiss(self, dlg: DialogFingerprint) -> str:
        """按结构规则处置一个弹窗，返回所采取的动作（进存证）。

        原则（2026-07-13 用户定）：任何弹窗都以**肯定**方式快速消除、回到既定
        轨道，不耦合弹窗内容。肯定优先级：点「是/确定」按钮（=精确版回车，
        不依赖焦点与默认按钮设定）→ 向弹窗投递回车（真机验证对新版自绘提示框
        有效）→ 两次回车仍在才 WM_CLOSE 兜底。**禁止 ESC**——在下单/撤单
        确认框上 ESC 语义是「否/取消」。
        """
        if dlg.has_edit:
            # 验证码/身份验证：必须输入内容才能通过，回车关不掉 →
            # 交给既有 OCR 流程（内部自带重试）。
            self.backend.input_ocr()
            return "input_ocr"
        label = choose_button(list(dlg.buttons))
        if label:
            win32api.PostMessage(dlg.buttons[label], win32con.BM_CLICK, 0, 0)
            return f"click:{label}"
        # 无可用按钮标签（自绘弹窗）：回车 = 默认按钮（肯定）。投递给弹窗
        # 本身而非全局按键——不依赖弹窗是否前台，也绝不会敲进别的窗口。
        for attempt in (1, 2):
            win32api.PostMessage(dlg.hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            win32api.PostMessage(dlg.hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
            time.sleep(0.2)
            if not (_safe_is_window(dlg.hwnd) and win32gui.IsWindowVisible(dlg.hwnd)):
                return "enter" if attempt == 1 else "enter*2"
        # 连回车都消不掉的弹窗几乎不可能是确认框 → 关窗兜底（≈点X），大声留痕。
        logger.warning("dialog ignores Enter, WM_CLOSE fallback title=%r", dlg.title)
        win32api.PostMessage(dlg.hwnd, win32con.WM_CLOSE, 0, 0)
        return "enter*2+wm_close"

    def pump(self, budget: float = 5.0, settle: float = 0.3) -> PumpResult:
        """提交动作后的「等待-发现-处置」循环，取代盲按 Enter。

        连续 ``settle`` 秒无弹窗即认为落定提前返回；总预算 ``budget`` 秒。
        每个处置过的弹窗都截图存证（work_dir）并记录标题/全文/动作。
        """
        result = PumpResult()
        deadline = time.time() + budget
        quiet_since: Optional[float] = None
        handled: dict[int, float] = {}  # hwnd → 上次处置时刻（防对同一弹窗连点）
        while time.time() < deadline:
            # 代次检查点：dispatcher 超时放锁后，脱缰线程不能继续抢弹窗——
            # 下一笔调用的 dialog_cleanup 正在处置同一个框（2026-08-03 串线事故）。
            check = getattr(self.backend, "_abort_if_stale", None)
            if check:
                check("dialogs.pump")
            dialogs = self.scan()
            if not dialogs:
                now = time.time()
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= settle:
                    break
                time.sleep(0.05)
                continue
            quiet_since = None
            for dlg in dialogs:
                last = handled.get(dlg.hwnd, 0.0)
                if time.time() - last < 0.5:
                    continue  # 刚点过，给它时间消失
                self._snapshot(dlg)
                action = self.dismiss(dlg)
                handled[dlg.hwnd] = time.time()
                logger.info("dialog handled title=%r action=%s text=%r",
                            dlg.title, action, dlg.text[:200])
                result.dialogs.append(
                    {"title": dlg.title, "text": dlg.text, "action": action})
                if not result.entrust_no:
                    result.entrust_no = extract_entrust_no(dlg.text)
            time.sleep(0.1)
        return result

    def cleanup(self) -> PumpResult:
        """degraded 自愈：清掉残留弹窗（同一套「肯定+存证」规则）。

        与 pump 的区别只有预算更短——此时上一笔已按 unknown 上报，
        调用方被要求核单，无论弹窗被肯定还是关闭，真相都以核单为准。
        """
        return self.pump(budget=2.0, settle=0.2)

    def _snapshot(self, dlg: DialogFingerprint) -> None:
        """处置前截图存证（尽力而为）。"""
        try:
            from . import win as _win
            path = os.path.join(_win.work_dir,
                                f"dialog_{int(time.time() * 1000)}_{dlg.hwnd:x}.png")
            self.backend.capture_window(dlg.hwnd, path)
        except Exception:
            logger.debug("dialog snapshot failed", exc_info=True)


def _get_text(hwnd: int) -> str:
    import ctypes
    import win32con as _wc
    u32 = ctypes.windll.user32
    n = u32.SendMessageW(hwnd, _wc.WM_GETTEXTLENGTH, 0, 0)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.SendMessageW(hwnd, _wc.WM_GETTEXT, n + 1, ctypes.byref(buf))
    return buf.value


def _safe_is_window(hwnd: int) -> bool:
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False
