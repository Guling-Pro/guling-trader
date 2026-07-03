"""Win32 automation against THS independent broker client (xiadan.exe).

Refactored from upstream `thsauto1.py` (https://github.com/crazyAttributor/ths-auto-trade).
**Behavior preserved verbatim** — the empirical control IDs, hotkey sequences,
OCR crop coordinates, and result-parsing strings are the actual value of the
original project, hard-won against the 申万宏源 xiadan.exe build. Do not "tidy"
them without testing against your own broker first.

Changes from upstream:
- `window_title` and `tesseract_cmd` now read from `Config` at `setup(config)`
  time, instead of module-level constants.
- `print(...)` replaced with `logger.{info,debug}`.
- No other logic edits.

This module is Windows-only (imports pywin32). Do **not** import it from
yu-agent.
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import logging
import os
import platform
import re
import sys
import tempfile
import time
from typing import Any, Optional

import pytesseract
from PIL import Image, ImageFilter

if platform.system() == "Windows":
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    import win32process
    import win32ui

from .const import BALANCE_CONTROL_ID_GROUP, VK_CODE

logger = logging.getLogger(__name__)

# PyInstaller bundled Tesseract 路径绑定（仅 onefile 模式激活）
if hasattr(sys, "_MEIPASS"):
    _tess_exe = os.path.join(sys._MEIPASS, "tesseract", "tesseract.exe")
    _tess_data_dir = os.path.join(sys._MEIPASS, "tesseract", "tessdata")
    if os.path.exists(_tess_exe):
        pytesseract.pytesseract.tesseract_cmd = _tess_exe
        os.environ["TESSDATA_PREFIX"] = _tess_data_dir
        logger.info("✓ Bundled Tesseract 已绑定：%s", _tess_exe)

# Tunables — kept identical to upstream.
sleep_time = 0.2
short_sleep_time = 0.05
refresh_sleep_time = 0.5
retry_time = 1

# Set by `setup()` from Config. Module-level so the existing call sites
# (`win32gui.FindWindow(None, window_title)`) keep working without threading
# config through every method.
window_title: str = "网上股票交易系统5.0"

# OCR 临时截图的落地目录。默认系统临时目录；setup() 可改成 exe 同级的 tmp/，
# 避免在 exe 当前目录乱丢 ocr.png / ocr_proc.png。
work_dir: str = tempfile.gettempdir()


def setup(window_title_value: str, tesseract_cmd: str, work_dir_value: str = "") -> None:
    """Apply runtime configuration. Call once at server startup before using the backend."""
    global window_title, work_dir
    window_title = window_title_value
    if work_dir_value:
        work_dir = work_dir_value
        try:
            os.makedirs(work_dir, exist_ok=True)
        except Exception as e:
            logger.warning("create work_dir %r failed: %s", work_dir, e)
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    logger.info(
        "thsauto setup: window_title=%r tesseract_cmd=%r work_dir=%r",
        window_title,
        tesseract_cmd or "<from PATH>",
        work_dir,
    )


def find_window_by_title_prefix(prefix: str) -> int:
    """Find first visible top-level window whose title **starts with** `prefix`.

    Hexin clients typically render `<base> - <broker> - <hint>` so exact-match
    `FindWindow` fails. Prefix match handles the broker suffix while staying
    safer than substring (avoids accidental matches in unrelated windows).
    """
    if not prefix:
        return 0
    matches: list[tuple[int, str]] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        text = win32gui.GetWindowText(hwnd)
        if text.startswith(prefix):
            matches.append((hwnd, text))

    win32gui.EnumWindows(cb, None)
    if not matches:
        return 0
    if len(matches) > 1:
        logger.warning(
            "found %d windows matching prefix %r — picking first: %r",
            len(matches),
            prefix,
            [t for _, t in matches],
        )
    hwnd, full = matches[0]
    logger.info("matched window prefix=%r → full_title=%r hwnd=%s", prefix, full, hwnd)
    return hwnd


def get_clipboard_data():
    """读剪贴板文本。永不抛异常——失败返回 None，让调用方的重试循环继续。

    OpenClipboard 在别的进程占用剪贴板时会抛（拷贝表格 + 拷贝数据验证码弹窗期间
    尤其常见）；GetClipboardData 在 CF_UNICODETEXT 格式还没就绪时也会抛。这些都不
    该让整个读取崩掉（之前交割单近三月就是崩在这里）。
    """
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return None
        return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    except Exception:
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def hot_key(keys):
    time.sleep(sleep_time)
    for key in keys:
        win32api.keybd_event(VK_CODE[key], 0, 0, 0)
        time.sleep(short_sleep_time)
    for key in reversed(keys):
        win32api.keybd_event(VK_CODE[key], 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(short_sleep_time)


def _activate_window(hwnd):
    """Best-effort foreground activation.

    Why a helper: Windows blocks SetForegroundWindow when the calling
    process isn't already foreground / no recent user input — it raises
    pywintypes.error('No error message available'), which crashes
    switch_to_normal / set_text / cancel paths. SwitchToThisWindow is
    undocumented but permissive (active_mian_window relies on it); fall
    back to it, then swallow if even that fails. Callers only need the
    window visible enough to receive subsequent clicks; the foreground
    contract was always best-effort anyway.
    """
    try:
        win32gui.SetForegroundWindow(hwnd)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
    except Exception as e:
        logger.debug("activate_window swallowed: %s", e)


def _force_ime_english(hwnd):
    """Close the IME on hwnd's thread so keybd_event injects raw ASCII.

    Why: when the OS is in Chinese pinyin mode, alphabetic keystrokes get
    intercepted as pinyin (e.g. 'm','b' → 拼音候选 like 目标/明白) instead of
    landing in the Edit control. This bites the captcha popup hardest, since
    its codes are letters+digits.
    """
    try:
        himc = ctypes.windll.imm32.ImmGetContext(hwnd)
        if himc:
            try:
                ctypes.windll.imm32.ImmSetOpenStatus(himc, False)
            finally:
                ctypes.windll.imm32.ImmReleaseContext(hwnd, himc)
    except Exception as e:
        logger.debug("force_ime_english failed: %s", e)


def set_text(hwnd, string, isPrice=False):
    _activate_window(hwnd)
    _force_ime_english(hwnd)
    win32api.SendMessage(hwnd, win32con.EM_SETSEL, 0, -1)
    if isPrice:
        rect = win32gui.GetWindowRect(hwnd)
        x, y, w, h = rect
        center_x = x + (w - x) // 2
        center_y = y + (h - y) // 2
        win32api.SetCursorPos((center_x, center_y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, center_x, center_y, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, center_x, center_y, 0, 0)
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, center_x, center_y, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, center_x, center_y, 0, 0)

    win32api.keybd_event(VK_CODE["backspace"], 0, 0, 0)
    time.sleep(short_sleep_time)
    win32api.keybd_event(VK_CODE["backspace"], 0, win32con.KEYEVENTF_KEYUP, 0)

    for char in string:
        if char.isupper():
            win32api.keybd_event(0xA0, 0, 0, 0)
            win32api.keybd_event(VK_CODE[char.lower()], 0, 0, 0)
            win32api.keybd_event(VK_CODE[char.lower()], 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(0xA0, 0, win32con.KEYEVENTF_KEYUP, 0)
        else:
            win32api.keybd_event(VK_CODE[char], 0, 0, 0)
            win32api.keybd_event(VK_CODE[char], 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.1)


def get_text(hwnd):
    length = ctypes.windll.user32.SendMessageW(hwnd, win32con.WM_GETTEXTLENGTH)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.SendMessageW(hwnd, win32con.WM_GETTEXT, length, ctypes.byref(buf))
    return buf.value


_PHANTOM_VALUES = frozenset({"", "0", "0.0", "0.00", "0.000", "-", "--"})


def parse_table(text):
    """Parse THS clipboard table. Drops two kinds of noise rows:
    - completely blank lines (trailing \\t\\r\\n separator artefact)
    - phantom placeholder rows where every cell is empty / zero / dash —
      THS pads its UI tables with empty rows when there's no data, and
      Ctrl+C copies those rows verbatim. A real order/position always has
      at least one non-zero, non-empty cell (a stock code, a timestamp,
      or a non-zero numeric).

    Also tolerates rows with fewer cells than the header (audit flagged
    IndexError); short rows fill missing cells with ``""``.
    """
    lines = text.split("\t\r\n")
    if not lines:
        return []
    keys = lines[0].split("\t")
    result = []
    for i in range(1, len(lines)):
        raw = lines[i]
        if not raw.strip("\t").strip():
            continue
        items = raw.split("\t")
        info = {keys[j]: (items[j] if j < len(items) else "") for j in range(len(keys))}
        if all(str(v).strip() in _PHANTOM_VALUES for v in info.values()):
            continue
        result.append(info)
    return result


# --- TreeView (SysTreeView32) 跨进程消息常量 ----------------------------------
# 用消息按"文字"定位/选中左侧查询树节点（如「交割单」），而非按像素点击 ——
# 与窗口缩放 / DPI / 行高无关。
_TV_FIRST = 0x1100
TVM_GETNEXTITEM = _TV_FIRST + 10
TVM_GETITEMW = _TV_FIRST + 62
TVM_SELECTITEM = _TV_FIRST + 11
TVM_GETITEMRECT = _TV_FIRST + 4
TVGN_ROOT = 0x0000
TVGN_NEXT = 0x0001
TVGN_CHILD = 0x0004
TVGN_CARET = 0x0009
TVIF_TEXT = 0x0001


if platform.system() == "Windows":
    class _TVITEMW(ctypes.Structure):
        _fields_ = [
            ("mask", ctypes.c_uint),
            ("hItem", ctypes.c_ssize_t),       # HTREEITEM（指针大小）
            ("state", ctypes.c_uint),
            ("stateMask", ctypes.c_uint),
            ("pszText", ctypes.c_void_p),
            ("cchTextMax", ctypes.c_int),
            ("iImage", ctypes.c_int),
            ("iSelectedImage", ctypes.c_int),
            ("cChildren", ctypes.c_int),
            ("lParam", ctypes.c_ssize_t),
        ]


class WinThsBackend:
    def __init__(self):
        self.hwnd_main = None
        # order_watch 与 RPC 共用：串行化对 THS 单窗口的访问，避免并发 copy_table。
        self.win_lock = asyncio.Lock()
        # agent 经 RPC 下单成功后登记的合同编号，供 order_watch 标记事件来源。
        self.agent_entrust_nos: set[str] = set()

    def _ensure_bound(self) -> dict[str, Any] | None:
        """检查是否已绑定；否则 lazy bind，返回错误 dict 或 None（成功）"""
        # 关键：缓存句柄必须验活。xiadan 重启后旧 hwnd 数值仍 >0 但窗口已销毁，
        # 不验活会拿死句柄去 SendMessage/FindWindowEx → Win32 报错 1400「无效的窗口句柄」。
        # IsWindow 判断句柄是否仍指向存活窗口；标题前缀校验顺带防 HWND 数值被系统回收复用。
        if self.hwnd_main and self.hwnd_main > 0:
            try:
                alive = bool(win32gui.IsWindow(self.hwnd_main)) and win32gui.GetWindowText(
                    self.hwnd_main
                ).startswith(window_title)
            except Exception:
                alive = False
            if alive:
                return None  # 已绑定且句柄有效
            logger.info(
                "缓存的 xiadan 句柄 %s 已失效（疑似重启/重登），重新捕获…", self.hwnd_main
            )
            self.hwnd_main = None  # 丢弃失效句柄，强制重绑

        # 尝试 bind
        logger.info("未检测到 xiadan 窗口，尝试 lazy bind...")
        self.bind_client()
        if self.hwnd_main and self.hwnd_main > 0:
            logger.info("✓ 成功绑定到 xiadan 窗口: hwnd=%s", self.hwnd_main)
            return None

        # bind 失败
        logger.error("✗ 未检测到 xiadan 窗口（window_title 为空或窗口未运行）")
        return {"code": 1, "error": "未检测到 xiadan 窗口（请确保同花顺已打开并登录）"}

    def bind_client(self):
        # Try exact match first for backward compat, then prefix match.
        hwnd = win32gui.FindWindow(None, window_title)
        if hwnd <= 0:
            hwnd = find_window_by_title_prefix(window_title)
        if hwnd > 0:
            _activate_window(hwnd)
            self.hwnd_main = hwnd

    def kill_client(self):
        self.hwnd_main = None
        retry = 5
        while retry > 0:
            hwnd = win32gui.FindWindow(None, window_title)
            if hwnd <= 0:
                hwnd = find_window_by_title_prefix(window_title)
            if hwnd == 0:
                time.sleep(1)
                break
            else:
                _activate_window(hwnd)
                time.sleep(sleep_time)
                hot_key(["alt", "F4"])
                retry -= 1

    def get_tree_hwnd(self):
        hwnd = self.hwnd_main
        hwnd = win32gui.FindWindowEx(hwnd, None, "AfxMDIFrame140s", None)
        hwnd = win32gui.FindWindowEx(hwnd, None, "AfxWnd140s", None)
        hwnd = win32gui.FindWindowEx(hwnd, None, None, "HexinScrollWnd")
        hwnd = win32gui.FindWindowEx(hwnd, None, "AfxWnd140s", None)
        hwnd = win32gui.FindWindowEx(hwnd, None, "SysTreeView32", None)
        return hwnd

    def get_right_hwnd(self):
        hwnd = self.hwnd_main
        hwnd = win32gui.FindWindowEx(hwnd, None, "AfxMDIFrame140s", None)
        hwnd = win32gui.GetDlgItem(hwnd, 0xE901)
        return hwnd

    def get_left_bottom_tabs(self):
        hwnd = self.hwnd_main
        hwnd = win32gui.FindWindowEx(hwnd, None, "AfxMDIFrame140s", None)
        hwnd = win32gui.FindWindowEx(hwnd, None, "AfxWnd140s", None)
        hwnd = win32gui.FindWindowEx(hwnd, None, "CCustomTabCtrl", None)
        return hwnd

    def _find_ctrl_by_id(
        self, root: int, cid: int, cls: str | None = None, visible: bool = False
    ) -> int:
        """在 root 的全部子孙里递归找【控件 ID==cid】(可选类名过滤/可见过滤)的第一个，
        找不到返回 0。

        取代只查【直接子控件】的 win32gui.GetDlgItem：新版皮肤给查询/下单面板多套了
        一层父容器，原本是 right_hwnd 直接子的控件(资金字段 0x3F4.. / 表格 0x417 /
        代码价量 0x408~0x40A)变成了孙辈，GetDlgItem 直接子查不到 → 报错 1421。递归
        枚举则无视嵌套层级，新旧版皮肤通吃，这是"无视新旧版本"的核心。

        visible=True：右区常同时挂着多个面板的同 ID 控件(如持仓/未成交/成交各一张
        0x417 表格)，只有当前激活面板的那个可见，用可见性过滤才不会误抓到隐藏面板的。
        """
        if not root:
            return 0
        hit: list[int] = []

        def _wk(h, _):
            try:
                if (
                    win32gui.GetDlgCtrlID(h) == cid
                    and (cls is None or win32gui.GetClassName(h) == cls)
                    and (not visible or win32gui.IsWindowVisible(h))
                ):
                    hit.append(h)
                    return False  # 命中即停止枚举
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(root, _wk, None)
        except Exception:
            pass
        return hit[0] if hit else 0

    def _find_grid(self, root: int) -> int:
        """找面板里的表格控件(0x417)。优先【可见的】CVirtualGridCtrl —— 右区同时挂着
        多个面板的 grid，只有当前激活面板的可见，不按可见性过滤会误读到隐藏的持仓表
        (导致 orders_active/filled 错读成 position)。逐级放宽回退，保证总能拿到一个。"""
        return (
            self._find_ctrl_by_id(root, 0x417, cls="CVirtualGridCtrl", visible=True)
            or self._find_ctrl_by_id(root, 0x417, visible=True)
            or self._find_ctrl_by_id(root, 0x417, cls="CVirtualGridCtrl")
            or self._find_ctrl_by_id(root, 0x417)
        )

    def get_ocr_hwnd(self):
        tid, pid = win32process.GetWindowThreadProcessId(self.hwnd_main)

        def enum_children(hwnd, results):
            try:
                if win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd):
                    win32gui.EnumChildWindows(hwnd, handler, results)
            except Exception:
                return

        def handler(hwnd, results):
            if win32gui.GetClassName(hwnd) == "Static":
                results.append(hwnd)
                return False
            enum_children(hwnd, results)
            return len(results) == 0

        popups = []
        windows = []
        win32gui.EnumThreadWindows(tid, lambda hwnd, l: l.append(hwnd), windows)
        for hwnd in windows:
            if not handler(hwnd, popups):
                break
        for ctrl in popups:
            text = get_text(ctrl)
            if "检测到您正在拷贝数据" in text:
                return ctypes.windll.user32.GetWindow(ctrl, win32con.GW_HWNDNEXT)
        return 0

    # ---- Bulk cancel (撤买/撤卖/全撤/撤最后) -------------------------------
    # Button IDs verified in F3 panel via /debug/controls. They also exist on
    # F1's 持仓 sub-panel with identical IDs, but staying on F3 keeps the
    # confirmation-dialog and captcha flow uniform.
    _BULK_CANCEL_BUTTONS = {
        "all": 0x7531,    # 全撤(Z/)
        "buy": 0x7532,    # 撤买(X)
        "sell": 0x7533,   # 撤卖(C)
        "last": 0x079A,   # 撤最后(G)
    }

    def _bulk_cancel(self, action: str):
        if action not in self._BULK_CANCEL_BUTTONS:
            return {
                "code": 1,
                "status": "failed",
                "msg": f"unknown action {action!r}; expected one of "
                       f"{list(self._BULK_CANCEL_BUTTONS)}",
            }
        btn_id = self._BULK_CANCEL_BUTTONS[action]
        self.switch_to_normal()
        hot_key(["F3"])
        self.refresh()
        right = self.get_right_hwnd()
        try:
            btn = win32gui.GetDlgItem(right, btn_id)
        except Exception as e:
            return {"code": 1, "status": "failed",
                    "msg": f"GetDlgItem 0x{btn_id:04X}: {e}"}
        if not btn:
            return {"code": 1, "status": "failed",
                    "msg": f"button 0x{btn_id:04X} not present in F3 panel"}
        # BM_CLICK fires the button's WM_COMMAND. Cross-process safe.
        win32api.PostMessage(btn, win32con.BM_CLICK, 0, 0)
        time.sleep(sleep_time)
        # xiadan typically pops a "您确定要撤销..." confirmation. The OK button
        # is default-focused; Enter accepts it. If there's no confirmation
        # (e.g. when there's nothing to cancel), Enter is a harmless no-op.
        hot_key(["enter"])
        time.sleep(sleep_time)
        # Handle anti-bot captcha if it appears.
        self.input_ocr()
        return {
            "code": 0,
            "status": "succeed",
            "action": action,
            "button_id": f"0x{btn_id:04X}",
        }

    def cancel_all(self):
        return self._bulk_cancel("all")

    def cancel_buy(self):
        return self._bulk_cancel("buy")

    def cancel_sell(self):
        return self._bulk_cancel("sell")

    def cancel_last(self):
        return self._bulk_cancel("last")

    def get_balance(self):
        self.switch_to_normal()
        hot_key(["F4"])
        self.refresh()
        hwnd = self.get_right_hwnd()
        data = {}
        for key, cid in BALANCE_CONTROL_ID_GROUP.items():
            ctrl = self._find_ctrl_by_id(hwnd, cid)
            if ctrl > 0:
                data[key] = get_text(ctrl)
        return {"code": 0, "status": "succeed", "data": data}

    def get_position(self):
        for retry in range(retry_time):
            self.switch_to_normal()
            hot_key(["F1"])
            hot_key(["F6"])
            self.refresh()
            hwnd = self.get_right_hwnd()
            ctrl = self._find_grid(hwnd)
            self.copy_table(ctrl)
            data = get_clipboard_data()
            if data:
                return {"code": 0, "status": "succeed", "data": parse_table(data)}
            time.sleep(sleep_time)
        return {"code": 1, "status": "failed", "msg": "读取数据失败（可能验证码弹窗或刷新超时），请稍后重试"}

    def get_gupiao(self):
        for retry in range(retry_time):
            self.switch_to_normal()
            hot_key(["F4"])
            self.refresh()
            hwnd = self.get_right_hwnd()
            ctrl = self._find_grid(hwnd)
            self.copy_table(ctrl)
            data = get_clipboard_data()
            if data:
                return {"code": 0, "status": "succeed", "data": parse_table(data)}
            time.sleep(sleep_time)
        return {"code": 1, "status": "failed", "msg": "读取数据失败（可能验证码弹窗或刷新超时），请稍后重试"}

    def get_active_orders(self):
        for retry in range(retry_time):
            self.switch_to_normal()
            _activate_window(self.hwnd_main)
            hot_key(["F1"])
            hot_key(["F8"])
            self.refresh()
            hwnd = self.get_right_hwnd()
            ctrl = self._find_grid(hwnd)
            self.copy_table(ctrl)
            data = get_clipboard_data()
            if data:
                return {"code": 0, "status": "succeed", "data": parse_table(data)}
            time.sleep(sleep_time)
        return {"code": 1, "status": "failed", "msg": "读取数据失败（可能验证码弹窗或刷新超时），请稍后重试"}

    def get_filled_orders(self):
        self.switch_to_normal()
        _activate_window(self.hwnd_main)
        hot_key(["F2"])
        hot_key(["F7"])
        self.refresh()
        hwnd = self.get_right_hwnd()
        ctrl = self._find_grid(hwnd)
        self.copy_table(ctrl)
        data = None
        retry = 0
        while not data and retry < retry_time:
            retry += 1
            time.sleep(sleep_time)
            data = get_clipboard_data()
        if data:
            return {"code": 0, "status": "succeed", "data": parse_table(data)}
        return {"code": 1, "status": "failed", "msg": "读取数据失败（可能验证码弹窗或刷新超时），请稍后重试"}

    # --- 交割单（低频，一次性拉一年做分析）----------------------------------
    def _select_tree_node_by_text(self, target: str, fallback_token: str = "") -> bool:
        """按文字在左侧树（SysTreeView32）找到节点 → 程序化选中 + 真实鼠标点击。

        fallback_token：整串匹配不中时的兜底"独特字"。如交割单传「割」（菜单里仅
        交割单含「割」；不能用「交」——会撞上 当日成交/历史成交）。

        - 读节点文字走 TreeView 跨进程消息（TVM_GETITEM）；定位用文字，缩放无关。
        - **ctypes 指针必须设 restype/argtypes**，否则 64 位地址被截断成 32 位 →
          跨进程读到的全是空文字 → 永远 "not found"（2026-05-21 实测的真因）。
        - 选中后还做一次真实鼠标点击（TVM_GETITEMRECT 取矩形 → 屏幕坐标 → 点击）：
          TVM_SELECTITEM 不一定触发 THS 右侧面板切换，真实点击才稳（对齐 click_kc_*）。
        - 点击坐标在 Per-Monitor-V2 DPI 上下文里换算，HiDPI（Retina/Parallels）下也准。
        """
        tree = self.get_tree_hwnd()
        if not tree:
            logger.warning("settlement: tree hwnd not found")
            return False
        _, pid = win32process.GetWindowThreadProcessId(tree)
        PROCESS_VM = 0x0008 | 0x0010 | 0x0020  # OPERATION | READ | WRITE
        MEM = 0x1000 | 0x2000  # COMMIT | RESERVE
        PAGE_RW = 0x04
        MEM_RELEASE = 0x8000
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        # 64 位指针截断防护（关键）：不设这些，VirtualAllocEx 返回值 / Read/Write 的
        # 地址参数都会被 ctypes 当 32 位 int 处理 → 高位丢失 → 读到错误内存。
        k32.VirtualAllocEx.restype = ctypes.c_void_p
        k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
        k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]

        h_proc = win32api.OpenProcess(PROCESS_VM, False, pid)
        bufsize = 512
        remote_text = k32.VirtualAllocEx(int(h_proc), None, bufsize, MEM, PAGE_RW)
        remote_item = k32.VirtualAllocEx(int(h_proc), None, ctypes.sizeof(_TVITEMW), MEM, PAGE_RW)
        try:
            if not remote_text or not remote_item:
                logger.warning("settlement: VirtualAllocEx failed")
                return False

            def _norm(s: str) -> str:
                # THS 把「交 割 单」「对 帐 单」用空格拉开对齐 → 树节点文字含空格。
                # 匹配前去掉半角/全角空格，否则 "交割单" in "交 割 单" = False。
                return s.replace(" ", "").replace("　", "")

            target_norm = _norm(target)
            fallback_norm = _norm(fallback_token)
            visited: list[str] = []
            fallback_node = 0

            def read_text(hitem: int) -> str:
                item = _TVITEMW()
                item.mask = TVIF_TEXT
                item.hItem = hitem
                item.pszText = remote_text
                item.cchTextMax = bufsize // 2
                k32.WriteProcessMemory(int(h_proc), remote_item,
                                       ctypes.byref(item), ctypes.sizeof(item), None)
                win32gui.SendMessage(tree, TVM_GETITEMW, 0, remote_item)
                buf = (ctypes.c_char * bufsize)()
                k32.ReadProcessMemory(int(h_proc), remote_text, buf, bufsize, None)
                return buf.raw.decode("utf-16-le", "ignore").split("\x00", 1)[0]

            def walk(hitem: int):
                nonlocal fallback_node
                while hitem:
                    txt = read_text(hitem)
                    visited.append(txt)
                    n = _norm(txt)
                    if target_norm in n:
                        return hitem
                    # 记住第一个含兜底字的节点（整串没命中时用）
                    if fallback_norm and fallback_norm in n and not fallback_node:
                        fallback_node = hitem
                    child = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_CHILD, hitem)
                    if child:
                        found = walk(child)
                        if found:
                            return found
                    hitem = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_NEXT, hitem)
                return 0

            node = walk(win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_ROOT, 0))
            if not node and fallback_node:
                logger.info("settlement: 整串未中，用兜底字 %r 命中节点", fallback_token)
                node = fallback_node
            if not node:
                try:
                    tree_cls = win32gui.GetClassName(tree)
                except Exception:
                    tree_cls = "?"
                # 诊断：dump 实际读到的节点文字，区分"空格没去净 / 读到空 / 树不对"
                logger.warning(
                    "settlement: tree node %r not found; tree=%s cls=%s visited(%d)=%r",
                    target, hex(tree), tree_cls, len(visited), visited[:40],
                )
                return False

            win32gui.SendMessage(tree, TVM_SELECTITEM, TVGN_CARET, node)

            # 取节点矩形：把 HTREEITEM 写进 RECT 头 8 字节，再发 TVM_GETITEMRECT。
            k32.WriteProcessMemory(int(h_proc), remote_text,
                                   ctypes.byref(ctypes.c_ssize_t(node)),
                                   ctypes.sizeof(ctypes.c_ssize_t), None)
            got = win32gui.SendMessage(tree, TVM_GETITEMRECT, 0, remote_text)
            if not got:
                logger.info("settlement: selected (no rect, 程序化) tree node %r", target)
                return True
            rect = (wintypes.LONG * 4)()
            k32.ReadProcessMemory(int(h_proc), remote_text, ctypes.byref(rect),
                                  ctypes.sizeof(rect), None)
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2

            DPI_PMv2 = ctypes.c_void_p(-4)
            old_ctx = None
            if hasattr(u32, "SetThreadDpiAwarenessContext"):
                try:
                    old_ctx = u32.SetThreadDpiAwarenessContext(DPI_PMv2)
                except Exception:
                    old_ctx = None
            try:
                pt = wintypes.POINT(cx, cy)
                u32.ClientToScreen(tree, ctypes.byref(pt))
                win32api.SetCursorPos((pt.x, pt.y))
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            finally:
                if old_ctx is not None:
                    try:
                        u32.SetThreadDpiAwarenessContext(old_ctx)
                    except Exception:
                        pass
            time.sleep(sleep_time)
            logger.info("settlement: clicked tree node %r at client(%d,%d)", target, cx, cy)
            return True
        finally:
            if remote_text:
                k32.VirtualFreeEx(int(h_proc), remote_text, 0, MEM_RELEASE)
            if remote_item:
                k32.VirtualFreeEx(int(h_proc), remote_item, 0, MEM_RELEASE)
            win32api.CloseHandle(h_proc)

    def _real_click_hwnd(self, h: int) -> None:
        """对控件做一次真实鼠标点击（取窗口中心 → SetCursorPos → 按下抬起）。

        在 Per-Monitor-V2 DPI 上下文里取坐标，HiDPI 下也准。比 BM_CLICK 更接近用户
        操作，对自绘 tab / 需要真实点击才切换的控件更稳。
        """
        u32 = ctypes.windll.user32
        DPI_PMv2 = ctypes.c_void_p(-4)
        old = None
        if hasattr(u32, "SetThreadDpiAwarenessContext"):
            try:
                old = u32.SetThreadDpiAwarenessContext(DPI_PMv2)
            except Exception:
                old = None
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            cx, cy = (l + r) // 2, (t + b) // 2
            win32api.SetCursorPos((cx, cy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        finally:
            if old is not None:
                try:
                    u32.SetThreadDpiAwarenessContext(old)
                except Exception:
                    pass
        time.sleep(sleep_time)

    def _click_button_by_text(self, text: str) -> bool:
        """找含 `text` 的时段按钮并真实点击。

        实测：交割单/资金股票/历史成交 等多个查询子面板各有一套时段按钮，含同名
        「近三月」的 Button 有 5 个，其中只有交割单面板那个是可见+可用的。必须只点
        **可见且可用**的那个，否则点的是隐藏副本、时段不切（停在默认近一周）。
        """
        cands: list[tuple[int, str, str, bool, bool]] = []

        def walker(h, _):
            try:
                wt = win32gui.GetWindowText(h) or ""
                if text in wt:
                    cands.append((
                        h, win32gui.GetClassName(h), wt,
                        bool(win32gui.IsWindowVisible(h)),
                        bool(win32gui.IsWindowEnabled(h)),
                    ))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(self.hwnd_main, walker, None)
        except Exception as e:
            logger.warning("settlement: EnumChildWindows failed: %s", e)
        logger.info("settlement: 时段 %r 候选=%r",
                    text, [(c, t, v, e) for _, c, t, v, e in cands])
        if not cands:
            logger.warning("settlement: 时段 %r 未找到", text)
            return False
        # 只点可见+可用的（避开隐藏副本）；优先 Button 类
        usable = [m for m in cands if m[3] and m[4]]
        pick = [m for m in usable if m[1] == "Button"] or usable
        if not pick:
            logger.warning("settlement: 时段 %r 有候选但无可见可用项", text)
            return False
        h = pick[0][0]
        self._real_click_hwnd(h)
        logger.info("settlement: 真实点击时段 hwnd=%s text=%r", hex(h), pick[0][2])
        return True

    # 交割单是「查询(F4)」展开后 资金股票 往下数第 8 个子节点：
    #   资金股票(0) 当日委托 当日成交 历史委托 历史成交 历史持仓 资金明细 对帐单 交割单(8)
    # THS 左树文字是回调式，跨进程 TVM_GETITEM 读不到（实测全空），所以不靠文字定位，
    # 改用「F4 落到资金股票 → 给树发 8 次 Down 键消息」走结构定位，纯键盘、无坐标、
    # 无 DPI 问题。最后用列名校验确认确实切到了交割单，绝不把资金股票数据当交割单返回。
    _SETTLEMENT_DOWN_FROM_F4 = 8
    # 交割单独有、资金股票/持仓没有的列，用来校验面板切对了
    _SETTLEMENT_MARKER_COLS = ("发生金额", "成交编号", "印花税", "成交日期")

    def _goto_settlement_panel(self) -> None:
        """F4 进查询（落 资金股票）→ 给左树发 N 次 Down 键 → 选中交割单（触发面板切换）。"""
        self.switch_to_normal()
        _activate_window(self.hwnd_main)
        hot_key(["F4"])  # 查询：默认选中并显示 资金股票
        time.sleep(refresh_sleep_time)
        tree = self.get_tree_hwnd()
        if not tree:
            logger.warning("settlement: tree hwnd not found")
            return
        # 先让左树拿到键盘焦点（跨线程需 AttachThreadInput），方向键才稳被处理。
        try:
            user32 = ctypes.windll.user32
            my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            tgt_tid, _ = win32process.GetWindowThreadProcessId(tree)
            attached = False
            if my_tid != tgt_tid and user32.AttachThreadInput(my_tid, tgt_tid, True):
                attached = True
            try:
                user32.SetFocus(tree)
            finally:
                if attached:
                    user32.AttachThreadInput(my_tid, tgt_tid, False)
        except Exception as e:
            logger.debug("settlement: focus tree failed: %s", e)
        # 给树发 WM_KEYDOWN/UP：每次 Down 下移一项，选中变化触发右侧面板切换
        # （与用户按方向键等效）。无需坐标，DPI 无关。
        for _ in range(self._SETTLEMENT_DOWN_FROM_F4):
            win32gui.SendMessage(tree, win32con.WM_KEYDOWN, win32con.VK_DOWN, 0)
            win32gui.SendMessage(tree, win32con.WM_KEYUP, win32con.VK_DOWN, 0)
            time.sleep(short_sleep_time)
        time.sleep(sleep_time)
        logger.info("settlement: F4 + %d×Down 切到交割单", self._SETTLEMENT_DOWN_FROM_F4)

    def _do_settlement(self, date_range: str = "近一年"):
        """读取交割单（默认近一年）。低频功能，一次性尽量多拿。"""
        try:
            self._goto_settlement_panel()
            # 时段：按文字点「近一年」等按钮（真实 Button，可命中）
            ranged = self._click_button_by_text(date_range)
            # 点完时段，表格要重新查询+刷新，多等一会儿再读，否则会读到过滤前/不完整
            # 的数据（实测近一月只读到 2 条）。
            time.sleep(refresh_sleep_time)
            self.refresh()
            time.sleep(refresh_sleep_time)

            hwnd = self.get_right_hwnd()
            ctrl = self._find_grid(hwnd)
            if not ctrl:
                return {"code": 1, "status": "failed",
                        "msg": "交割单表格控件(0x417)未找到，可能未切到交割单面板"}
            # 拷表 + 读剪贴板可能拿到不完整快照（过滤未落定/竞态）。重拷几次取行数最多
            # 的那次为准。get_clipboard_data 已永不抛异常（失败返回 None）。
            rows: list[dict] = []
            for attempt in range(3):
                self.copy_table(ctrl)
                data = None
                for _ in range(retry_time + 2):
                    time.sleep(sleep_time)
                    data = get_clipboard_data()
                    if data:
                        break
                if data:
                    parsed = parse_table(data)
                    if len(parsed) > len(rows):
                        rows = parsed
                # 行数已稳定（这次没读到更多）就不必再拷
                if attempt >= 1 and data and len(parse_table(data)) <= len(rows):
                    break
                time.sleep(refresh_sleep_time)
            if not rows:
                return {"code": 1, "status": "failed", "msg": "交割单读取为空"}

            # 列名校验：确认确实是交割单面板，避免把资金股票/持仓数据误当交割单返回。
            cols = set(rows[0].keys()) if rows else set()
            is_settlement = any(m in c for m in self._SETTLEMENT_MARKER_COLS for c in cols)
            if rows and not is_settlement:
                logger.warning("settlement: 面板列名不像交割单，cols=%r", list(cols))
                return {"code": 1, "status": "failed",
                        "msg": "未能切到交割单面板（读到的是其它面板），请重试或人工确认",
                        "got_columns": list(cols)}
            return {
                "code": 0,
                "status": "succeed",
                "date_range": date_range,
                "range_applied": ranged,   # False = 用了面板默认时段，需人工确认范围
                "count": len(rows),
                "data": rows,
            }
        except Exception as e:
            logger.exception("settlement failed")
            return {"code": 1, "status": "failed", "msg": f"交割单读取异常: {e}"}

    def _lookup_entrust_no(self, stock_no, op_keyword, amount, price, timeout=8.0):
        """After buy/sell submission, find the freshly-placed order in
        orders/active by matching (code, op, qty, price). Returns entrust_no
        string or None if not found within timeout.

        Replaces the upstream `ocr_rect` approach which read entrust_no by
        OCR-ing a screen region (right-300:right, bottom-21:bottom). That
        region is fragile to xiadan version / DPI / occlusion / dialog
        timing — it reliably failed in our 100-share test even though the
        order was actually placed. orders/active reads the broker's view via
        clipboard which is the source of truth.
        """
        target_price = f"{float(price):.3f}" if price is not None else None
        target_amount = str(int(amount))
        deadline = time.time() + timeout
        last_seen_rows = 0
        while time.time() < deadline:
            result = self.get_active_orders()
            if result.get("code") == 0:
                rows = result.get("data", [])
                last_seen_rows = len(rows)
                candidates = []
                for r in rows:
                    if r.get("证券代码", "").strip() != str(stock_no):
                        continue
                    if op_keyword not in r.get("操作", ""):
                        continue
                    if r.get("委托数量", "").strip() != target_amount:
                        continue
                    if target_price is not None and r.get("委托价格", "").strip() != target_price:
                        continue
                    # Skip already-cancelled phantom rows.
                    if "已撤" in r.get("备注", ""):
                        continue
                    candidates.append(r)
                if candidates:
                    candidates.sort(
                        key=lambda r: int(r.get("合同编号", "0") or 0),
                        reverse=True,
                    )
                    eno = candidates[0].get("合同编号", "").strip()
                    if eno:
                        logger.info(
                            "lookup_entrust_no matched stock=%s op=%s qty=%s price=%s -> %s",
                            stock_no, op_keyword, target_amount, target_price, eno,
                        )
                        return eno
            time.sleep(0.3)
        logger.warning(
            "lookup_entrust_no timeout stock=%s op=%s qty=%s price=%s rows_last=%d",
            stock_no, op_keyword, target_amount, target_price, last_seen_rows,
        )
        return None

    def _submit_trade(self, panel_key, op_keyword, stock_no, amount, price):
        """Shared form-fill + submit + lookup pipeline for buy/sell.

        panel_key: 'F1' (buy) or 'F2' (sell).
        op_keyword: '买入' or '卖出' — substring matched against orders/active 操作 column.
        """
        self.switch_to_normal()
        _activate_window(self.hwnd_main)
        hot_key([panel_key])
        time.sleep(sleep_time)
        hwnd = self.get_right_hwnd()
        ctrl = win32gui.GetDlgItem(hwnd, 0x408)
        set_text(ctrl, stock_no)
        time.sleep(sleep_time)
        price_str = None
        if price is not None:
            price_str = "%.3f" % price
            ctrl = win32gui.GetDlgItem(hwnd, 0x409)
            set_text(ctrl, price_str, True)
            time.sleep(short_sleep_time)
        ctrl = win32gui.GetDlgItem(hwnd, 0x40A)
        set_text(ctrl, str(amount))
        time.sleep(sleep_time)
        # Submit form → 确认买卖 dialog → confirm. THS may then pop an anti-bot
        # captcha that blocks the whole window; input_ocr() solves it (and is a
        # no-op when no popup is present). Only after the captcha clears does the
        # "已成功提交" result popup show, so handle the captcha BETWEEN the confirm
        # Enter and the final dismiss — three blind Enters alone can't dismiss a
        # captcha (it needs the actual code typed) and leave the order stuck.
        hot_key(["enter"])   # submit form → 确认买卖 dialog
        hot_key(["enter"])   # confirm → 提交委托（可能弹验证码）
        self.input_ocr()     # 处理反机器人验证码（无弹窗立即返回）
        hot_key(["enter"])   # dismiss 结果弹窗
        time.sleep(sleep_time)
        entrust_no = self._lookup_entrust_no(stock_no, op_keyword, amount, price)
        if entrust_no:
            return {
                "code": 0,
                "status": "succeed",
                "entrust_no": entrust_no,
                "stock_no": str(stock_no),
                "amount": int(amount),
                "price": float(price) if price is not None else None,
                "op": op_keyword,
            }
        return {
            "code": 2,
            "status": "unknown",
            "msg": "已提交但未能在 orders/active 表中匹配到对应订单，请自行确认状态",
        }

    def _do_sell(self, stock_no, amount, price):
        return self._submit_trade("F2", "卖出", stock_no, amount, price)

    def _do_buy(self, stock_no, amount, price):
        return self._submit_trade("F1", "买入", stock_no, amount, price)

    def sell_kc(self, stock_no, amount, price):
        self.switch_to_kechuang()
        self.click_kc_sell()
        hwnd = self.get_right_hwnd()
        ctrl = win32gui.GetDlgItem(hwnd, 0x408)
        set_text(ctrl, stock_no)
        time.sleep(sleep_time)
        if price is not None:
            time.sleep(sleep_time)
            price = "%.3f" % price
            ctrl = win32gui.GetDlgItem(hwnd, 0x409)
            set_text(ctrl, price)
            time.sleep(sleep_time)
        ctrl = win32gui.GetDlgItem(hwnd, 0x40A)
        set_text(ctrl, str(amount))
        time.sleep(sleep_time)
        hot_key(["enter"])
        retry = 0
        while retry < retry_time:
            time.sleep(sleep_time)
            result = self.get_result()
            if result:
                hot_key(["enter"])
                return result
            hot_key(["enter"])
            retry += 1
        return {"code": 2, "status": "unknown", "msg": "获取结果失败,请自行确认订单状态"}

    def buy_kc(self, stock_no, amount, price):
        self.switch_to_kechuang()
        self.click_kc_buy()
        hwnd = self.get_right_hwnd()
        ctrl = win32gui.GetDlgItem(hwnd, 0x408)
        set_text(ctrl, stock_no)
        time.sleep(sleep_time)
        if price is not None:
            time.sleep(sleep_time)
            price = "%.3f" % price
            ctrl = win32gui.GetDlgItem(hwnd, 0x409)
            set_text(ctrl, price)
            time.sleep(sleep_time)
        ctrl = win32gui.GetDlgItem(hwnd, 0x40A)
        set_text(ctrl, str(amount))
        time.sleep(sleep_time)
        hot_key(["enter"])
        retry = 0
        while retry < retry_time:
            time.sleep(sleep_time)
            result = self.get_result()
            if result:
                hot_key(["enter"])
                return result
            hot_key(["enter"])
            retry += 1
        return {"code": 2, "status": "unknown", "msg": "获取结果失败,请自行确认订单状态"}

    def _do_cancel(self, entrust_no):
        try:
            return self._cancel_inner(entrust_no)
        except Exception as e:
            logger.exception("cancel(%s) unhandled exception", entrust_no)
            return {"code": 1, "status": "failed", "msg": f"cancel error: {e}"}

    def _cancel_inner(self, entrust_no):
        self.switch_to_normal()
        hot_key(["F3"])
        self.refresh()
        hwnd = self.get_right_hwnd()
        if not hwnd:
            return {"code": 1, "status": "failed", "msg": "right pane not found"}
        ctrl = win32gui.GetDlgItem(hwnd, 0x417)
        if not ctrl:
            return {"code": 1, "status": "failed", "msg": "table control 0x417 not found in F3 panel"}
        self.copy_table(ctrl)
        data = None
        for _ in range(retry_time):
            time.sleep(sleep_time)
            try:
                data = get_clipboard_data()
            except Exception as e:
                logger.warning("cancel clipboard read failed: %s", e)
                continue
            if data:
                break
        if not data:
            return {"code": 1, "status": "failed", "msg": "clipboard empty after copy_table"}
        entrusts = parse_table(data)
        if not entrusts:
            return {"code": 1, "status": "failed", "msg": "F3 table parsed empty"}
        # F3 may show 委托编号 or 合同编号 depending on THS version/panel state.
        # _lookup_entrust_no returns 合同编号 from F1+F8; cancel must match either.
        id_col = None
        for candidate in ("委托编号", "合同编号"):
            if candidate in entrusts[0]:
                id_col = candidate
                break
        if not id_col:
            cols = list(entrusts[0].keys())
            return {
                "code": 1,
                "status": "failed",
                "msg": f"F3 table has neither 委托编号 nor 合同编号, columns: {cols}",
            }
        find = None
        for i, entrust in enumerate(entrusts):
            if str(entrust[id_col]) == str(entrust_no):
                find = i
                break
        if find is None:
            return {"code": 1, "status": "failed", "msg": f"没找到指定订单 {entrust_no}"}
        left, top, right, bottom = win32gui.GetWindowRect(ctrl)
        x = 50 + left
        y = 30 + 16 * find + top
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(sleep_time)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(sleep_time)
        hot_key(["enter"])
        time.sleep(sleep_time)
        hot_key(["enter"])
        return {"code": 0, "status": "succeed"}

    def get_result(self, cid=0x3EC):
        tid, pid = win32process.GetWindowThreadProcessId(self.hwnd_main)

        def enum_children(hwnd, results):
            try:
                if win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd):
                    win32gui.EnumChildWindows(hwnd, handler, results)
            except Exception:
                return

        def handler(hwnd, results):
            if (
                win32api.GetWindowLong(hwnd, win32con.GWL_ID) == cid
                and win32gui.GetClassName(hwnd) == "Static"
            ):
                results.append(hwnd)
                return False
            enum_children(hwnd, results)
            return len(results) == 0

        popups = []
        windows = []
        win32gui.EnumThreadWindows(tid, lambda hwnd, l: l.append(hwnd), windows)
        for hwnd in windows:
            if not handler(hwnd, popups):
                break
        if popups:
            ctrl = popups[0]
            text = get_text(ctrl)
            if "已成功提交" in text:
                return {
                    "code": 0,
                    "status": "succeed",
                    "msg": text,
                    "entrust_no": text.split("合同编号：")[1].split("。")[0],
                }
            else:
                return {"code": 1, "status": "failed", "msg": text}

    def refresh(self):
        hot_key(["F5"])
        time.sleep(refresh_sleep_time)

    def active_mian_window(self):
        if self.hwnd_main is not None:
            ctypes.windll.user32.SwitchToThisWindow(self.hwnd_main, True)
            time.sleep(sleep_time)

    def switch_to_normal(self):
        tabs = self.get_left_bottom_tabs()
        left, top, right, bottom = win32gui.GetWindowRect(tabs)
        x = left + 10
        y = top + 5
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(sleep_time)
        _activate_window(self.hwnd_main)

    def switch_to_kechuang(self):
        tabs = self.get_left_bottom_tabs()
        left, top, right, bottom = win32gui.GetWindowRect(tabs)
        x = left + 200
        y = top + 5
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(sleep_time)

    def click_kc_buy(self):
        tree = self.get_tree_hwnd()
        left, top, right, bottom = win32gui.GetWindowRect(tree)
        x = left + 10
        y = top + 10
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(sleep_time)

    def click_kc_sell(self):
        tree = self.get_tree_hwnd()
        left, top, right, bottom = win32gui.GetWindowRect(tree)
        x = left + 10
        y = top + 30
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(sleep_time)

    def copy_table(self, hwnd):
        os.system("echo off | clip")
        _activate_window(hwnd)
        hot_key(["ctrl", "c"])
        self.input_ocr()

    def _preprocess_captcha(self, image):
        """Upscale + grayscale + Otsu + sharpen — boosts tesseract accuracy on
        72x32 stylized captchas. Returns a PIL.Image."""
        import numpy as np
        import cv2

        arr = np.array(image)
        if arr.ndim == 3:
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        else:
            gray = arr
        scale = 4
        h, w = gray.shape
        upscaled = cv2.resize(
            gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC
        )
        _, binary = cv2.threshold(
            upscaled, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU
        )
        pil = Image.fromarray(binary)
        return pil.filter(ImageFilter.SHARPEN)

    def _refresh_captcha(self, captcha_static):
        """Click the captcha image to trigger image regeneration. xiadan does
        NOT auto-refresh on wrong submission — without this each retry OCRs
        the same image and gets the same wrong answer."""
        rect = win32gui.GetWindowRect(captcha_static)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        win32api.SetCursorPos((cx, cy))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(short_sleep_time)

    def input_ocr(self):
        """OCR captcha popup → type code → submit. Retry up to 10x if rejected.

        Four rewrites vs upstream:
        - Tesseract config: ``--psm 7`` (single text line) + alphanumeric
          whitelist. Default PSM 3 returns ``"VY qs"`` for a "VYqS" captcha;
          the embedded space crashes the per-char keyboard loop in
          ``set_text`` (``KeyError`` on ``VK_CODE[' ']``).
        - Edit/Button discovery: walk the popup dialog's children by class
          and find the Edit + "确定" Button directly. Upstream walked
          ``GW_HWNDNEXT`` 3 times from the captcha image static — that
          Z-order assumption no longer holds in current xiadan builds, where
          the Edit's hwnd is several thousand higher than its siblings, so
          the walk lands on the wrong control. Upstream's keybd_event path
          accidentally tolerated this (keystrokes go to the focused window,
          not the SetForegroundWindow target); ``WM_SETTEXT`` is direct, so
          a wrong hwnd means the Edit stays empty and the dialog reports
          "验证码错误!!".
        - Typing: ``WM_SETTEXT`` instead of ``set_text``. Atomic, bypasses
          IME and shift-state timing.
        - Submit: ``BM_CLICK`` on the OK button instead of a global Enter
          keystroke — doesn't depend on focus.
        - Retry: re-OCR + resubmit if popup persists. Wrong captcha causes
          a fresh image; retry buys statistical convergence.
        """
        max_retries = 10
        whitelist = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789"
        )
        ocr_config = f"--psm 7 -c tessedit_char_whitelist={whitelist}"
        for attempt in range(1, max_retries + 1):
            captcha_static = self.get_ocr_hwnd()
            if not captcha_static:
                return
            # GA_ROOT=2 — top-level popup dialog.
            dialog = ctypes.windll.user32.GetAncestor(captcha_static, 2)
            if not dialog:
                logger.warning("ocr attempt=%d cannot resolve dialog from %s",
                               attempt, hex(captcha_static))
                return
            edit_hwnd = 0
            ok_btn = 0

            def walker(h, _):
                nonlocal edit_hwnd, ok_btn
                cls = win32gui.GetClassName(h)
                if cls == "Edit" and not edit_hwnd:
                    edit_hwnd = h
                elif cls == "Button" and not ok_btn:
                    if "确定" in (win32gui.GetWindowText(h) or ""):
                        ok_btn = h

            win32gui.EnumChildWindows(dialog, walker, None)
            if not edit_hwnd:
                logger.warning("ocr attempt=%d no Edit in dialog %s",
                               attempt, hex(dialog))
                return
            # On retries (attempt > 1), force a fresh captcha image first —
            # xiadan doesn't auto-rotate on wrong submission, so without this
            # every retry OCRs the same image and gets the same wrong answer.
            if attempt > 1:
                self._refresh_captcha(captcha_static)
            ocr_png = os.path.join(work_dir, "ocr.png")
            ocr_proc_png = os.path.join(work_dir, "ocr_proc.png")
            self.capture_window(captcha_static, ocr_png)
            try:
                raw_image = Image.open(ocr_png)
                image = self._preprocess_captcha(raw_image)
                image.save(ocr_proc_png)
            except Exception:
                logger.exception("ocr attempt=%d preprocess failed", attempt)
                image = Image.open(ocr_png)
            try:
                text = pytesseract.image_to_string(image, config=ocr_config)
            except Exception:
                logger.exception("ocr attempt=%d tesseract failed", attempt)
                text = ""
            code = text.strip()
            logger.info(
                "ocr attempt=%d edit=%s ok_btn=%s raw=%r code=%r",
                attempt, hex(edit_hwnd),
                hex(ok_btn) if ok_btn else None, text, code,
            )
            if not code:
                time.sleep(short_sleep_time)
                continue
            # xiadan's captcha Edit only accepts focus from real mouse input
            # (anti-bot — API SetFocus is treated as untrusted, WM_SETTEXT is
            # silently dropped). So: bring popup to foreground, click the Edit
            # center to grant focus, attach thread input, type via WM_CHAR.
            user32 = ctypes.windll.user32
            _activate_window(dialog)
            time.sleep(short_sleep_time)
            er = win32gui.GetWindowRect(edit_hwnd)
            cx = (er[0] + er[2]) // 2
            cy = (er[1] + er[3]) // 2
            win32api.SetCursorPos((cx, cy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(short_sleep_time)
            my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            target_tid, _ = win32process.GetWindowThreadProcessId(edit_hwnd)
            attached = False
            try:
                if my_tid != target_tid:
                    if user32.AttachThreadInput(my_tid, target_tid, True):
                        attached = True
                user32.SetFocus(edit_hwnd)
                # Select-all + clear, robust against any pre-existing chars.
                user32.SendMessageW(edit_hwnd, win32con.EM_SETSEL, 0, -1)
                user32.SendMessageW(edit_hwnd, win32con.WM_CLEAR, 0, 0)
                # WM_CHAR per char — real-typing semantics, bypasses IME and
                # the SetText anti-bot subclass.
                for ch in code:
                    user32.SendMessageW(edit_hwnd, win32con.WM_CHAR, ord(ch), 0)
                    time.sleep(0.02)
            finally:
                if attached:
                    user32.AttachThreadInput(my_tid, target_tid, False)
            # Verify what's actually in the Edit before clicking OK.
            n = user32.SendMessageW(edit_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.SendMessageW(
                edit_hwnd, win32con.WM_GETTEXT, n + 1, ctypes.byref(buf)
            )
            actual = buf.value
            logger.info(
                "ocr attempt=%d wrote=%r read_back=%r match=%s",
                attempt, code, actual, actual == code,
            )
            time.sleep(short_sleep_time)
            if ok_btn:
                win32api.SendMessage(ok_btn, win32con.BM_CLICK, 0, 0)
            else:
                hot_key(["enter"])
            time.sleep(sleep_time)
            if not self.get_ocr_hwnd():
                logger.info("ocr accepted attempt=%d code=%r", attempt, code)
                return
            logger.info("ocr rejected attempt=%d code=%r", attempt, code)
        logger.warning("ocr gave up after %d attempts", max_retries)

    def capture_window(self, hwnd, file_name):
        # HiDPI 修复（Retina / Parallels 200% 缩放）：进程"部分 DPI 感知"时
        # GetWindowRect 返回逻辑像素，但窗口 DC 的 BitBlt 按物理像素复制 → 只截到
        # 验证码左上一块（半张图），OCR 必错。临时把线程切到 Per-Monitor-V2 DPI
        # 感知，使 GetWindowRect 也返回物理像素，截全图后还原（只影响这次截图）。
        user32 = ctypes.windll.user32
        DPI_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
        old_ctx = None
        if hasattr(user32, "SetThreadDpiAwarenessContext"):
            try:
                old_ctx = user32.SetThreadDpiAwarenessContext(
                    DPI_CONTEXT_PER_MONITOR_AWARE_V2
                )
            except Exception as e:
                logger.debug("SetThreadDpiAwarenessContext failed: %s", e)
                old_ctx = None
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = right - left
            height = bottom - top

            hdc = win32gui.GetWindowDC(hwnd)
            dc = win32ui.CreateDCFromHandle(hdc)
            cdc = dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(dc, width, height)
            cdc.SelectObject(bmp)
            cdc.BitBlt((0, 0), (width, height), dc, (0, 0), win32con.SRCCOPY)

            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer(
                "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
            )

            win32gui.DeleteObject(bmp.GetHandle())
            dc.DeleteDC()
            cdc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hdc)

            img.save(file_name)
        finally:
            if old_ctx is not None:
                try:
                    user32.SetThreadDpiAwarenessContext(old_ctx)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Async surface for PH-061 dispatcher.
    #
    # The 7 whitelist methods called by trader.dispatcher.handle_call.
    # Sync pywin32 work is wrapped in asyncio.to_thread so the trader
    # event loop (ws_client, tray) stays responsive.
    # ------------------------------------------------------------------

    async def balance(self) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.get_balance)

    async def position(self) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.get_position)

    async def orders_active(self) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.get_active_orders)

    async def orders_filled(self) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.get_filled_orders)

    async def settlement(self, date_range: str = "近一年") -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self._do_settlement, date_range)

    async def buy(
        self,
        stock_no: str,
        amount: int,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        # price=None ⇒ 市价单：保持 None 透传，让 _submit_trade 跳过价格框，
        # 沿用 xiadan 按股票代码自动带出的对手价。强转 0 会把价格框写成 "0.000"，
        # 同花顺无法以 0.00 挂单。
        return await asyncio.to_thread(self._do_buy, stock_no, amount, price)

    async def sell(
        self,
        stock_no: str,
        amount: int,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        # price=None ⇒ 市价单：保持 None 透传（见 buy 注释）。
        return await asyncio.to_thread(self._do_sell, stock_no, amount, price)

    async def cancel(self, entrust_no: str) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self._do_cancel, entrust_no)
