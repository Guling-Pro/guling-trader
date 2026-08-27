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
import functools
import json
import logging
import os
import platform
import re
import sys
import tempfile
import threading
import time
import unicodedata
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

from .const import (
    BALANCE_CONTROL_ID_GROUP,
    MARKET_AMOUNT_ID,
    MARKET_CODE_ID,
    MARKET_STRATEGY,
    MARKET_STRATEGY_COMBO_ID,
    MARKET_SUBMIT_BTN_ID,
    MARKET_TREE_PATHS,
    VK_CODE,
)
from .table_guard import check_table
from .rows import (
    normalize_active_row,
    normalize_balance,
    normalize_filled_row,
    normalize_position_row,
    normalize_settlement_row,
    is_in_flight,
)
from .. import contract
from ..contract import CLS_ABORTED, CLS_NOT_BOUND, CLS_READ_FAILED, CLS_TABLE_MISMATCH

logger = logging.getLogger(__name__)


def _match_market_fill(before, after, stock_no, op_keyword, requested_amount):
    """前后成交表差分 → 市价单成交回执。before/after 为 get_filled_orders 的 data。

    五档即成剩撤下单后几乎不留 orders_active（全成→成交表；部分成→成交部分进成交表、
    剩余被撤），故市价回执查成交表(orders_filled)差分，而非 orders_active。可能部分
    成交 → 回执带回真实成交数量与按金额加权的成交均价。
    """
    def _key(r):
        return (r.get("成交编号") or "") or (
            r.get("证券代码"), r.get("成交数量"), r.get("成交均价"), r.get("成交金额"))

    seen = {_key(r) for r in before}
    filled_qty = 0
    filled_amt = 0.0
    for r in after:
        if _key(r) in seen:
            continue
        if (r.get("证券代码") or "") != str(stock_no):
            continue
        if op_keyword not in (r.get("方向") or ""):
            continue
        q, a = r.get("成交数量"), r.get("成交金额")
        if q is None or a is None:
            continue
        filled_qty += int(q)
        filled_amt += float(a)

    payload = {"stock_no": str(stock_no), "方向": op_keyword,
               "requested_amount": int(requested_amount)}
    if filled_qty <= 0:
        payload["filled_amount"] = 0
        return contract.submitted_unconfirmed(
            "已提交但成交表尚未出现本次成交（可能非连续竞价时段/涨跌停被拒/尚未成交）。"
            "请用同一 client_order_id 重发查询或调 query_order 核实，勿改单重下",
            data=payload)

    payload["filled_amount"] = filled_qty
    payload["成交均价"] = round(filled_amt / filled_qty, 3)
    payload["成交金额"] = round(filled_amt, 2)
    payload["fill_state"] = "filled" if filled_qty >= int(requested_amount) else "partially_filled"
    return contract.ok(payload)

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

# 账户列表项：每次按当前绑定主窗口重新枚举，不保存其 HWND；同花顺重启后
# HWND 可能变化。0x0912 来自当前 Windows 控件快照：ComboBox、可见、可用。
# 展开后，同花顺创建可见的顶层 ComboLBox（ID 0x03E8）；真机确认其标准
# LB_GETTEXT 可直接返回账户原文，末项“编辑账户”不是可切换账户。
ACCOUNT_SELECTOR_ID = 0x094C
ACCOUNT_SELECTOR_CLASS = "Button"
ACCOUNT_VERIFY_TIMEOUT_SECS = 3.0
ACCOUNT_TEXT_PLACEHOLDERS = frozenset({"", "NUL", "ＸＸ证券"})
ACCOUNT_DROPDOWN_ID = 0x0912
ACCOUNT_DROPDOWN_SETTLE_SECS = 0.3
ACCOUNT_LISTBOX_ID = 0x03E8
ACCOUNT_LISTBOX_CLASS = "ComboLBox"
ACCOUNT_LISTBOX_NON_ACCOUNT_ITEMS = frozenset({"编辑账户"})
LB_GETTEXT = 0x0189
LB_GETTEXTLEN = 0x018A
LB_GETCOUNT = 0x018B
LB_ERR = -1

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


def _matching_windows_by_title_prefix(prefix: str) -> list[tuple[int, str]]:
    """Return every visible top-level window whose title starts with ``prefix``.

    This deliberately has no ``FindWindow`` fast path. An exact title is also
    a prefix match, so it must be considered together with broker-suffixed
    windows rather than silently bypassing the ambiguity check.
    """
    if not prefix:
        return []
    matches: list[tuple[int, str]] = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            text = win32gui.GetWindowText(hwnd) or ""
            if text.startswith(prefix):
                matches.append((hwnd, text))
        except Exception:
            # Enumeration is an identity check, not a best-effort lookup.
            # Ignore an unreadable candidate; the caller still fails closed
            # unless exactly one readable candidate remains.
            return

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        logger.warning("unable to enumerate windows for title prefix %r", prefix, exc_info=True)
        return []
    return matches


def find_window_by_title_prefix(prefix: str) -> int:
    """Return the sole visible top-level prefix match, or ``0`` if ambiguous.

    Hexin clients typically render ``<base> - <broker> - <hint>``. Prefix
    matching handles that suffix, but choosing an arbitrary first match is
    unsafe because subsequent F-keys and clicks are process-global.
    """
    matches = _matching_windows_by_title_prefix(prefix)
    if len(matches) != 1:
        if matches:
            logger.error(
                "refusing ambiguous window binding: %d windows match prefix %r: %r",
                len(matches),
                prefix,
                [title for _, title in matches],
            )
        return 0
    hwnd, full = matches[0]
    logger.info("matched unique window prefix=%r → full_title=%r hwnd=%s", prefix, full, hwnd)
    return hwnd


def _normalize_executable_identity(path: Any) -> str:
    """Normalize a process image path only for equality comparison."""
    return os.path.normcase(os.path.normpath(str(path or "").strip()))


def _window_process_identity(hwnd: int) -> tuple[int, str] | None:
    """Return ``(pid, executable_path)`` for a window, or ``None`` on doubt.

    ``GetWindowThreadProcessId`` alone is insufficient: Windows can recycle an
    HWND after xiadan restarts. The image path gives the cached binding a second
    stable identity signal. Every failure intentionally returns ``None`` so
    callers stop rather than sending input to an unverified window.
    """
    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        pid = int(pid)
        if pid <= 0:
            return None
        access = (
            getattr(win32con, "PROCESS_QUERY_LIMITED_INFORMATION", 0x1000)
            | getattr(win32con, "PROCESS_QUERY_INFORMATION", 0x0400)
            | getattr(win32con, "PROCESS_VM_READ", 0x0010)
        )
        handle = win32api.OpenProcess(access, False, pid)
        try:
            executable = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
        executable = _normalize_executable_identity(executable)
        if not executable:
            return None
        return pid, executable
    except Exception:
        logger.warning("unable to verify process identity for hwnd=%s", hwnd, exc_info=True)
        return None


def _foreground_window() -> int:
    """Read the foreground HWND. Missing Win32 APIs are a failed check."""
    try:
        return int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        return 0


def get_clipboard_data(open_retries: int = 10):
    """读剪贴板文本。永不抛异常——失败返回 None，让调用方的重试循环继续。

    OpenClipboard 在别的进程占用剪贴板时会抛（拷贝表格 + 拷贝数据验证码弹窗期间
    尤其常见）；剪贴板被别的进程锁通常只有几毫秒，直接放弃太浪费 → 退避重试若干次
    再放弃。GetClipboardData 在 CF_UNICODETEXT 格式还没就绪时也会抛，这属于"本次没
    数据"，不重试直接返回 None。
    """
    for _ in range(max(1, open_retries)):
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            time.sleep(0.02)  # 被别的进程短暂锁住 → 退避重试
            continue
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
    return None


def hot_key(keys, before_dispatch=None):
    """Send a hotkey after the empirical delay.

    State-changing callers can revalidate focus and their call generation
    after this delay, immediately before the first physical key event.
    """
    time.sleep(sleep_time)
    if before_dispatch:
        before_dispatch()
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


def set_text(hwnd, string, isPrice=False):
    """快速填值：EM_SETSEL 全选 + WM_CLEAR 清空 + 逐字符 WM_CHAR 直发（无逐字延迟）。

    交易讲究快。原实现用 keybd_event + 每字符 sleep(0.1)，"000970" 就要 0.6s，还抢
    全局键盘/光标、要强切 IME。改用 WM_CHAR **直接发给目标 Edit**：
    - "真实键入"语义 —— 逐字符触发 EN_CHANGE，THS 证券代码→名称联想/校验照常；
    - 直发 hwnd，不依赖焦点、不移动光标、绕过 IME 与 shift 时序；
    - 无逐字 sleep，整串几乎瞬间完成，比原来快一个数量级。
    isPrice 保留签名兼容；价格串由调用方已按 %.3f 格式化。
    """
    u32 = ctypes.windll.user32
    _activate_window(hwnd)
    u32.SendMessageW(hwnd, win32con.EM_SETSEL, 0, -1)  # 全选
    u32.SendMessageW(hwnd, win32con.WM_CLEAR, 0, 0)    # 清空（防残留）
    for ch in str(string):
        u32.SendMessageW(hwnd, win32con.WM_CHAR, ord(ch), 0)


def get_text(hwnd):
    length = ctypes.windll.user32.SendMessageW(hwnd, win32con.WM_GETTEXTLENGTH)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.SendMessageW(hwnd, win32con.WM_GETTEXT, length, ctypes.byref(buf))
    return buf.value


def _account_candidates_from_listbox(items: list[str]) -> list[dict[str, Any]]:
    """Map verified account ListBox rows to Alt+1..Alt+9 in visible order."""
    account_texts = [
        text.strip() for text in items
        if text.strip() and text.strip() not in ACCOUNT_LISTBOX_NON_ACCOUNT_ITEMS
    ]
    if len(account_texts) > 9:
        raise ValueError(f"账户下拉列表含 {len(account_texts)} 个账户，超过 Alt+1..Alt+9 范围")
    return [
        {"slot": index, "shortcut": f"Alt+{index}", "text": text}
        for index, text in enumerate(account_texts, start=1)
    ]


_ACCOUNT_IDENTITY_SEPARATORS = re.compile(r"[\s\-‐‑‒–—―]+")


def _account_identity(text: Any) -> str:
    """Canonical form used only to compare the selector with its ListBox row.

    The verified THS controls render the same account as e.g. ``券商 王*甲`` in
    the selector and ``券商-王*甲`` in the ListBox. Only formatting separators
    are ignored; the broker name and masked holder name (including ``*``) remain
    part of the identity.
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
    return _ACCOUNT_IDENTITY_SEPARATORS.sub("", normalized)


_PHANTOM_VALUES = frozenset({"", "0", "0.0", "0.00", "0.000", "-", "--"})


def _is_phantom_value(value: Any) -> bool:
    """Return whether a copied cell is an empty-table placeholder value."""
    text = str(value).strip().strip("\x00")
    if text in _PHANTOM_VALUES:
        return True
    # THS emits different decimal precision depending on the selected table,
    # e.g. 0.0000 in the order grid. Treat only an actual numeric zero as empty.
    if re.fullmatch(r"[+-]?0+(?:\.0+)?", text):
        return True
    return False


def table_columns(text):
    """取 THS 剪贴板表格的表头列名（与 parse_table 同一套切分约定）。

    单独一个函数是因为**空表也要能校验归属**：今天无挂单/无成交时 parse_table
    返回 []，表头却仍在——归属校验只能看表头，不能看行。
    """
    if not text:
        return []
    return [c for c in text.split("\t\r\n")[0].split("\t") if c.strip()]


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
        if all(_is_phantom_value(v) for v in info.values()):
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
        # 64 位布局（hItem/pszText/lParam = 8 字节）；当目标进程是 64 位时用。
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

    class _TVITEM32(ctypes.Structure):
        # 32 位布局（hItem/pszText/lParam = 4 字节）。xiadan 是 32 位进程，64 位 Python
        # 发 TVM_GETITEMW 时结构指针大小必须与目标一致，否则目标读到错位结构 → 消息
        # 失败(返回 0)、文字读空。这就是历史上"树文字读不到"被误判为"回调式不可读"的真因。
        _fields_ = [
            ("mask", ctypes.c_uint),
            ("hItem", ctypes.c_uint32),
            ("state", ctypes.c_uint),
            ("stateMask", ctypes.c_uint),
            ("pszText", ctypes.c_uint32),
            ("cchTextMax", ctypes.c_int),
            ("iImage", ctypes.c_int),
            ("iSelectedImage", ctypes.c_int),
            ("cChildren", ctypes.c_int),
            ("lParam", ctypes.c_uint32),
        ]

    def _proc_is_wow64(pid: int):
        """目标进程是否 32 位(在 64 位 Windows 上以 WOW64 运行)。用于给跨进程 TVITEM
        选对应位数的布局。失败返回 None。"""
        try:
            h = win32api.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            wow = wintypes.BOOL()
            ctypes.windll.kernel32.IsWow64Process(int(h), ctypes.byref(wow))
            return bool(wow.value)
        except Exception:
            return None


class ThsState:
    """trader 内存态：存各查询的最近一次解析结果 + 时间戳。

    数据流：剪贴板只做"拷贝→读取→立刻清空"的毫秒级中转，解析结果落到这里；消费方
    可读 last-known（`get`），不必再触碰剪贴板。线程安全——order_watch 后台线程与
    RPC 可能并发访问。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._store: dict[str, dict] = {}

    def update(self, key: str, data: Any) -> None:
        with self._lock:
            self._store[key] = {"data": data, "ts": time.time()}

    def get(self, key: str, max_age: Optional[float] = None) -> Any:
        """取最近一次结果；传 max_age（秒）则超龄返回 None（避免拿到过时数据）。"""
        with self._lock:
            entry = self._store.get(key)
        if not entry:
            return None
        if max_age is not None and (time.time() - entry["ts"]) > max_age:
            return None
        return entry["data"]

    def snapshot(self) -> dict:
        """各 key 的时间戳与行数概览（诊断用）。"""
        with self._lock:
            out = {}
            for k, v in self._store.items():
                d = v["data"]
                out[k] = {"ts": v["ts"], "rows": len(d) if isinstance(d, list) else 1}
            return out


class StaleCallAborted(RuntimeError):
    """本线程所属的调用已被作废（dispatcher 超时后放锁），必须立刻停手。"""


class WindowSafetyError(RuntimeError):
    """A global UI action was stopped before it could target an unverified window."""


# Some xiadan builds write neither rows nor headers for an empty
# CVirtualGridCtrl. This is never used for ordinary clipboard failures.
_VERIFIED_EMPTY_GRID = object()


def guarded(fn):
    """工作线程入口装饰器：登记调用代次，本笔被作废时在检查点中止。

    装在同步实现上（而非 to_thread 调用点）——RPC 异步壳的形状保持不变，
    代次跟着方法走，内部相互调用（下单→查成交表）自动继承同一代次。
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        return self._run_guarded(fn.__get__(self, type(self)), *args, **kwargs)

    return wrapper


class WinThsBackend:
    def __init__(self):
        self.hwnd_main = None
        # HWND values can be recycled after xiadan exits. Keep the process
        # identity captured at bind time and verify it before every global UI
        # action; title text by itself is not a sufficient identity proof.
        self._bound_pid: int | None = None
        self._bound_executable: str | None = None
        # 启动后必须先由 switch_account(slot) 按账户列表核验 0x094C；之后每笔
        # 交易都核对该文本。任何读取失败或文本变化都阻断买卖和撤单。
        self._account_trading_blocked = True
        self._last_account_text: str | None = None
        # order_watch 与 RPC 共用：串行化对 THS 单窗口的访问，避免并发拷表。
        self.win_lock = asyncio.Lock()
        # agent 经 RPC 下单成功后登记的合同编号，供 order_watch 标记事件来源。
        self.agent_entrust_nos: set[str] = set()
        # 内存态：查询结果的 last-known 存储（剪贴板仅作毫秒级中转）。
        self.state = ThsState()
        # Last raw columns observed by _grab_grid. Limit-order ownership needs
        # stronger evidence than the generic query-table marker check, and an
        # empty but valid table has no rows from which to recover this fact.
        self._last_grid_columns: dict[str, tuple[str, ...]] = {}
        # 原始剪贴板与最终订单行只保存在内存；仅基线拒绝时写入 trader.log，
        # 用于定位券商空表占位行、短行或合同号异常，绝不随普通查询回执上送。
        self._last_grid_debug: dict[str, dict[str, Any]] = {}
        # No-header empty grids retain their verification separately: an
        # unqualified clipboard failure must not become an empty baseline.
        self._last_grid_verified_empty: set[str] = set()
        # dispatcher 侧调用超时后置位；下一次调用进入前先跑 dialog_cleanup 自愈。
        self.degraded = False
        # 调用代次：dispatcher 超时后 +1，作废所有在飞的工作线程（见 _abort_if_stale）。
        self._gen = 0
        self._gen_lock = threading.Lock()
        self._tls = threading.local()
        # 下单台账（幂等 + client_order_id 回显）；懒加载，见 ledger 属性。
        self._ledger = None

    # --- 调用代次：治「超时线程脱缰」---------------------------------------
    # dispatcher 的 25s 总超时用 asyncio.wait_for 包 asyncio.to_thread，超时只取消
    # 等待协程——**线程取消不掉**，它还在发全局按键；而 finally 已经放了 win_lock，
    # 下一笔立刻进场 → 两个线程同击一个 xiadan 窗口（页面被别人切走 = 抓错表；
    # 弹窗被两边抢 = 验证码/确认框错点）。
    # 代次机制让脱缰线程在下一个检查点自己退出：工作线程进场时记下当时的代次，
    # 超时时 dispatcher 把代次 +1，线程在每个 UI 动作前对一次，不一致就抛
    # StaleCallAborted 退出。检查点覆盖翻页(switch_to_normal/refresh)、抓表
    # (read_table_text)、弹窗(input_ocr / dialogs.pump / dialog_cleanup)与下单提交。

    def invalidate_inflight(self, reason: str = "") -> int:
        """作废当前在飞的工作线程（dispatcher 超时时调用）。返回新代次。"""
        with self._gen_lock:
            self._gen += 1
            gen = self._gen
        logger.warning("调用代次 → %s，在飞线程已作废：%s", gen, reason or "(未注明)")
        return gen

    def _run_guarded(self, fn, *args, **kwargs):
        """在工作线程里带代次运行 fn；被作废则中止并返回 failed（结果已无人接收）。

        可重入：内层已登记代次时直接执行（如 _submit_market_trade 内部调
        get_filled_orders），中止异常一路抛到最外层那次统一收口。
        """
        if getattr(self._tls, "gen", None) is not None:
            return fn(*args, **kwargs)
        with self._gen_lock:
            self._tls.gen = self._gen
        try:
            return fn(*args, **kwargs)
        except StaleCallAborted as e:
            logger.warning("脱缰线程已在 %s 处停手（%s）", getattr(e, "where", "?"), e)
            return contract.fail(contract.CODE_ABORTED, CLS_ABORTED, f"调用已作废：{e}")
        except WindowSafetyError as e:
            logger.warning("窗口安全检查阻止了 UI 输入：%s", e)
            return contract.fail(contract.CODE_NOT_BOUND, CLS_NOT_BOUND, str(e))
        finally:
            self._tls.gen = None

    def _abort_if_stale(self, where: str) -> None:
        """代次检查点。非受管线程（UI/测试直调）不拦。"""
        mine = getattr(self._tls, "gen", None)
        if mine is None:
            return
        with self._gen_lock:
            current = self._gen
        if mine != current:
            err = StaleCallAborted(
                f"代次 {mine} 已被 {current} 取代，在 {where} 处放弃，避免与新调用同击一窗")
            err.where = where
            raise err

    def _pump_dialogs(self):
        """提交动作后的弹窗「发现-处置-存证」循环（见 ths/dialogs.py）。"""
        self._abort_if_stale("pump_dialogs")
        if not self._bound_window_is_valid():
            self._clear_bound_window("cannot pump dialogs for an invalid binding")
            raise WindowSafetyError("弹窗处置前交易窗口绑定已失效，已停止处理弹窗")
        from .dialogs import DialogSentry
        return DialogSentry(self).pump()

    def dialog_cleanup(self):
        """degraded 自愈入口：清掉残留弹窗并留存证（dispatcher 在超时后的
        下一次调用前执行）。返回 PumpResult，内容进日志。"""
        self._abort_if_stale("dialog_cleanup")
        if not self._bound_window_is_valid():
            self._clear_bound_window("cannot clean dialogs for an invalid binding")
            raise WindowSafetyError("弹窗清理前交易窗口绑定已失效，已停止处理弹窗")
        from .dialogs import DialogSentry
        result = DialogSentry(self).cleanup()
        if result.dialogs:
            logger.warning("dialog_cleanup 清掉残留弹窗：%s", result.dialogs)
        return result

    def _clear_bound_window(self, reason: str) -> None:
        if self.hwnd_main:
            logger.warning("discarding bound xiadan window hwnd=%s: %s", self.hwnd_main, reason)
        self.hwnd_main = None
        self._bound_pid = None
        self._bound_executable = None

    def _bound_window_is_valid(self) -> bool:
        """Check the cached HWND, title, PID, and executable identity.

        Failure is intentionally indistinguishable from a missing binding to
        callers. A successful title match alone is not enough because a new
        process can inherit a recycled HWND and render the same title.
        """
        hwnd = self.hwnd_main
        if not hwnd or hwnd <= 0 or not self._bound_pid or not self._bound_executable:
            return False
        try:
            if not win32gui.IsWindow(hwnd):
                return False
            if not (win32gui.GetWindowText(hwnd) or "").startswith(window_title):
                return False
        except Exception:
            return False
        identity = _window_process_identity(hwnd)
        return bool(
            identity
            and identity[0] == self._bound_pid
            and identity[1] == self._bound_executable
        )

    def _activate_bound_window(self) -> bool:
        """Activate the verified xiadan window and require it to become foreground."""
        if not self._bound_window_is_valid():
            self._clear_bound_window("cached HWND/title/process identity verification failed")
            return False
        _activate_window(self.hwnd_main)
        if _foreground_window() != self.hwnd_main:
            logger.warning(
                "xiadan activation failed or focus was stolen: expected=%s actual=%s",
                self.hwnd_main, _foreground_window(),
            )
            return False
        return True

    def _foreground_is_bound_process(self) -> bool:
        """Whether the foreground top-level window belongs to the bound client.

        A confirmation/captcha dialog can legitimately become foreground after
        a submit. It is still accepted only when its process identity matches
        the bound xiadan process; another application with a copied title is
        never accepted.
        """
        if not self._bound_window_is_valid():
            self._clear_bound_window("bound identity changed while checking foreground")
            return False
        identity = _window_process_identity(_foreground_window())
        return bool(
            identity
            and identity[0] == self._bound_pid
            and identity[1] == self._bound_executable
        )

    def _window_is_owned_by_bound_process(self, hwnd: int) -> bool:
        """Whether ``hwnd`` still belongs to the exact client we bound.

        Direct WM_CHAR/BM_CLICK delivery does not need foreground focus, but
        it *does* need this check: child HWND values are recyclable too. A
        control ID and a title are not sufficient evidence that a message is
        going to the same xiadan process.
        """
        if not hwnd or not self._bound_window_is_valid():
            self._clear_bound_window("bound identity changed while checking a target control")
            return False
        identity = _window_process_identity(hwnd)
        return bool(
            identity
            and identity[0] == self._bound_pid
            and identity[1] == self._bound_executable
        )

    def _require_owned_window_for_input(self, hwnd: int, where: str) -> None:
        """Fail closed before directly messaging a state-changing control."""
        self._abort_if_stale(where)
        if not self._window_is_owned_by_bound_process(hwnd):
            raise WindowSafetyError(
                f"{where}: 目标控件不属于当前已绑定的 xiadan 进程，已停止发送输入"
            )

    def _activate_owned_window(self, hwnd: int) -> bool:
        """Activate a bound-process popup and require that exact popup in front."""
        if not hwnd or not self._bound_window_is_valid():
            self._clear_bound_window("cannot activate popup from an invalid binding")
            return False
        identity = _window_process_identity(hwnd)
        if not (
            identity
            and identity[0] == self._bound_pid
            and identity[1] == self._bound_executable
        ):
            logger.warning("refusing to activate foreign popup hwnd=%s", hwnd)
            return False
        _activate_window(hwnd)
        return _foreground_window() == hwnd

    def _require_foreground_for_input(self, where: str,
                                      allow_bound_process_popup: bool = False,
                                      expected_popup: int | None = None) -> None:
        """Fail closed before a process-global hotkey or physical mouse event."""
        self._abort_if_stale(where)
        if expected_popup:
            if (
                _foreground_window() == expected_popup
                and self._window_is_owned_by_bound_process(expected_popup)
            ):
                return
            if not self._activate_owned_window(expected_popup):
                raise WindowSafetyError(
                    f"{where}: 指定 xiadan 弹窗未能保持前台或绑定已失效，已停止发送全局输入"
                )
            return
        if allow_bound_process_popup and self._foreground_is_bound_process():
            return
        if not self._activate_bound_window():
            raise WindowSafetyError(
                f"{where}: xiadan 窗口未能保持前台或绑定已失效，已停止发送全局输入"
            )

    def _send_hotkey(self, keys: list[str], where: str,
                     allow_bound_process_popup: bool = False,
                     expected_popup: int | None = None,
                     before_dispatch=None) -> None:
        self._require_foreground_for_input(
            where, allow_bound_process_popup, expected_popup
        )

        def final_check() -> None:
            # hot_key waits before pressing its first key. The dispatcher may
            # time out or another app may take focus during that interval.
            self._require_foreground_for_input(
                where, allow_bound_process_popup, expected_popup
            )
            if before_dispatch:
                before_dispatch()

        hot_key(keys, before_dispatch=final_check)

    def _click_screen(self, x: int, y: int, where: str) -> None:
        """Perform one physical click only after foreground verification."""
        self._require_foreground_for_input(where)
        win32api.SetCursorPos((x, y))
        # Moving the pointer may itself take long enough for another app to
        # surface. Recheck at the last possible point before button-down.
        if (
            _foreground_window() != self.hwnd_main
            or not self._window_is_owned_by_bound_process(self.hwnd_main)
        ):
            raise WindowSafetyError(
                f"{where}: 鼠标点击前前台窗口或进程身份已变化，已停止发送点击"
            )
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _click_owned_popup(self, popup: int, x: int, y: int, where: str) -> None:
        """Click an exact, bound-process popup rather than whichever window is frontmost."""
        if not self._activate_owned_window(popup):
            raise WindowSafetyError(
                f"{where}: 弹窗不属于当前已绑定的 xiadan 进程或未能置前，已停止点击"
            )
        win32api.SetCursorPos((x, y))
        if (
            _foreground_window() != popup
            or not self._window_is_owned_by_bound_process(popup)
        ):
            raise WindowSafetyError(
                f"{where}: 鼠标点击前弹窗焦点或进程身份已变化，已停止点击"
            )
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _set_owned_text(self, hwnd: int, value: Any, where: str, is_price: bool = False) -> None:
        """Fill a form control only after both foreground and ownership checks."""
        self._require_foreground_for_input(where)
        self._require_owned_window_for_input(hwnd, where)
        set_text(hwnd, value, is_price)

    def _post_owned_button_click(self, hwnd: int, where: str, before_dispatch=None) -> None:
        """Deliver BM_CLICK only to a control in the bound xiadan process."""
        self._require_owned_window_for_input(hwnd, where)
        if before_dispatch:
            before_dispatch()
        win32api.PostMessage(hwnd, win32con.BM_CLICK, 0, 0)

    def _switch_to_normal_safely(self) -> None:
        """Use the existing navigation primitive with its input guard enabled.

        Keeping the public ``switch_to_normal()`` default unchanged avoids
        turning pure read navigation into an availability dependency on focus.
        """
        self._tls.require_window_safety = True
        try:
            self.switch_to_normal()
        finally:
            self._tls.require_window_safety = False

    def _pre_submit_failure(self, message: str, code: str = contract.CODE_NOT_BOUND,
                            error_class: str = CLS_NOT_BOUND) -> dict[str, Any]:
        """Build a never-submitted receipt for a failure before the submit action."""
        return contract.fail(code, error_class, message, data={"submitted": False})

    def _activate_or_pre_submit_failure(self, where: str) -> dict[str, Any] | None:
        try:
            self._require_foreground_for_input(where)
        except StaleCallAborted:
            raise
        except WindowSafetyError as e:
            logger.warning("pre-submit foreground check failed at %s: %s", where, e)
            return self._pre_submit_failure(str(e))
        return None

    def _ensure_bound(self) -> dict[str, Any] | None:
        """检查是否已绑定；否则 lazy bind，返回错误 dict 或 None（成功）"""
        if self.hwnd_main and self.hwnd_main > 0:
            if self._bound_window_is_valid():
                return None
            self._clear_bound_window("cached binding validation failed")

        # 尝试 bind
        logger.info("未检测到 xiadan 窗口，尝试 lazy bind...")
        self.bind_client()
        if self.hwnd_main and self.hwnd_main > 0 and self._bound_window_is_valid():
            logger.info("✓ 成功绑定到 xiadan 窗口: hwnd=%s", self.hwnd_main)
            return None

        # bind 失败
        logger.error("✗ 未检测到 xiadan 窗口（window_title 为空或窗口未运行）")
        return contract.fail(contract.CODE_NOT_BOUND, CLS_NOT_BOUND,
                             "未检测到 xiadan 窗口（请确保同花顺已打开并登录）")

    def bind_client(self):
        # Exact FindWindow is deliberately not used: an exact-title window and
        # a broker-suffixed one both match the configured prefix, so accepting
        # the exact one would bypass the ambiguity protection.
        self._clear_bound_window("rebinding")
        hwnd = find_window_by_title_prefix(window_title)
        if hwnd <= 0:
            return
        identity = _window_process_identity(hwnd)
        if not identity:
            logger.error("refusing to bind hwnd=%s: process identity unavailable", hwnd)
            return
        self.hwnd_main = hwnd
        self._bound_pid, self._bound_executable = identity
        if not self._activate_bound_window():
            self._clear_bound_window("window did not become foreground after bind")

    def kill_client(self):
        """Close only the uniquely bound client; never Alt+F4 a title lookup."""
        if self._ensure_bound():
            return False
        try:
            self._send_hotkey(["alt", "F4"], "kill_client")
        except WindowSafetyError as e:
            logger.warning("refusing to close client: %s", e)
            return False
        self._clear_bound_window("close requested")
        return True

    def get_tree_hwnd(self):
        # 结构链保持不变，仅把带 MFC 版本号的类名(AfxMDIFrame140s/AfxWnd140s)换成
        # 前缀匹配，版本号变了也不断链；HexinScrollWnd/SysTreeView32 名称稳定，精确匹配。
        hwnd = self._child_by_class_prefix(self.hwnd_main, "AfxMDIFrame")
        hwnd = self._child_by_class_prefix(hwnd, "AfxWnd")
        hwnd = win32gui.FindWindowEx(hwnd, None, None, "HexinScrollWnd")
        hwnd = self._child_by_class_prefix(hwnd, "AfxWnd")
        hwnd = win32gui.FindWindowEx(hwnd, None, "SysTreeView32", None)
        return hwnd

    def get_right_hwnd(self):
        hwnd = self._child_by_class_prefix(self.hwnd_main, "AfxMDIFrame")
        hwnd = win32gui.GetDlgItem(hwnd, 0xE901) if hwnd else 0
        return hwnd

    def get_left_bottom_tabs(self):
        hwnd = self._child_by_class_prefix(self.hwnd_main, "AfxMDIFrame")
        hwnd = self._child_by_class_prefix(hwnd, "AfxWnd")
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

    def _read_account_selector_text(self) -> Optional[str]:
        """按控件 ID/class 重新定位当前账户项并读取其文本。

        不缓存账户控件 HWND：同花顺重启或切换页面后 HWND 会变化。控件可能处于
        隐藏模板层，所以这里不能要求 visible=True；但目标必须仍属于已绑定的
        xiadan 进程，且占位文本不算账户身份。
        """
        ctrl = self._find_ctrl_by_id(
            self.hwnd_main, ACCOUNT_SELECTOR_ID, cls=ACCOUNT_SELECTOR_CLASS
        )
        if not ctrl or not self._window_is_owned_by_bound_process(ctrl):
            return None
        text = (get_text(ctrl) or "").strip()
        if text in ACCOUNT_TEXT_PLACEHOLDERS:
            return None
        return text

    @property
    def account_trading_blocked(self) -> bool:
        """Whether account identity currently blocks every trading action."""
        return self._account_trading_blocked

    def require_explicit_account_selection(self) -> None:
        """Clear a prior selection when the control connection starts a new session."""
        self._last_account_text = None
        self._account_trading_blocked = True
        logger.info("账户交易核验已重置，等待 switch_account 明确选择")

    @guarded
    def _verify_account_for_trade(self) -> dict[str, Any]:
        """建立或核对交易账户基线；失败时阻断所有交易动作。

        账户必须先由明确的 switch_account(slot) 与下拉列表槽位核验。此后
        buy/sell/cancel 与人工单 confirm_external_cancel 每次执行前都必须读取到
        相同文本，避免用户手动切换同花顺账户后订单落入错误账户。
        """
        self._abort_if_stale("verify_account_for_trade")
        current = self._read_account_selector_text()
        expected = self._last_account_text
        if not current:
            self._account_trading_blocked = True
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "交易前无法读取当前账户（控件 ID 0x094C），已禁止买卖和撤单",
                data={
                    "account_verified": False,
                    "expected_account_text": expected,
                    "account_text": None,
                    "submitted": False,
                },
            )

        if expected is None:
            self._account_trading_blocked = True
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "当前账户尚未通过 switch_account 明确核验，已禁止买卖和撤单；"
                "请先调用 list_accounts，再调用 switch_account 选择账户",
                data={
                    "account_verified": False,
                    "account_text": current,
                    "submitted": False,
                },
            )

        if current != expected:
            self._account_trading_blocked = True
            logger.error("交易前账户不一致：expected=%r actual=%r", expected, current)
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "交易前账户已变化（期望：%s，当前：%s），已禁止买卖和撤单；"
                "请确认同花顺账户后调用 switch_account 重新核验" % (expected, current),
                data={
                    "account_verified": False,
                    "expected_account_text": expected,
                    "account_text": current,
                    "submitted": False,
                },
            )

        self._account_trading_blocked = False
        return contract.ok({
            "account_verified": True,
            "account_text": current,
            "account_baseline_established": False,
        })

    async def verify_account_for_trade(self) -> dict[str, Any]:
        """Async trading preflight; caller must already hold ``win_lock``."""
        bound_err = self._ensure_bound()
        if bound_err:
            self._account_trading_blocked = True
            return bound_err
        return await asyncio.to_thread(self._verify_account_for_trade)

    def _find_grid(self, root: int) -> int:
        """只找当前可见面板的表格控件(0x417)。

        右区会同时挂载多个页面的 grid；若页面未切换，回退读取隐藏 grid 会把持仓等旧页面
        伪装成当前查询结果。因此找不到可见 grid 时宁可返回 0 并拒绝读取。
        """
        return (
            self._find_ctrl_by_id(root, 0x417, cls="CVirtualGridCtrl", visible=True)
            or self._find_ctrl_by_id(root, 0x417, visible=True)
        )

    @staticmethod
    def _child_by_class_prefix(parent: int, prefix: str) -> int:
        """在 parent 的直接子窗口里找第一个【类名以 prefix 开头】的，绕开 MFC 版本号后缀。

        get_tree/right/tabs 的父子链原本写死 AfxMDIFrame140s / AfxWnd140s，其中 140
        = MFC 14.0。同花顺一旦换 MFC 工具链重编，后缀会变(如 142s) → FindWindowEx
        精确匹配失效。用前缀匹配只锁 "AfxMDIFrame"/"AfxWnd" 语义部分，版本号无关。
        """
        if not parent:
            return 0
        h = 0
        while True:
            h = win32gui.FindWindowEx(parent, h, None, None)
            if h == 0:
                return 0
            try:
                if win32gui.GetClassName(h).startswith(prefix):
                    return h
            except Exception:
                pass

    def _find_input(self, root: int, cid: int) -> int:
        """找下单表单输入框(证券代码0x408/价格0x409/数量0x40A)。优先【可见的 Edit】，
        逐级放宽 —— 右区同时挂着买/卖等多个表单，只有当前面板的可见。"""
        return (
            self._find_ctrl_by_id(root, cid, cls="Edit", visible=True)
            or self._find_ctrl_by_id(root, cid, cls="Edit")
            or self._find_ctrl_by_id(root, cid, visible=True)
            or self._find_ctrl_by_id(root, cid)
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
        try:
            self._switch_to_normal_safely()
            self._send_hotkey(["F3"], "bulk_cancel_panel")
            self.refresh(require_window_safety=True)
            right = self.get_right_hwnd()
            btn = self._find_ctrl_by_id(right, btn_id, cls="Button", visible=True) \
                or self._find_ctrl_by_id(right, btn_id, cls="Button")
        except WindowSafetyError as e:
            return self._pre_submit_failure(str(e))
        except Exception as e:
            return {"code": 1, "status": "failed",
                    "msg": f"GetDlgItem 0x{btn_id:04X}: {e}"}
        if not btn:
            return {"code": 1, "status": "failed",
                    "msg": f"button 0x{btn_id:04X} not present in F3 panel"}
        try:
            # BM_CLICK is focus-independent, but must still target a control
            # owned by the client we verified at bind time.
            self._post_owned_button_click(btn, f"bulk_cancel_{action}")
        except WindowSafetyError as e:
            return self._pre_submit_failure(str(e))
        time.sleep(sleep_time)
        # "您确定要撤销..." 确认框 / 验证码：结构化处置 + 存证（取代盲 Enter）。
        try:
            pump = self._pump_dialogs()
        except (StaleCallAborted, WindowSafetyError) as e:
            return contract.submitted_unconfirmed(
                f"批量撤单动作已发出，但后续弹窗核验未完成：{e}",
                data={"action": action, "submitted": True},
            )
        return pump.attach_to({
            "code": 0,
            "status": "succeed",
            "action": action,
            "button_id": f"0x{btn_id:04X}",
        })

    def cancel_all(self):
        return self._bulk_cancel("all")

    def cancel_buy(self):
        return self._bulk_cancel("buy")

    def cancel_sell(self):
        return self._bulk_cancel("sell")

    def cancel_last(self):
        return self._bulk_cancel("last")

    @guarded
    def get_balance(self):
        # 多账户登录时每个账户各挂一套同 ID 资金控件，只有当前账户的可见；
        # 不按可见性过滤会读到其他账户隐藏面板的数字（2026-07-14 双账户
        # 切换演练：Alt+2 已切到账户二，balance 仍返回账户一全套数字）。
        # 只认可见控件、读不到重试后明确报错——不做未过滤兜底：兜底在面板
        # 加载间隙同样可能抓到其他账户的隐藏副本，真钱 sizing 宁可失败不可读错。
        for retry in range(retry_time):
            self.switch_to_normal()
            hot_key(["F4"])
            self.refresh()
            hwnd = self.get_right_hwnd()
            data = {}
            for key, cid in BALANCE_CONTROL_ID_GROUP.items():
                ctrl = self._find_ctrl_by_id(hwnd, cid, visible=True)
                if ctrl > 0:
                    data[key] = get_text(ctrl)
            if data:
                normalized = normalize_balance(data)
                self.state.update("balance", normalized)
                return contract.ok(normalized)
            time.sleep(sleep_time)
        return contract.fail(
            contract.CODE_READ_FAILED, CLS_READ_FAILED,
            "未找到可见的资金面板控件（面板未加载完或客户端异常），"
            "已放弃读取——不回退读隐藏面板（多账户下可能是其他账户的数字），请稍后重试")

    # 抓表重试上限：翻页键没落到 xiadan 时抓到的是上一张表，重抓一次通常就对了；
    # 三次仍不对说明面板真的没切过去，明确失败 —— 绝不 succeed 携错表出门。
    _GRID_ATTEMPTS = 3

    @property
    def ledger(self):
        """下单台账（懒加载）。拿不到就返回 None——回显是增强字段，不阻断查询。"""
        if self._ledger is None:
            try:
                from ..config import app_data_dir
                from ..order_ledger import OrderLedger
                self._ledger = OrderLedger(app_data_dir() / "orders.db")
            except Exception:
                logger.warning("下单台账不可用，client_order_id 本次不回显", exc_info=True)
                return None
        return self._ledger

    def _coid_map(self) -> dict:
        led = self.ledger
        if led is None:
            return {}
        try:
            return led.coid_by_entrust()
        except Exception:
            logger.warning("台账 join 失败，本次不回显 client_order_id", exc_info=True)
            return {}

    def _grab_grid(self, kind: str, goto, label: str, normalize=None):
        """翻页→抓表→**校验表头归属**→解析。错表即重抓，仍不对则显式 failed。

        2026-08-03 串线事故的正面修复：翻页快捷键是全局按键，没落到 xiadan 时
        grid 里还是上一次查询的表，Ctrl+C 原样抓走，过去非空即 code=0 出门。
        """
        self._last_grid_columns.pop(kind, None)
        self._last_grid_verified_empty.discard(kind)
        self._last_grid_debug.pop(kind, None)
        got_columns: list[str] = []
        reason = ""
        navigation_failed = False
        for attempt in range(1, self._GRID_ATTEMPTS + 1):
            navigated = goto()
            if navigated is False:
                navigation_failed = True
                logger.warning("%s 未能导航到目标页面（第 %d/%d 次），未读取当前残留表格",
                               label, attempt, self._GRID_ATTEMPTS)
                time.sleep(sleep_time)
                continue
            hwnd = self.get_right_hwnd()
            ctrl = self._find_grid(hwnd)
            data = self.read_table_text(ctrl) if ctrl else None
            if data is _VERIFIED_EMPTY_GRID:
                logger.info("%s 已通过拷贝验证码核验为空表（客户端未输出表头）", label)
                self._last_grid_verified_empty.add(kind)
                self.state.update(kind, [])
                return contract.ok([])
            if data:
                # 表头取自原始文本而非解析结果：空表（今天无挂单/无成交）是合法
                # 结果，它照样有表头，必须能通过校验并以 data=[] 正常返回。
                got_columns = table_columns(data)
                reason = check_table(kind, got_columns) or ""
                parsed = parse_table(data)
                if not reason:
                    rows = normalize(parsed) if normalize else parsed
                    self._last_grid_columns[kind] = tuple(got_columns)
                    self._last_grid_debug[kind] = {
                        "clipboard_text": data,
                        "normalized_rows": rows,
                    }
                    self.state.update(kind, rows)
                    return contract.ok(rows)
                logger.warning("%s 抓到错表（第 %d/%d 次）：%s cols=%r",
                               label, attempt, self._GRID_ATTEMPTS, reason, got_columns)
            time.sleep(sleep_time)
        if reason:
            return contract.fail(
                contract.CODE_TABLE_MISMATCH, CLS_TABLE_MISMATCH,
                f"{label}：抓到的不是本次请求的表（{reason}），"
                f"重抓 {self._GRID_ATTEMPTS} 次仍不符，已拒绝返回错表，请稍后重试",
                data={"got_columns": got_columns})
        if navigation_failed:
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                f"{label}：未能导航到目标页面，已中止读取以避免使用残留表格，请稍后重试")
        return contract.fail(contract.CODE_READ_FAILED, CLS_READ_FAILED,
                             f"{label}：读取数据失败（可能验证码弹窗或刷新超时），请稍后重试")

    @guarded
    def get_position(self):
        def goto():
            self.switch_to_normal()
            hot_key(["F1"])
            hot_key(["F6"])
            self.refresh()

        return self._grab_grid(
            "position", goto, "持仓查询",
            normalize=lambda rows: [normalize_position_row(r) for r in rows])

    def get_gupiao(self):
        for retry in range(retry_time):
            self.switch_to_normal()
            hot_key(["F4"])
            self.refresh()
            hwnd = self.get_right_hwnd()
            ctrl = self._find_grid(hwnd)
            data = self.read_table_text(ctrl)
            if data:
                parsed = parse_table(data)
                self.state.update("gupiao", parsed)
                return {"code": 0, "status": "succeed", "data": parsed}
            time.sleep(sleep_time)
        return {"code": 1, "status": "failed", "msg": "读取数据失败（可能验证码弹窗或刷新超时），请稍后重试"}

    @guarded
    def get_active_orders(self):
        # 最险的一条：错表被消费侧读成「无挂单」→ 孤儿单存活、止损哨兵被架空。
        def goto():
            self.switch_to_normal()
            _activate_window(self.hwnd_main)
            # 当前 Windows 实机验证 F1 -> F8 可切到当日委托；保留表头校验和只读可见
            # grid 的约束，热键失效时会安全拒绝错表，不能把持仓表当委托表返回。
            hot_key(["F1"])
            hot_key(["F8"])
            self.refresh()

        # C3：只返回在飞单。终态（已成/已撤/废单/全部成交）不出现在本表；
        # **状态识别不出来的一律按在飞返回**——宁可多给一行让消费侧看见，也不能
        # 把一张活着的挂单藏起来（孤儿单架空止损哨兵是最险的失效模式）。
        return self._grab_active(goto, include_terminal=False)

    def get_active_orders_all(self):
        """委托表全量（含终态），**内部用**：order_watch 靠终态行 diff 出
        filled/canceled 事件，用过滤后的表会把这些事件全丢掉。
        对外 RPC 的 orders_active 只给在飞单（C3）。"""
        def goto():
            self.switch_to_normal()
            _activate_window(self.hwnd_main)
            hot_key(["F1"])
            hot_key(["F8"])
            self.refresh()

        return self._grab_active(goto, include_terminal=True)

    def _grab_active(self, goto, include_terminal: bool):
        coid_map = self._coid_map()

        def is_empty_order_placeholder(raw: dict[str, Any]) -> bool:
            """识别空委托表带时间/展示列的残留占位行。

            同花顺有时在没有任何委托时仍复制一行时间或市场展示值，因此通用
            ``parse_table`` 不会把它视作全空行。只有订单身份及数量/价格等核心字段
            全部为占位值才过滤；有任一真实订单字段却没有合同号的行仍必须拒绝基线。
            """
            identity_fields = (
                "合同编号", "委托编号", "证券代码", "操作", "买卖标志", "买卖",
                "委托数量", "委托价格", "委托价", "成交数量",
            )
            return all(
                _is_phantom_value(raw.get(field, ""))
                for field in identity_fields
            )

        def normalize(rows):
            out = []
            for raw in rows:
                if is_empty_order_placeholder(raw):
                    logger.debug("委托查询过滤空表展示占位行：keys=%r", list(raw))
                    continue
                row = normalize_active_row(raw, coid_map)
                if include_terminal or is_in_flight(
                    row["状态"], row["委托数量"], row["已成数量"]):
                    out.append(row)
            return out

        return self._grab_grid("active_orders", goto, "委托查询", normalize=normalize)

    @guarded
    def get_filled_orders(self):
        def goto():
            self.switch_to_normal()
            _activate_window(self.hwnd_main)
            hot_key(["F2"])
            hot_key(["F7"])
            self.refresh()

        coid_map = self._coid_map()
        return self._grab_grid(
            "filled_orders", goto, "成交查询",
            normalize=lambda rows: [normalize_filled_row(r, coid_map) for r in rows])

    # --- 自选股（新版专有）------------------------------------------------
    # 新版 xiadan 的自选股是内嵌 CEF(Chromium) 渲染的网页，没有原生表格控件、无 CDP
    # 调试口、本地 SelfStockInfo.json 由行情 app 写(常过期)。唯一能实时拿到的方式是
    # 截图 + OCR。用同花顺的习惯：新加的自选股出现在顶部，所以只读第一屏(顶部)即可
    # 覆盖"检测新增"；全量需滚屏(CEF 不吃 WM_MOUSEWHEEL，暂不支持)。旧版无此菜单。

    def _capture_window_png(self, hwnd):
        """DPI 感知 PrintWindow(PW_RENDERFULLCONTENT) 截取窗口 → PIL.Image。
        能截 Chromium/CEF（BitBlt 会黑屏）；2x 屏(Parallels/Retina)切 Per-Monitor-V2
        让 GetWindowRect 返回物理像素，不截半张。"""
        user32 = ctypes.windll.user32
        old = None
        if hasattr(user32, "SetThreadDpiAwarenessContext"):
            try:
                old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PMv2
            except Exception:
                old = None
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            w, h = r - l, b - t
            hdc = win32gui.GetWindowDC(hwnd)
            dc = win32ui.CreateDCFromHandle(hdc)
            cdc = dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(dc, w, h)
            cdc.SelectObject(bmp)
            user32.PrintWindow(hwnd, cdc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
            bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1)
            win32gui.DeleteObject(bmp.GetHandle())
            cdc.DeleteDC()
            dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hdc)
            return img
        finally:
            if old is not None:
                try:
                    user32.SetThreadDpiAwarenessContext(old)
                except Exception:
                    pass

    def _open_account_dropdown(self) -> bool:
        """点击当前快照确认的账户 ComboBox。"""
        combo = self._find_ctrl_by_id(
            self.hwnd_main, ACCOUNT_DROPDOWN_ID, cls="ComboBox", visible=True
        )
        if (combo
                and win32gui.IsWindowEnabled(combo)
                and self._window_is_owned_by_bound_process(combo)):
            left, top, right, bottom = win32gui.GetWindowRect(combo)
            self._click_screen((left + right) // 2, (top + bottom) // 2,
                               "account_dropdown")
            return True
        raise RuntimeError(
            f"未找到可见且属于当前同花顺进程的账户 ComboBox（ID=0x{ACCOUNT_DROPDOWN_ID:04X}）；"
            "未点击、未读取账户、未切换账户"
        )

    def _find_open_account_listbox(self) -> int:
        """Find the visible standard dropdown ListBox confirmed by the live snapshot."""
        matches: list[int] = []

        def visit(hwnd, _):
            try:
                if (
                    win32gui.IsWindowVisible(hwnd)
                    and win32gui.GetDlgCtrlID(hwnd) == ACCOUNT_LISTBOX_ID
                    and win32gui.GetClassName(hwnd) == ACCOUNT_LISTBOX_CLASS
                ):
                    matches.append(hwnd)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(visit, None)
        if len(matches) != 1:
            raise RuntimeError(
                "未唯一定位到账户下拉列表：期望可见 %s / ID=0x%04X，实际命中=%s"
                % (ACCOUNT_LISTBOX_CLASS, ACCOUNT_LISTBOX_ID,
                   [f"0x{hwnd:X}" for hwnd in matches])
            )
        return matches[0]

    def _read_account_listbox_items(self, listbox: int) -> list[str]:
        """Read standard ListBox rows without changing its selected item."""
        # ctypes.windll.user32 is process-global. Do not set SendMessageW.argtypes
        # there: other paths intentionally use its two-argument form to read
        # controls such as 0x094C, and a global four-argument signature makes
        # every later account read fail with "takes 4 arguments (2 given)".
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        send_message = user32.SendMessageW
        send_message.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        )
        send_message.restype = ctypes.c_ssize_t
        count = send_message(listbox, LB_GETCOUNT, 0, 0)
        if count == LB_ERR or count < 0:
            raise RuntimeError(f"账户下拉列表 LB_GETCOUNT 失败，返回 {count}")
        items: list[str] = []
        for index in range(count):
            length = send_message(listbox, LB_GETTEXTLEN, index, 0)
            if length == LB_ERR or length < 0:
                raise RuntimeError(
                    f"账户下拉列表 LB_GETTEXTLEN 失败，index={index}，返回 {length}"
                )
            buffer = ctypes.create_unicode_buffer(length + 1)
            copied = send_message(
                listbox, LB_GETTEXT, index, ctypes.addressof(buffer)
            )
            if copied == LB_ERR:
                raise RuntimeError(f"账户下拉列表 LB_GETTEXT 失败，index={index}")
            items.append(buffer.value)
        return items

    @guarded
    def get_account_list(self):
        """展开账户下拉框并读取标准 ListBox 文本；不选择、不切换账户。"""
        self._switch_to_normal_safely()
        current = self._read_account_selector_text()
        opened = False
        try:
            opened = self._open_account_dropdown()
            time.sleep(ACCOUNT_DROPDOWN_SETTLE_SECS)
            listbox = self._find_open_account_listbox()
            candidates = _account_candidates_from_listbox(
                self._read_account_listbox_items(listbox)
            )
            if not candidates:
                return contract.fail(
                    contract.CODE_READ_FAILED, CLS_READ_FAILED,
                    "账户下拉框已打开，但未读取到可切换账户；未切换账户",
                    data={"accounts": [], "current_account_text": current,
                          "partial": True, "submitted": False},
                )

            accounts = [
                {
                    "slot": item["slot"],
                    "shortcut": item["shortcut"],
                    "text": item["text"],
                }
                for item in candidates
            ]
            return contract.ok({
                "accounts": accounts,
                "current_account_text": current,
                "partial": False,
                "source": "listbox_text",
                "msg": "已读取账户下拉列表原始文本，未选择或切换任何账户",
            })
        except Exception as e:
            logger.warning("account list ListBox read failed: %s", e, exc_info=True)
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                f"读取账户下拉列表失败：{e}；未切换账户",
                data={"accounts": [], "current_account_text": current,
                      "partial": True, "submitted": False},
            )
        finally:
            if opened:
                try:
                    self._send_hotkey(["esc"], "close_account_dropdown")
                except Exception:
                    logger.warning("账户下拉框关闭失败，请人工确认当前界面", exc_info=True)

    def _ocr_leftmost_codes(self, img) -> list[str]:
        """OCR 图中 6 位数字，只取【最左一簇】(x 最小)=代码列，排除右侧数字列(主力净额/
        总金额等)产生的假 6 位数。去重保序（顶部在前）。"""
        from pytesseract import Output
        d = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=Output.DICT)
        got = [(t.strip(), d["left"][i]) for i, t in enumerate(d["text"])
               if re.fullmatch(r"\d{6}", (t or "").strip())]
        if not got:
            return []
        min_x = min(x for _, x in got)
        seen, out = set(), []
        for t, x in got:
            if x <= min_x + 120 and t not in seen:  # 容差 120 物理像素
                seen.add(t)
                out.append(t)
        return out

    @guarded
    def get_watchlist(self):
        """读自选股代码（截图+OCR 代码列）。仅第一屏(顶部)——新增出现在顶部，足够检测
        新增；全量需滚屏(CEF 暂不支持)。旧版无此菜单会返回错误。"""
        self.switch_to_normal()
        if not self._select_tree_node_by_text("自选股"):
            return contract.fail(contract.CODE_READ_FAILED, CLS_READ_FAILED,
                                 "未找到自选股菜单（旧版 xiadan 无此菜单，请用新版）")
        time.sleep(1.0)  # 等内嵌 CEF 渲染出自选股（0.2s 太短）
        try:
            # 截整个窗口：PrintWindow(PW_RENDERFULLCONTENT) 会把内嵌 CEF 的自选股一并截到；
            # 直接找 CEF 子窗口常 IsWindowVisible=False 找不到，截整窗更稳（代码列 OCR 用
            # 最左簇过滤，自然排除左侧菜单与右侧数字列）。
            img = self._capture_window_png(self.hwnd_main)
            codes = self._ocr_leftmost_codes(img)
        except Exception as e:
            logger.exception("get_watchlist OCR failed")
            return contract.fail(contract.CODE_READ_FAILED, CLS_READ_FAILED,
                                 f"自选股截图/OCR 失败: {e}")
        if not codes:
            return contract.fail(contract.CODE_READ_FAILED, CLS_READ_FAILED,
                                 "OCR 未识别到自选股代码（面板可能未切到自选 tab）")
        self.state.update("watchlist", codes)
        # partial=True：仅顶部第一屏（CEF 不支持滚屏），契约里写明非全量。
        return contract.ok({"count": len(codes), "partial": True, "codes": codes})

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
        # 目标位数决定 TVITEM 布局：xiadan 是 32 位 → 用 4 字节指针的 _TVITEM32，否则
        # 64 位结构发给 32 位目标会错位 → TVM_GETITEMW 失败、文字读空。
        is32 = _proc_is_wow64(pid)
        TVITEM = _TVITEM32 if is32 else _TVITEMW
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
        remote_item = k32.VirtualAllocEx(int(h_proc), None, ctypes.sizeof(TVITEM), MEM, PAGE_RW)
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
                item = TVITEM()
                item.mask = TVIF_TEXT
                item.hItem = (hitem & 0xFFFFFFFF) if is32 else hitem
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

    def _select_tree_path(self, path: tuple[str, ...],
                          require_window_safety: bool = False) -> bool:
        """按左树完整路径精确匹配节点，再选中并点击最终节点。

        市价入口的文字在不同券商版本中可能不同，且“买入”会与普通 ``买入[F1]`` 重名。
        因此不做深度优先子串搜索；调用方必须提供从顶层开始的完整路径。当前真机路径为
        ``("市价买入",)`` 或 ``("市价卖出",)``。

        跨进程 TreeView 读写/位数处理/DPI 点击与 _select_tree_node_by_text 同构；那套原语有
        交割单/自选股导航依赖，为免在无法回归的环境重构破坏，这里独立实现，真机稳定后可再合并。
        """
        tree = self.get_tree_hwnd()
        if not tree:
            logger.warning("market: tree hwnd not found")
            return False
        if require_window_safety:
            self._require_owned_window_for_input(tree, "market_tree_navigation")
        _, pid = win32process.GetWindowThreadProcessId(tree)
        is32 = _proc_is_wow64(pid)
        TVITEM = _TVITEM32 if is32 else _TVITEMW
        PROCESS_VM = 0x0008 | 0x0010 | 0x0020
        MEM = 0x1000 | 0x2000
        PAGE_RW = 0x04
        MEM_RELEASE = 0x8000
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        k32.VirtualAllocEx.restype = ctypes.c_void_p
        k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
        k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]

        h_proc = win32api.OpenProcess(PROCESS_VM, False, pid)
        bufsize = 512
        remote_text = k32.VirtualAllocEx(int(h_proc), None, bufsize, MEM, PAGE_RW)
        remote_item = k32.VirtualAllocEx(int(h_proc), None, ctypes.sizeof(TVITEM), MEM, PAGE_RW)
        try:
            if not remote_text or not remote_item:
                logger.warning("market: VirtualAllocEx failed")
                return False

            def _norm(s: str) -> str:
                return s.replace(" ", "").replace("　", "")

            normalized_path = tuple(_norm(item) for item in path)
            if not normalized_path or any(not item for item in normalized_path):
                logger.warning("market: invalid tree path %r", path)
                return False

            def read_text(hitem: int) -> str:
                item = TVITEM()
                item.mask = TVIF_TEXT
                item.hItem = (hitem & 0xFFFFFFFF) if is32 else hitem
                item.pszText = remote_text
                item.cchTextMax = bufsize // 2
                k32.WriteProcessMemory(int(h_proc), remote_item,
                                       ctypes.byref(item), ctypes.sizeof(item), None)
                win32gui.SendMessage(tree, TVM_GETITEMW, 0, remote_item)
                buf = (ctypes.c_char * bufsize)()
                k32.ReadProcessMemory(int(h_proc), remote_text, buf, bufsize, None)
                return buf.raw.decode("utf-16-le", "ignore").split("\x00", 1)[0]

            def find_sibling(hitem: int, target: str) -> int:
                seen: list[str] = []
                while hitem:
                    text = _norm(read_text(hitem))
                    seen.append(text)
                    if text == target:
                        return hitem
                    hitem = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_NEXT, hitem)
                logger.warning("market: path component %r not found; siblings=%r", target, seen)
                return 0

            node = 0
            siblings = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_ROOT, 0)
            for component in normalized_path:
                node = find_sibling(siblings, component)
                if not node:
                    logger.warning("market: tree path %r not found", path)
                    return False
                siblings = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_CHILD, node)

            # 选中 + 真实鼠标点击（触发右侧面板切换；同 _select_tree_node_by_text）
            win32gui.SendMessage(tree, TVM_SELECTITEM, TVGN_CARET, node)
            k32.WriteProcessMemory(int(h_proc), remote_text,
                                   ctypes.byref(ctypes.c_ssize_t(node)),
                                   ctypes.sizeof(ctypes.c_ssize_t), None)
            got = win32gui.SendMessage(tree, TVM_GETITEMRECT, 0, remote_text)
            if not got:
                logger.info("market: selected (no rect, 程序化) path %r", path)
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
                if require_window_safety:
                    self._click_screen(pt.x, pt.y, "market_tree_navigation")
                else:
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
            logger.info("market: clicked tree path %r at client(%d,%d)", path, cx, cy)
            return True
        finally:
            if remote_text:
                k32.VirtualFreeEx(int(h_proc), remote_text, 0, MEM_RELEASE)
            if remote_item:
                k32.VirtualFreeEx(int(h_proc), remote_item, 0, MEM_RELEASE)
            win32api.CloseHandle(h_proc)

    def _select_market_tree_path(self, op_keyword: str) -> bool:
        """选择当前客户端实际存在的市价买卖路径，兼容两种已验证菜单结构。"""
        paths = MARKET_TREE_PATHS.get(op_keyword, ())
        for path in paths:
            if self._select_tree_path(path, require_window_safety=True):
                logger.info("market: selected compatible path %r for %s", path, op_keyword)
                return True
        logger.warning("market: no compatible path found for %s; attempted=%r", op_keyword, paths)
        return False

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

    # 交割单时段按钮的控件 ID（实测规整连续，见 tools/dump_settlement_buttons.py）。
    # 5 个查询面板各有一套同 ID 副本，只有当前交割单面板那套可见 → 用可见性过滤命中。
    _SETTLEMENT_RANGE_IDS = {
        "近一周": 0x14BC,
        "近一月": 0x14BD,
        "近三月": 0x14BE,
        "近一年": 0x14BF,
    }

    def _click_settlement_range(self, date_range: str) -> bool:
        """按【控件 ID + 可见性】点交割单时段按钮，取代按文字匹配（零文字依赖）。"""
        cid = self._SETTLEMENT_RANGE_IDS.get(date_range)
        if cid is None:
            logger.warning("settlement: 未知时段 %r（支持 %s）",
                           date_range, list(self._SETTLEMENT_RANGE_IDS))
            return False
        # 全窗口找【可见】的那个：5 面板各有一套同 ID 副本，只有当前交割单面板的可见。
        btn = self._find_ctrl_by_id(self.hwnd_main, cid, cls="Button", visible=True)
        if not btn:
            logger.warning("settlement: 时段按钮 0x%04X(%s) 无可见实例", cid, date_range)
            return False
        self._real_click_hwnd(btn)
        logger.info("settlement: 点击时段 %s id=0x%04X hwnd=%s", date_range, cid, hex(btn))
        return True

    # 交割单独有、资金股票/持仓没有的列，用来校验确实切到了交割单面板
    _SETTLEMENT_MARKER_COLS = ("发生金额", "成交编号", "印花税", "成交日期")

    def _goto_settlement_panel(self) -> None:
        """F4 进查询(展开+落资金股票) → 按标签选中「交割单」树节点(触发面板切换)。

        导航靠位数感知的 TVM_GETITEM 读树文字、按标签定位——免疫菜单重排，取代旧的
        「数 8 次 Down 键」脆弱走位。失败只记日志，由调用方的列名校验(_SETTLEMENT_
        MARKER_COLS)兜底：切错面板会返回错误而非把别的面板数据当交割单。
        """
        self.switch_to_normal()
        _activate_window(self.hwnd_main)
        hot_key(["F4"])  # 查询：展开并默认选中 资金股票（确保交割单节点可见可点）
        time.sleep(refresh_sleep_time)
        if self._select_tree_node_by_text("交割单", fallback_token="割"):
            time.sleep(sleep_time)
            logger.info("settlement: 按标签「交割单」导航成功")
        else:
            logger.warning("settlement: 按标签导航「交割单」失败（树文字读取或节点缺失）")

    @guarded
    def _do_settlement(self, date_range: str = "近一年"):
        """读取交割单（默认近一年）。低频功能，一次性尽量多拿。"""
        try:
            self._goto_settlement_panel()
            # 时段：按文字点「近一年」等按钮（真实 Button，可命中）
            # 大查询（如近一年 5000+ 行）THS 首次常「查询超时」、弹超时确认框、表格为空。
            # 需要重新点时段触发重查（等效用户"再点几下 tab"数据才出来），并先回车关掉可能
            # 的超时弹窗——模态框会挡住重点，必须先关。循环到读出非空为止（也顺带解决小查询
            # 过滤未落定/竞态的不完整快照：取行数最多的一轮）。
            rows: list[dict] = []
            ranged = False
            for attempt in range(6):
                hot_key(["enter"])          # 关掉上一轮可能残留的「查询超时」确认框（无框则无害）
                time.sleep(short_sleep_time)
                if self._click_settlement_range(date_range):
                    ranged = True
                time.sleep(refresh_sleep_time)
                self.refresh()
                time.sleep(refresh_sleep_time)
                hwnd = self.get_right_hwnd()
                ctrl = self._find_grid(hwnd)
                if ctrl:
                    data = self.read_table_text(ctrl)
                    if data:
                        parsed = parse_table(data)
                        if len(parsed) > len(rows):
                            rows = parsed
                        # 已拿到数据且这轮没读到更多 → 稳定，停
                        if attempt >= 1 and len(parse_table(data)) <= len(rows):
                            break
                time.sleep(refresh_sleep_time)
            if not rows:
                return contract.fail(
                    contract.CODE_READ_FAILED, CLS_READ_FAILED,
                    "交割单读取为空（大查询可能仍在超时，请稍后重试或改用更小时段）")

            # 列名校验：确认确实是交割单面板，避免把资金股票/持仓数据误当交割单返回。
            cols = set(rows[0].keys()) if rows else set()
            is_settlement = any(m in c for m in self._SETTLEMENT_MARKER_COLS for c in cols)
            if rows and not is_settlement:
                logger.warning("settlement: 面板列名不像交割单，cols=%r", list(cols))
                return contract.fail(
                    contract.CODE_TABLE_MISMATCH, CLS_TABLE_MISMATCH,
                    "未能切到交割单面板（读到的是其它面板），请重试或人工确认",
                    data={"got_columns": list(cols)})
            normalized = [normalize_settlement_row(r) for r in rows]
            self.state.update("settlement", normalized)
            return contract.ok({
                "date_range": date_range,
                "range_applied": ranged,   # False = 用了面板默认时段，需人工确认范围
                "count": len(normalized),
                "rows": normalized,
            })
        except Exception as e:
            logger.exception("settlement failed")
            return contract.fail(contract.CODE_INTERNAL_ERROR,
                                 contract.CLS_INTERNAL_ERROR, f"交割单读取异常: {e}")

    @staticmethod
    def _columns_include_any(columns: tuple[str, ...] | list[str], aliases: tuple[str, ...]) -> bool:
        return any(alias in str(column) for column in columns for alias in aliases)

    @classmethod
    def _active_order_columns_reliable(cls, columns: tuple[str, ...] | list[str]) -> bool:
        """Whether an active-order table can prove a limit-order identity.

        The generic table guard only proves that this is broadly an order table.
        Ownership needs every component of the fingerprint and a stable order
        ID; accepting a partial broker skin here would turn a later heuristic
        into a false contract-number claim.
        """
        columns = tuple(str(column) for column in columns if str(column).strip())
        required = (
            ("合同编号", "委托编号"),
            ("证券代码",),
            # 券商皮肤实际可见三种方向列名；必须与 normalize_active_row
            # 的取值别名保持一致，否则基线通过后仍无法按完整指纹认领新合同号。
            ("操作", "买卖标志", "买卖"),
            ("委托数量",),
            ("委托价格", "委托价"),
            ("成交数量",),
            ("备注", "委托状态", "状态"),
        )
        return bool(columns) and all(cls._columns_include_any(columns, aliases) for aliases in required)

    @staticmethod
    def _entrust_no(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _matching_limit_entrust_nos(cls, rows: list[dict], stock_no: Any,
                                    op_keyword: str, amount: Any, price: Any) -> list[str]:
        """Return IDs whose complete normalized fingerprint exactly matches.

        This helper does not infer any ordering from contract numbers. It is
        deliberately pure so the ambiguity rule can be unit-tested without
        Windows APIs.
        """
        try:
            target_amount = int(amount)
            target_price = round(float(price), 3)
        except (TypeError, ValueError):
            return []
        matched: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            entrust_no = cls._entrust_no(row.get("entrust_no"))
            if not entrust_no:
                continue
            if row.get("证券代码") != str(stock_no):
                continue
            if row.get("方向") != op_keyword:
                continue
            if row.get("委托数量") != target_amount:
                continue
            row_price = row.get("委托价")
            if isinstance(row_price, bool) or not isinstance(row_price, (int, float)):
                continue
            if round(float(row_price), 3) != target_price:
                continue
            matched.append(entrust_no)
        return matched

    @classmethod
    def _unique_new_limit_entrust_no(cls, rows: list[dict], baseline_entrust_nos: set[str],
                                     stock_no: Any, op_keyword: str, amount: Any,
                                     price: Any) -> tuple[str | None, int]:
        """Claim an ID only when exactly one full-fingerprint candidate is new."""
        candidates = [
            entrust_no
            for entrust_no in cls._matching_limit_entrust_nos(
                rows, stock_no, op_keyword, amount, price)
            if entrust_no not in baseline_entrust_nos
        ]
        return (candidates[0], 1) if len(candidates) == 1 else (None, len(candidates))

    def _read_limit_order_baseline(self) -> tuple[set[str] | None, dict[str, Any] | None]:
        """Read a strict, pre-submit full order-table baseline for limit orders."""
        result = self.get_active_orders_all()
        if not contract.is_succeed(result):
            detail = ((result.get("error") or {}).get("message")) or "委托表读取失败"
            return None, contract.fail(
                result.get("code") or contract.CODE_READ_FAILED,
                ((result.get("error") or {}).get("class")) or CLS_READ_FAILED,
                f"限价委托提交前无法建立可靠委托表基线（{detail}），已中止未提交",
                data={"submitted": False},
            )
        columns = self._last_grid_columns.get("active_orders", ())
        debug_snapshot = self._last_grid_debug.get("active_orders", {})
        verified_empty = "active_orders" in self._last_grid_verified_empty
        if not verified_empty and not self._active_order_columns_reliable(columns):
            self._log_limit_baseline_debug(
                "unreliable_columns", columns, debug_snapshot,
            )
            return None, contract.fail(
                contract.CODE_TABLE_MISMATCH, CLS_TABLE_MISMATCH,
                "限价委托提交前的委托表缺少合同号、代码、方向、数量、价格或状态列，"
                "无法可靠归属新订单，已中止未提交",
                data={"submitted": False, "got_columns": list(columns)},
            )
        rows = result.get("data")
        if not isinstance(rows, list):
            self._log_limit_baseline_debug(
                "non_list_rows", columns, debug_snapshot,
            )
            return None, contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "限价委托提交前的委托表结果不是行列表，无法建立可靠基线，已中止未提交",
                data={"submitted": False},
            )
        if verified_empty:
            if rows:
                return None, contract.fail(
                    contract.CODE_READ_FAILED, CLS_READ_FAILED,
                    "委托表空表核验与返回行数不一致，无法建立可靠基线，已中止未提交",
                    data={"submitted": False},
                )
            return set(), None
        ids = [self._entrust_no(row.get("entrust_no")) for row in rows if isinstance(row, dict)]
        if len(ids) != len(rows) or any(not entrust_no for entrust_no in ids) or len(set(ids)) != len(ids):
            missing_indexes = [
                index for index, row in enumerate(rows)
                if not isinstance(row, dict) or not self._entrust_no(row.get("entrust_no"))
            ]
            id_indexes: dict[str, list[int]] = {}
            for index, row in enumerate(rows):
                if isinstance(row, dict):
                    entrust_no = self._entrust_no(row.get("entrust_no"))
                    if entrust_no:
                        id_indexes.setdefault(entrust_no, []).append(index)
            duplicates = {
                entrust_no: indexes for entrust_no, indexes in id_indexes.items()
                if len(indexes) > 1
            }
            self._log_limit_baseline_debug(
                "missing_or_duplicate_contract", columns, debug_snapshot,
                missing_contract_row_indexes=missing_indexes,
                duplicate_contract_ids=duplicates,
            )
            return None, contract.fail(
                contract.CODE_TABLE_MISMATCH, CLS_TABLE_MISMATCH,
                "限价委托提交前的委托表存在缺失或重复合同号，无法证明新旧订单边界，已中止未提交",
                data={"submitted": False, "got_columns": list(columns)},
            )
        return set(ids), None

    @staticmethod
    def _log_limit_baseline_debug(reason: str, columns: tuple[str, ...] | list[str],
                                  snapshot: dict[str, Any], **details: Any) -> None:
        """将委托表基线拒绝时的剪贴板和解析中间态写入本地诊断日志。"""
        payload = {
            "reason": reason,
            "columns": list(columns),
            "clipboard_text": snapshot.get("clipboard_text"),
        "normalized_rows": snapshot.get("normalized_rows"),
            **details,
        }
        logger.error(
            "[LIMIT_BASELINE_DEBUG] %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )

    def _lookup_entrust_no(self, stock_no, op_keyword, amount, price,
                           baseline_entrust_nos: set[str] | None, timeout=8.0):
        """Find one uniquely new full-fingerprint order after a limit submit.

        The previous implementation sorted same-parameter candidates by the
        largest contract number. That assumption is not evidence of ownership:
        an old/manual/external order can have a larger number. Only a contract
        absent from the pre-submit baseline is eligible, and multiple new rows
        are explicitly unresolved.
        """
        if baseline_entrust_nos is None:
            logger.warning("lookup_entrust_no refused without a pre-submit baseline")
            return None
        deadline = time.time() + timeout
        last_seen_rows = 0
        while time.time() < deadline:
            result = self.get_active_orders_all()
            if contract.is_succeed(result):
                columns = self._last_grid_columns.get("active_orders", ())
                verified_empty = "active_orders" in self._last_grid_verified_empty
                if not verified_empty and not self._active_order_columns_reliable(columns):
                    logger.warning("lookup_entrust_no refused unreliable columns=%r", columns)
                    return None
                rows = result.get("data") or []
                if not isinstance(rows, list):
                    logger.warning("lookup_entrust_no got non-list order rows")
                    return None
                if verified_empty and rows:
                    logger.warning("lookup_entrust_no got rows with verified-empty marker")
                    return None
                last_seen_rows = len(rows)
                entrust_no, candidate_count = self._unique_new_limit_entrust_no(
                    rows, baseline_entrust_nos, stock_no, op_keyword, amount, price)
                if entrust_no:
                    logger.info(
                        "lookup_entrust_no uniquely matched new order stock=%s op=%s qty=%s price=%s -> %s",
                        stock_no, op_keyword, amount, price, entrust_no,
                    )
                    return entrust_no
                if candidate_count > 1:
                    logger.warning(
                        "lookup_entrust_no ambiguous: %d newly appeared full matches "
                        "stock=%s op=%s qty=%s price=%s",
                        candidate_count, stock_no, op_keyword, amount, price,
                    )
                    return None
            time.sleep(0.3)
        logger.warning(
            "lookup_entrust_no timeout stock=%s op=%s qty=%s price=%s rows_last=%d",
            stock_no, op_keyword, amount, price, last_seen_rows,
        )
        return None

    def _submit_trade(self, panel_key, op_keyword, stock_no, amount, price):
        """Shared conservative limit-order form-fill, submit, and ownership path."""
        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        try:
            baseline_entrust_nos, baseline_error = self._read_limit_order_baseline()
            if baseline_error:
                return baseline_error
            if baseline_entrust_nos is None:
                return self._pre_submit_failure("限价委托提交前未能建立委托表基线")

            self._switch_to_normal_safely()
            self._send_hotkey([panel_key], "limit_order_panel")
            time.sleep(sleep_time)
            hwnd = self.get_right_hwnd()
            if not hwnd:
                return self._pre_submit_failure(
                    "限价委托提交前未找到右侧下单面板",
                    contract.CODE_READ_FAILED, CLS_READ_FAILED,
                )
            code_ctrl = self._find_input(hwnd, 0x408)
            amount_ctrl = self._find_input(hwnd, 0x40A)
            if not code_ctrl or not amount_ctrl:
                return self._pre_submit_failure(
                    "限价委托提交前未找到证券代码或数量输入框",
                    contract.CODE_READ_FAILED, CLS_READ_FAILED,
                )
            self._set_owned_text(code_ctrl, stock_no, "limit_order_code")
            time.sleep(sleep_time)
            if price is not None:
                price_ctrl = self._find_input(hwnd, 0x409)
                if not price_ctrl:
                    return self._pre_submit_failure(
                        "限价委托提交前未找到价格输入框",
                        contract.CODE_READ_FAILED, CLS_READ_FAILED,
                    )
                self._set_owned_text(price_ctrl, "%.3f" % price, "limit_order_price", True)
                time.sleep(short_sleep_time)
            self._set_owned_text(amount_ctrl, str(amount), "limit_order_amount")
            time.sleep(sleep_time)

            # The last foreground check occurs immediately before Enter. Mark
            # the outcome unknown before invoking the global submit key: an
            # exception during the key event cannot prove that no submit began.
            self._send_hotkey(
                ["enter"], "limit_submit", before_dispatch=mark_submitted
            )
        except StaleCallAborted:
            raise
        except WindowSafetyError as e:
            if not submitted:
                return self._pre_submit_failure(str(e))
            return contract.submitted_unconfirmed(
                f"限价委托已进入提交动作，但前台窗口校验随后失败：{e}",
                data={"stock_no": str(stock_no), "submitted": True},
            )
        except Exception as e:
            logger.exception("limit order failed before/while submit stock=%s", stock_no)
            if not submitted:
                return self._pre_submit_failure(
                    f"限价委托提交前发生异常：{e}",
                    contract.CODE_READ_FAILED, CLS_READ_FAILED,
                )
            return contract.submitted_unconfirmed(
                f"限价委托已进入提交动作，但客户端未能确认结果：{e}",
                data={"stock_no": str(stock_no), "submitted": True},
            )

        try:
            pump = self._pump_dialogs()
            time.sleep(sleep_time)
            entrust_no = self._entrust_no(getattr(pump, "entrust_no", None))
            if not entrust_no:
                entrust_no = self._lookup_entrust_no(
                    stock_no, op_keyword, amount, price, baseline_entrust_nos)
            if entrust_no:
                return contract.ok(pump.attach_to({
                    "entrust_no": entrust_no,
                    "stock_no": str(stock_no),
                    "方向": op_keyword,
                    "委托数量": int(amount),
                    "委托价": float(price) if price is not None else None,
                    "submitted": True,
                }))
            if pump.texts:
                return contract.broker_rejected(
                    "；".join(pump.texts),
                    message="委托已进入提交动作但未能在委托表唯一确认，客户端有提示",
                    data=pump.attach_to({"stock_no": str(stock_no), "submitted": True}),
                )
            return contract.submitted_unconfirmed(
                "限价委托已提交，但委托表中没有唯一新增的完整匹配订单；未认领合同号。"
                "请用同一 client_order_id 查询或人工核对，勿改单重下",
                data=pump.attach_to({"stock_no": str(stock_no), "submitted": True}),
            )
        except StaleCallAborted as e:
            return contract.submitted_unconfirmed(
                f"限价委托已提交，但后续核验被作废：{e}",
                data={"stock_no": str(stock_no), "submitted": True},
            )
        except Exception as e:
            logger.exception("limit order post-submit verification failed stock=%s", stock_no)
            return contract.submitted_unconfirmed(
                f"限价委托已提交，但后续核验失败：{e}",
                data={"stock_no": str(stock_no), "submitted": True},
            )

    @guarded
    def _do_sell(self, stock_no, amount, price):
        # price is None ⇒ 真·市价委托(五档即成剩撤)；有值 ⇒ F2 限价挂单(原逻辑)。
        if price is None:
            return self._submit_market_trade("卖出", stock_no, amount)
        return self._submit_trade("F2", "卖出", stock_no, amount, price)

    @guarded
    def _do_buy(self, stock_no, amount, price):
        if price is None:
            return self._submit_market_trade("买入", stock_no, amount)
        return self._submit_trade("F1", "买入", stock_no, amount, price)

    def _set_market_strategy(self, combo, key, expected_index):
        """委托策略 ComboBox 切到五档即成剩撤：键盘位置数字(WM_CHAR)→CB_GETCURSEL 校验，
        未命中回退 CB_SETCURSEL(index)。返回是否已确为期望项。

        键盘法（真机验证优先）：标准 ComboBox 收到数字字符 → 增量匹配"以该数字开头"的项
        （买入'1'→'1-...'、卖出'4'→'4-五档即成剩撤'），且能触发同花顺的策略变更处理。
        跨进程发键需 AttachThreadInput + SetFocus，否则 WM_CHAR 落不到目标控件。"""
        self._require_foreground_for_input("market_strategy")
        self._require_owned_window_for_input(combo, "market_strategy")
        CB_GETCURSEL, CB_SETCURSEL = 0x0147, 0x014E
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        my_tid = kernel32.GetCurrentThreadId()
        tgt_tid, _ = win32process.GetWindowThreadProcessId(combo)
        attached = False
        try:
            if my_tid != tgt_tid:
                attached = bool(user32.AttachThreadInput(my_tid, tgt_tid, True))
            user32.SetFocus(combo)
            user32.SendMessageW(combo, win32con.WM_CHAR, ord(key), 0)
        finally:
            if attached:
                user32.AttachThreadInput(my_tid, tgt_tid, False)
        time.sleep(short_sleep_time)
        idx = win32gui.SendMessage(combo, CB_GETCURSEL, 0, 0)
        if idx != expected_index:
            # 键盘法未命中 → 程序化兜底设选中项
            logger.info("market strategy keyboard set idx=%s != %s, fallback CB_SETCURSEL",
                        idx, expected_index)
            win32gui.SendMessage(combo, CB_SETCURSEL, expected_index, 0)
            time.sleep(short_sleep_time)
            idx = win32gui.SendMessage(combo, CB_GETCURSEL, 0, 0)
        return idx == expected_index

    def _submit_market_trade(self, op_keyword, stock_no, amount):
        """市价委托(五档即成剩撤)：导航子面板→填单→设策略→提交→查成交回执。

        五档即成剩撤=立即成交、剩余自动撤销、无残留挂单；可能部分成交 → 回执查成交表
        (orders_filled)前后差分拿真实成交量/均价。仅连续竞价时段可下，集合竞价/涨跌停会被
        拒（回执查不到成交时返回 unknown 并提示可能非交易时段，不当成功）。"""
        strat = MARKET_STRATEGY.get(op_keyword)
        if not strat:
            return contract.fail(contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                                 f"未知方向 {op_keyword!r}")

        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        try:
            self._switch_to_normal_safely()
            # 下单前快照成交表作 before 基线（差分辨"本次新增成交" vs 历史成交；~1-2s，
            # 换回执真实性，值得——市价单可能部分成交，必须拿准实际成交量/均价）。
            pre = self.get_filled_orders()
            # ``get_filled_orders`` returns the v2 contract envelope, whose
            # successful code is the string ``"ok"`` (not legacy integer 0).
            # Checking ``code != 0`` rejected every successful read,
            # including the normal no-history case ``data=[]``.
            if not contract.is_succeed(pre):
                reason = ((pre.get("error") or {}).get("message")) or ""
                return contract.fail(
                    contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                    f"下单前无法读取成交表作回执基线（{reason}），已中止未提交——"
                    "空基线会把历史成交误算成本次成交。请稍后重试",
                    data={"submitted": False})
            before = pre.get("data")
            if not isinstance(before, list):
                return contract.fail(
                    contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                    "下单前成交表回执基线格式异常，已中止未提交",
                    data={"submitted": False})

            if not self._select_market_tree_path(op_keyword):
                return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                                     "未能导航到市价委托面板", data={"submitted": False})
            time.sleep(sleep_time)
            hwnd = self.get_right_hwnd()
            code_ctrl = self._find_input(hwnd, MARKET_CODE_ID)
            amount_ctrl = self._find_input(hwnd, MARKET_AMOUNT_ID)
            if not code_ctrl or not amount_ctrl:
                return self._pre_submit_failure(
                    "市价委托提交前未找到证券代码或数量输入框",
                    contract.CODE_READ_FAILED, CLS_READ_FAILED,
                )

            # 填证券代码 + 数量（市价面板无价格框）。
            self._set_owned_text(code_ctrl, stock_no, "market_order_code")
            time.sleep(sleep_time)
            self._set_owned_text(amount_ctrl, str(amount), "market_order_amount")
            time.sleep(short_sleep_time)

            # 委托策略 = 五档即成剩撤（卖出默认是即成剩撤=深市专有、沪市会拒 → 必须显式设）
            combo = self._find_ctrl_by_id(hwnd, MARKET_STRATEGY_COMBO_ID, cls="ComboBox", visible=True) \
                or self._find_ctrl_by_id(hwnd, MARKET_STRATEGY_COMBO_ID)
            if not combo:
                return self._pre_submit_failure(
                    "未找到委托策略下拉框", contract.CODE_READ_FAILED, CLS_READ_FAILED)
            if not self._set_market_strategy(combo, strat["key"], strat["index"]):
                logger.warning("market strategy not set to 五档即成剩撤 op=%s, abort", op_keyword)
                return contract.fail(contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                                     "委托策略未能设为五档即成剩撤，已中止（避免下错单）",
                                     data={"submitted": False})

            # 提交：优先定向投递 BM_CLICK；没有按钮才使用受前台校验的 Enter。
            submit_btn = self._find_ctrl_by_id(hwnd, MARKET_SUBMIT_BTN_ID, cls="Button", visible=True) \
                or self._find_ctrl_by_id(hwnd, MARKET_SUBMIT_BTN_ID)
            if submit_btn:
                self._post_owned_button_click(
                    submit_btn, "market_submit", before_dispatch=mark_submitted
                )
            else:
                self._send_hotkey(["enter"], "market_submit", before_dispatch=mark_submitted)
        except StaleCallAborted:
            raise
        except WindowSafetyError as e:
            if not submitted:
                return self._pre_submit_failure(str(e))
            return contract.submitted_unconfirmed(
                f"市价委托已进入提交动作，但窗口安全校验随后失败：{e}",
                data={"stock_no": str(stock_no), "submitted": True},
            )
        except Exception as e:
            logger.exception("market order failed before/while submit stock=%s", stock_no)
            if not submitted:
                return self._pre_submit_failure(
                    f"市价委托提交前发生异常：{e}", contract.CODE_READ_FAILED, CLS_READ_FAILED)
            return contract.submitted_unconfirmed(
                f"市价委托已进入提交动作，但客户端未能确认结果：{e}",
                data={"stock_no": str(stock_no), "submitted": True},
            )
        try:
            pump = self._pump_dialogs()   # 确认框/验证码/结果框：结构化处置 + 存证
        except (StaleCallAborted, WindowSafetyError) as e:
            return contract.submitted_unconfirmed(
                f"市价委托已进入提交动作，但后续弹窗核验未完成：{e}",
                data={"stock_no": str(stock_no), "submitted": True},
            )
        time.sleep(sleep_time)

        # 回执：轮询成交表拿本次新增成交（五档即成剩撤成交极快，给足 8s）
        deadline = time.time() + 8.0
        while time.time() < deadline:
            post = self.get_filled_orders()
            if contract.is_succeed(post):
                r = _match_market_fill(before, post.get("data") or [],
                                       stock_no, op_keyword, amount)
                if contract.is_succeed(r):
                    r["data"] = pump.attach_to(r["data"])
                    return r
            time.sleep(0.3)
        logger.warning("market submit unconfirmed stock=%s op=%s amount=%s dialogs=%s",
                       stock_no, op_keyword, amount, pump.dialogs)
        data = pump.attach_to({"stock_no": str(stock_no), "方向": op_keyword,
                               "requested_amount": int(amount), "filled_amount": 0,
                               "submitted": True})
        if pump.texts:
            # 有柜台原文 ⇒ 大概率是明确拒绝，走 broker_rejected 让 class 可分流。
            return contract.broker_rejected(
                "；".join(pump.texts),
                message="已提交但未在成交表确认成交，客户端有提示，请核对成交与委托",
                data=data)
        return contract.submitted_unconfirmed(
            "已提交但未在成交表确认成交（可能非连续竞价时段/涨跌停被拒/无成交）。"
            "安全动作=用同一 client_order_id 原样重发（幂等），或调 query_order 核实",
            data=data)

    @guarded
    def _do_cancel(self, entrust_no):
        try:
            return self._cancel_inner(entrust_no)
        except StaleCallAborted:
            raise
        except WindowSafetyError as e:
            logger.warning("cancel(%s) stopped by window safety: %s", entrust_no, e)
            return self._pre_submit_failure(str(e))
        except Exception as e:
            logger.exception("cancel(%s) unhandled exception", entrust_no)
            return contract.fail(contract.CODE_INTERNAL_ERROR, contract.CLS_INTERNAL_ERROR,
                                 f"cancel error: {e}")

    def _cancel_inner(self, entrust_no):
        self._switch_to_normal_safely()
        self._send_hotkey(["F3"], "cancel_panel")
        self.refresh(require_window_safety=True)
        hwnd = self.get_right_hwnd()
        if not hwnd:
            return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                                 "撤单：未找到右侧面板")
        ctrl = self._find_grid(hwnd)
        if not ctrl:
            return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                                 "撤单：F3 面板未找到委托表控件")
        data = self.read_table_text(ctrl)
        if not data:
            return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                                 "撤单：拷贝委托表未落定（可能验证码弹窗）")
        entrusts = parse_table(data)
        if not entrusts:
            return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                                 "撤单：F3 委托表解析为空")
        # F3 may show 委托编号 or 合同编号 depending on THS version/panel state.
        # _lookup_entrust_no returns 合同编号 from F1+F8; cancel must match either.
        id_col = None
        for candidate in ("委托编号", "合同编号"):
            if candidate in entrusts[0]:
                id_col = candidate
                break
        if not id_col:
            cols = list(entrusts[0].keys())
            return contract.fail(
                contract.CODE_TABLE_MISMATCH, contract.CLS_TABLE_MISMATCH,
                f"撤单：F3 表既无委托编号也无合同编号，实得列 {cols}",
                data={"got_columns": cols})
        find = None
        for i, entrust in enumerate(entrusts):
            if str(entrust[id_col]) == str(entrust_no):
                find = i
                break
        if find is None:
            return contract.fail(contract.CODE_NOT_FOUND, contract.CLS_NOT_FOUND,
                                 f"撤单：委托表中没找到指定订单 {entrust_no}（可能已成/已撤）")
        # 撤单是按行号算坐标的盲点击。两次点击分别校验前台，避免第二下在
        # 已失焦、HWND 被复用或调用作废后落到其他应用/其他控件。
        self._require_owned_window_for_input(ctrl, "cancel_click")
        left, top, right, bottom = win32gui.GetWindowRect(ctrl)
        x = 50 + left
        y = 30 + 16 * find + top
        self._click_screen(x, y, "cancel_select_row")
        time.sleep(sleep_time)
        self._click_screen(x, y, "cancel_confirm_row")
        time.sleep(sleep_time)
        # 双击委托行后可能弹「撤单确认」——结构化处置（取代两次盲 Enter），
        # 弹窗内容带回回执。
        try:
            pump = self._pump_dialogs()
        except (StaleCallAborted, WindowSafetyError) as e:
            return contract.submitted_unconfirmed(
                f"撤单双击已发出，但后续弹窗核验未完成：{e}",
                data={"entrust_no": str(entrust_no), "submitted": True},
            )
        if pump.status == "pending":
            return contract.fail(
                contract.CODE_READ_FAILED,
                contract.CLS_READ_FAILED,
                "撤单确认弹窗未找到可识别的肯定按钮，未自动确认撤单；请人工核对订单状态",
                data=pump.attach_to({"entrust_no": str(entrust_no), "submitted": False}),
            )
        return contract.ok(pump.attach_to({"entrust_no": str(entrust_no), "submitted": True}))

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

    def refresh(self, require_window_safety: bool = False):
        self._abort_if_stale("refresh")
        if require_window_safety:
            self._send_hotkey(["F5"], "refresh")
        else:
            hot_key(["F5"])
        time.sleep(refresh_sleep_time)

    def active_mian_window(self):
        if self.hwnd_main is not None:
            ctypes.windll.user32.SwitchToThisWindow(self.hwnd_main, True)
            time.sleep(sleep_time)

    def switch_to_normal(self, require_window_safety: bool = False):
        # 翻页/抓表链路的第一个动作 —— 代次检查放这里，脱缰线程在发出任何
        # 全局按键之前就退出。
        require_window_safety = require_window_safety or bool(
            getattr(self._tls, "require_window_safety", False)
        )
        self._abort_if_stale("switch_to_normal")
        tabs = self.get_left_bottom_tabs()
        if require_window_safety:
            self._require_owned_window_for_input(tabs, "switch_to_normal")
        left, top, right, bottom = win32gui.GetWindowRect(tabs)
        x = left + 10
        y = top + 5
        if require_window_safety:
            self._click_screen(x, y, "switch_to_normal")
        else:
            win32api.SetCursorPos((x, y))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(sleep_time)
        _activate_window(self.hwnd_main)

    def _empty_clipboard(self) -> bool:
        """用 API 清空剪贴板，替代 os.system("echo off | clip")（后者慢且闪 cmd 窗）。
        被别的进程锁住时退避重试。"""
        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
            except Exception:
                time.sleep(0.02)
                continue
            try:
                win32clipboard.EmptyClipboard()
            except Exception:
                pass
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
            return True
        return False

    def read_table_text(self, hwnd, timeout: float = 2.0):
        """把表格拷进剪贴板→确认本次拷贝落定→读文本→立刻清空。剪贴板仅作毫秒级中转。

        用 GetClipboardSequenceNumber 确认本次 Ctrl+C 真写入了新内容（系统级计数器，
        任何进程改动剪贴板它就 +1）：清空后取基线 seq0，拷贝落定则 seq 变化。若超时
        仍未变化 = 拷贝没落定（窗口没焦点 / 被验证码挡），返回 None 让调用方重试——
        **绝不返回上一次遗留的陈旧表格**。读完立刻清空，剪贴板不留数据。
        """
        self._abort_if_stale("read_table_text")
        user32 = ctypes.windll.user32
        self._empty_clipboard()
        seq0 = user32.GetClipboardSequenceNumber()  # 清空后取基线，之后变化=本次拷贝
        _activate_window(hwnd)
        hot_key(["ctrl", "c"])
        # Some empty grids produce no text at all. Only accept that as empty
        # after the known copy-data captcha was present and cleared; any
        # no-popup copy failure remains None and is retried/failed by callers.
        captcha_seen = bool(self.get_ocr_hwnd())
        self.input_ocr()  # 处理"检测到您正在拷贝数据"验证码（无弹窗立即返回）
        captcha_cleared = captcha_seen and not self.get_ocr_hwnd()
        deadline = time.time() + timeout
        data = None
        while time.time() < deadline:
            if user32.GetClipboardSequenceNumber() != seq0:
                data = get_clipboard_data()
                if data:
                    break
            time.sleep(0.02)
        self._empty_clipboard()  # 读完立刻清空——剪贴板只当毫秒级中转点
        if data is None and captcha_cleared:
            logger.info("grid copy produced no text after known captcha cleared; verified empty grid")
            return _VERIFIED_EMPTY_GRID
        return data

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

    def _refresh_captcha(self, captcha_static, dialog):
        """Click the captcha image to trigger image regeneration. xiadan does
        NOT auto-refresh on wrong submission — without this each retry OCRs
        the same image and gets the same wrong answer."""
        rect = win32gui.GetWindowRect(captcha_static)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        self._require_owned_window_for_input(captcha_static, "captcha_refresh")
        self._click_owned_popup(dialog, cx, cy, "captcha_refresh")
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
            # 每轮都对代次：验证码流程最长可跑十几秒，脱缰线程绝不能在这里
            # 继续点弹窗——那正是下一笔调用要处置的同一个框。
            self._abort_if_stale(f"input_ocr#{attempt}")
            captcha_static = self.get_ocr_hwnd()
            if not captcha_static:
                return
            # GA_ROOT=2 — top-level popup dialog.
            dialog = ctypes.windll.user32.GetAncestor(captcha_static, 2)
            if not dialog:
                logger.warning("ocr attempt=%d cannot resolve dialog from %s",
                               attempt, hex(captcha_static))
                return
            if not self._window_is_owned_by_bound_process(dialog):
                raise WindowSafetyError(
                    "captcha_dialog: 验证码弹窗不属于当前已绑定的 xiadan 进程，已停止输入"
                )
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
            self._require_owned_window_for_input(edit_hwnd, "captcha_edit")
            if ok_btn:
                self._require_owned_window_for_input(ok_btn, "captcha_confirm")
            # On retries (attempt > 1), force a fresh captcha image first —
            # xiadan doesn't auto-rotate on wrong submission, so without this
            # every retry OCRs the same image and gets the same wrong answer.
            if attempt > 1:
                self._refresh_captcha(captcha_static, dialog)
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
                "ocr attempt=%d edit=%s ok_btn=%s recognized=%s length=%d",
                attempt, hex(edit_hwnd),
                hex(ok_btn) if ok_btn else None, bool(code), len(code),
            )
            if not code:
                time.sleep(short_sleep_time)
                continue
            # xiadan's captcha Edit only accepts focus from real mouse input
            # (anti-bot — API SetFocus is treated as untrusted, WM_SETTEXT is
            # silently dropped). So: bring popup to foreground, click the Edit
            # center to grant focus, attach thread input, type via WM_CHAR.
            user32 = ctypes.windll.user32
            er = win32gui.GetWindowRect(edit_hwnd)
            cx = (er[0] + er[2]) // 2
            cy = (er[1] + er[3]) // 2
            self._click_owned_popup(dialog, cx, cy, "captcha_focus_edit")
            time.sleep(short_sleep_time)
            self._require_owned_window_for_input(edit_hwnd, "captcha_type")
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
                    self._require_owned_window_for_input(edit_hwnd, "captcha_type")
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
                "ocr attempt=%d wrote_length=%d read_back_length=%d match=%s",
                attempt, len(code), len(actual), actual == code,
            )
            time.sleep(short_sleep_time)
            if ok_btn:
                # PostMessage：确定按钮的 handler 若再弹模态框（如"验证码错误"），
                # SendMessage 会同步卡死本线程（同 2026-07-13 事故根因）。
                self._post_owned_button_click(ok_btn, "captcha_confirm")
            else:
                self._send_hotkey(
                    ["enter"], "captcha_confirm", expected_popup=dialog
                )
            time.sleep(sleep_time)
            if not self.get_ocr_hwnd():
                logger.info("ocr accepted attempt=%d", attempt)
                return
            logger.info("ocr rejected attempt=%d", attempt)
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

    async def list_accounts(self) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.get_account_list)

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

    async def orders_active_all(self) -> dict[str, Any]:
        """内部用（order_watch）：含终态的委托全量表。"""
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.get_active_orders_all)

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

    async def watchlist(self) -> dict[str, Any]:
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.get_watchlist)

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

    async def switch_account(self, slot: Any) -> dict[str, Any]:
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return contract.fail(contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                                 f"slot 参数无效：{slot!r}，须为 1-9 的整数")
        if not 1 <= slot <= 9:
            return contract.fail(contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                                 f"slot 超出范围：{slot}，须为 1-9 的整数")
        bound_err = self._ensure_bound()
        if bound_err:
            return bound_err
        return await asyncio.to_thread(self.do_switch_account, slot)

    @guarded
    def do_switch_account(self, slot: int):
        """向 xiadan 发送 Alt+N，切换多账户登录下的当前活跃资金账户。

        每次先读取下拉列表，以槽位对应的账户文本作为目标。若目标已经是当前
        账户，直接核验并返回资金，绝不因文本未变化把交易锁死；否则才发送
        Alt+N，并轮询到当前账户与目标账户匹配。
        """
        self._account_trading_blocked = True
        listed = self.get_account_list()
        listed_data = listed.get("data") if isinstance(listed, dict) else None
        if not contract.is_succeed(listed) or not isinstance(listed_data, dict):
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "切换前无法读取可选账户列表，未发送切换按键，已禁止买卖和撤单",
                data={
                    "slot": slot,
                    "account_verified": False,
                    "accounts": (listed_data or {}).get("accounts", []),
                    "submitted": False,
                },
            )

        accounts = listed_data.get("accounts")
        target = next(
            (item for item in accounts if isinstance(item, dict) and item.get("slot") == slot),
            None,
        ) if isinstance(accounts, list) else None
        if not target or not isinstance(target.get("text"), str):
            return contract.fail(
                contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                f"slot={slot} 不在当前可切换账户列表中，未发送切换按键",
                data={"slot": slot, "accounts": accounts or [], "submitted": False},
            )

        target_text = target["text"]
        target_identity = _account_identity(target_text)
        same_identity_count = sum(
            _account_identity(item.get("text")) == target_identity
            for item in accounts if isinstance(item, dict)
        )
        if not target_identity or same_identity_count != 1:
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "账户列表中存在无法唯一核验的目标账户，未发送切换按键，已禁止买卖和撤单",
                data={
                    "slot": slot,
                    "target_account_text": target_text,
                    "accounts": accounts,
                    "submitted": False,
                },
            )

        previous = self._read_account_selector_text()
        if not previous:
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "切换前无法读取当前账户（控件 ID 0x094C），未发送切换按键，已禁止买卖和撤单",
                data={"slot": slot, "account_verified": False, "submitted": False},
            )

        if _account_identity(previous) == target_identity:
            current = previous
            already_active = True
        else:
            already_active = False
            self._send_hotkey(["alt", str(slot)], "switch_account")

            # 切换会触发账户列表和资金/持仓面板重载。账户控件可能先保留旧文本，
            # 因此轮询至与列表中目标槽位匹配；不使用固定 HWND，确保同花顺重启后仍可定位。
            current = None
            deadline = time.monotonic() + ACCOUNT_VERIFY_TIMEOUT_SECS
            while time.monotonic() < deadline:
                self._abort_if_stale("switch_account_verify")
                current = self._read_account_selector_text()
                if current and _account_identity(current) == target_identity:
                    break
                time.sleep(sleep_time)

        if not current or _account_identity(current) != target_identity:
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "Alt+%s 已发送，但当前账户未匹配目标账户 %s，已禁止买卖和撤单；"
                "请确认目标账户后重试" % (slot, target_text),
                data={
                    "slot": slot,
                    "account_verified": False,
                    "target_account_text": target_text,
                    "previous_account_text": previous,
                    "account_text": current,
                    "submitted": False,
                },
            )

        balance = self.get_balance()
        if not contract.is_succeed(balance):
            return contract.fail(
                contract.CODE_READ_FAILED, CLS_READ_FAILED,
                "已核验当前账户为 %s，但资金信息读取失败，已禁止买卖和撤单；请稍后重试"
                % current,
                data={
                    "slot": slot,
                    "target_account_text": target_text,
                    "account_verified": False,
                    "account_text": current,
                    "balance": None,
                    "balance_error": balance.get("error") if isinstance(balance, dict) else None,
                    "submitted": False,
                },
            )

        self._last_account_text = current
        self._account_trading_blocked = False
        message = f"当前已是：{current}" if already_active else f"已切换到：{current}"
        return contract.ok({
            "slot": slot,
            "target_account_text": target_text,
            "account_verified": True,
            "account_text": current,
            "already_active": already_active,
            "balance": balance.get("data"),
            "msg": message,
        })
