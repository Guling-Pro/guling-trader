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
import logging
import os
import platform
import re
import sys
import tempfile
import threading
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

from .const import (
    BALANCE_CONTROL_ID_GROUP,
    MARKET_AMOUNT_ID,
    MARKET_CODE_ID,
    MARKET_STRATEGY,
    MARKET_STRATEGY_COMBO_ID,
    MARKET_SUBMIT_BTN_ID,
    MARKET_TREE_PARENT,
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


_PHANTOM_VALUES = frozenset({"", "0", "0.0", "0.00", "0.000", "-", "--"})


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
        # order_watch 与 RPC 共用：串行化对 THS 单窗口的访问，避免并发拷表。
        self.win_lock = asyncio.Lock()
        # agent 经 RPC 下单成功后登记的合同编号，供 order_watch 标记事件来源。
        self.agent_entrust_nos: set[str] = set()
        # 内存态：查询结果的 last-known 存储（剪贴板仅作毫秒级中转）。
        self.state = ThsState()
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
        from .dialogs import DialogSentry
        return DialogSentry(self).pump()

    def dialog_cleanup(self):
        """degraded 自愈入口：清掉残留弹窗并留存证（dispatcher 在超时后的
        下一次调用前执行）。返回 PumpResult，内容进日志。"""
        self._abort_if_stale("dialog_cleanup")
        from .dialogs import DialogSentry
        result = DialogSentry(self).cleanup()
        if result.dialogs:
            logger.warning("dialog_cleanup 清掉残留弹窗：%s", result.dialogs)
        return result

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
        return contract.fail(contract.CODE_NOT_BOUND, CLS_NOT_BOUND,
                             "未检测到 xiadan 窗口（请确保同花顺已打开并登录）")

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
        self.switch_to_normal()
        hot_key(["F3"])
        self.refresh()
        right = self.get_right_hwnd()
        try:
            btn = self._find_ctrl_by_id(right, btn_id, cls="Button", visible=True) \
                or self._find_ctrl_by_id(right, btn_id, cls="Button")
        except Exception as e:
            return {"code": 1, "status": "failed",
                    "msg": f"GetDlgItem 0x{btn_id:04X}: {e}"}
        if not btn:
            return {"code": 1, "status": "failed",
                    "msg": f"button 0x{btn_id:04X} not present in F3 panel"}
        # BM_CLICK fires the button's WM_COMMAND. Cross-process safe.
        win32api.PostMessage(btn, win32con.BM_CLICK, 0, 0)
        time.sleep(sleep_time)
        # "您确定要撤销..." 确认框 / 验证码：结构化处置 + 存证（取代盲 Enter）。
        pump = self._pump_dialogs()
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
        got_columns: list[str] = []
        reason = ""
        for attempt in range(1, self._GRID_ATTEMPTS + 1):
            goto()
            hwnd = self.get_right_hwnd()
            ctrl = self._find_grid(hwnd)
            data = self.read_table_text(ctrl) if ctrl else None
            if data:
                # 表头取自原始文本而非解析结果：空表（今天无挂单/无成交）是合法
                # 结果，它照样有表头，必须能通过校验并以 data=[] 正常返回。
                got_columns = table_columns(data)
                reason = check_table(kind, got_columns) or ""
                parsed = parse_table(data)
                if not reason:
                    rows = normalize(parsed) if normalize else parsed
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

        def normalize(rows):
            out = []
            for raw in rows:
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

    def _select_tree_child(self, parent_text: str, child_text: str) -> bool:
        """先按 parent_text 定位父节点，再在其【直接子节点】里整串精确匹配 child_text 选中。

        用于市价委托 └ 买入/卖出——子节点文字'买入'与顶层'买入[F1]'前缀相同，深度优先的
        _select_tree_node_by_text 会先撞顶层，故必须限定在父节点子树内、且整串精确匹配。

        跨进程 TreeView 读写/位数处理/DPI 点击与 _select_tree_node_by_text 同构；那套原语有
        交割单/自选股导航依赖，为免在无法回归的环境重构破坏，这里独立实现，真机稳定后可再合并。
        """
        tree = self.get_tree_hwnd()
        if not tree:
            logger.warning("market: tree hwnd not found")
            return False
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

            parent_norm = _norm(parent_text)
            child_norm = _norm(child_text)

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

            # 1) 深度优先找父节点（parent_text 在菜单里唯一，子串匹配足够）
            def find_parent(hitem: int):
                while hitem:
                    if parent_norm in _norm(read_text(hitem)):
                        return hitem
                    child = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_CHILD, hitem)
                    if child:
                        found = find_parent(child)
                        if found:
                            return found
                    hitem = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_NEXT, hitem)
                return 0

            parent = find_parent(win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_ROOT, 0))
            if not parent:
                logger.warning("market: parent tree node %r not found", parent_text)
                return False

            # 2) 只在父节点的【直接子节点】里整串精确匹配 child_text（免撞顶层"买入[F1]"）
            node = 0
            seen_children: list[str] = []
            hchild = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_CHILD, parent)
            while hchild:
                ctext = _norm(read_text(hchild))
                seen_children.append(ctext)
                if ctext == child_norm:
                    node = hchild
                    break
                hchild = win32gui.SendMessage(tree, TVM_GETNEXTITEM, TVGN_NEXT, hchild)
            if not node:
                logger.warning("market: child %r under %r not found; children=%r",
                               child_text, parent_text, seen_children)
                return False

            # 3) 选中 + 真实鼠标点击（触发右侧面板切换；同 _select_tree_node_by_text）
            win32gui.SendMessage(tree, TVM_SELECTITEM, TVGN_CARET, node)
            k32.WriteProcessMemory(int(h_proc), remote_text,
                                   ctypes.byref(ctypes.c_ssize_t(node)),
                                   ctypes.sizeof(ctypes.c_ssize_t), None)
            got = win32gui.SendMessage(tree, TVM_GETITEMRECT, 0, remote_text)
            if not got:
                logger.info("market: selected (no rect, 程序化) child %r/%r",
                            parent_text, child_text)
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
            logger.info("market: clicked tree child %r/%r at client(%d,%d)",
                        parent_text, child_text, cx, cy)
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
            if contract.is_succeed(result):
                # 契约 v2：行已规范化（数值为 number、方向/状态为枚举、id 键为 entrust_no）。
                rows = result.get("data") or []
                last_seen_rows = len(rows)
                candidates = []
                for r in rows:
                    if (r.get("证券代码") or "") != str(stock_no):
                        continue
                    if op_keyword not in (r.get("方向") or ""):
                        continue
                    if r.get("委托数量") != int(target_amount):
                        continue
                    if target_price is not None and r.get("委托价") != float(target_price):
                        continue
                    candidates.append(r)
                if candidates:
                    candidates.sort(
                        key=lambda r: int(r.get("entrust_no") or 0),
                        reverse=True,
                    )
                    eno = (candidates[0].get("entrust_no") or "").strip()
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
        ctrl = self._find_input(hwnd, 0x408)
        set_text(ctrl, stock_no)
        time.sleep(sleep_time)
        price_str = None
        if price is not None:
            price_str = "%.3f" % price
            ctrl = self._find_input(hwnd, 0x409)
            set_text(ctrl, price_str, True)
            time.sleep(short_sleep_time)
        ctrl = self._find_input(hwnd, 0x40A)
        set_text(ctrl, str(amount))
        time.sleep(sleep_time)
        # Submit form（Enter 提交表单本身）→ 之后可能出现的确认框/验证码/结果框
        # 交给 DialogSentry 结构化处置（发现弹窗→点肯定按钮→存证；含 Edit 的
        # 验证码框走 input_ocr）。取代旧的三连盲 Enter：不再依赖焦点与时序，
        # 弹窗标题/全文/所点按钮全部带回回执，绝不静默。
        # 提交前最后一次对代次：填单到这里已过去约 1s，若本笔已被超时作废，
        # 绝不能在下一笔正在操作同一窗口时又敲一次提交。
        self._abort_if_stale("submit_trade")
        hot_key(["enter"])   # submit form → 可能弹「委托确认」
        pump = self._pump_dialogs()
        time.sleep(sleep_time)
        entrust_no = pump.entrust_no or self._lookup_entrust_no(
            stock_no, op_keyword, amount, price)
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
            # 回查无此单 + 有弹窗文本 ⇒ 大概率被拒/废单：原文原样带回 broker_msg，
            # class 由关键词表尽力映射（认不出即 unknown = 不可自动重试）。
            return contract.broker_rejected(
                "；".join(pump.texts),
                message="委托未进入委托列表，客户端有提示",
                data=pump.attach_to({"stock_no": str(stock_no), "submitted": True}))
        return contract.submitted_unconfirmed(
            "已提交但未能在委托表中匹配到对应订单，真相不可知。"
            "安全动作=用同一 client_order_id 原样重发（幂等），或调 query_order 核实",
            data=pump.attach_to({"stock_no": str(stock_no), "submitted": True}))

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

        self.switch_to_normal()
        _activate_window(self.hwnd_main)
        # 下单前快照成交表作 before 基线（差分辨"本次新增成交" vs 历史成交；~1-2s，
        # 换回执真实性，值得——市价单可能部分成交，必须拿准实际成交量/均价）。
        pre = self.get_filled_orders()
        if pre.get("code") != 0:
            # 基线拿不到就**不下单**。空基线会把当日同股同向的历史成交算成本次成交
            # （回执差分认「after 里 before 没有的行」），直接污染真钱 sizing 的输入；
            # 而市价单发出去就没法回收。宁可不下单让调用方重试，也不带着空基线提交。
            reason = ((pre.get("error") or {}).get("message")) or ""
            return contract.fail(
                contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                f"下单前无法读取成交表作回执基线（{reason}），已中止未提交——"
                "空基线会把历史成交误算成本次成交。请稍后重试",
                data={"submitted": False})
        before = pre.get("data") or []

        if not self._select_tree_child(MARKET_TREE_PARENT, op_keyword):
            return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                                 "未能导航到市价委托面板", data={"submitted": False})
        time.sleep(sleep_time)
        hwnd = self.get_right_hwnd()

        # 填 证券代码 + 数量（市价面板无价格框）
        set_text(self._find_input(hwnd, MARKET_CODE_ID), stock_no)
        time.sleep(sleep_time)
        set_text(self._find_input(hwnd, MARKET_AMOUNT_ID), str(amount))
        time.sleep(short_sleep_time)

        # 委托策略 = 五档即成剩撤（卖出默认是即成剩撤=深市专有、沪市会拒 → 必须显式设）
        combo = self._find_ctrl_by_id(hwnd, MARKET_STRATEGY_COMBO_ID, cls="ComboBox", visible=True) \
            or self._find_ctrl_by_id(hwnd, MARKET_STRATEGY_COMBO_ID)
        if not combo:
            return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                                 "未找到委托策略下拉框", data={"submitted": False})
        if not self._set_market_strategy(combo, strat["key"], strat["index"]):
            logger.warning("market strategy not set to 五档即成剩撤 op=%s, abort", op_keyword)
            return contract.fail(contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                                 "委托策略未能设为五档即成剩撤，已中止（避免下错单）",
                                 data={"submitted": False})

        # 提交：点提交按钮（焦点无关，避开 combo 焦点吞 Enter）。
        # 必须 PostMessage：SendMessage 是同步跨进程调用，按钮 handler 弹出模态
        # 「委托确认」框时不返回 → 线程死锁（2026-07-13 事故根因），后续弹窗
        # 处理代码永远执行不到。
        self._abort_if_stale("submit_market_trade")
        submit_btn = self._find_ctrl_by_id(hwnd, MARKET_SUBMIT_BTN_ID, cls="Button", visible=True) \
            or self._find_ctrl_by_id(hwnd, MARKET_SUBMIT_BTN_ID)
        if submit_btn:
            win32api.PostMessage(submit_btn, win32con.BM_CLICK, 0, 0)
        else:
            hot_key(["enter"])
        pump = self._pump_dialogs()   # 确认框/验证码/结果框：结构化处置 + 存证
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
        except Exception as e:
            logger.exception("cancel(%s) unhandled exception", entrust_no)
            return contract.fail(contract.CODE_INTERNAL_ERROR, contract.CLS_INTERNAL_ERROR,
                                 f"cancel error: {e}")

    def _cancel_inner(self, entrust_no):
        self.switch_to_normal()
        hot_key(["F3"])
        self.refresh()
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
        # 撤单是按行号算坐标的盲点击：本笔若已被作废，页面早被下一笔切走，
        # 这两下点击会落到未知控件上 —— 提交类动作前必须对代次。
        self._abort_if_stale("cancel_click")
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
        # 双击委托行后可能弹「撤单确认」——结构化处置（取代两次盲 Enter），
        # 弹窗内容带回回执。
        pump = self._pump_dialogs()
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

    def refresh(self):
        self._abort_if_stale("refresh")
        hot_key(["F5"])
        time.sleep(refresh_sleep_time)

    def active_mian_window(self):
        if self.hwnd_main is not None:
            ctypes.windll.user32.SwitchToThisWindow(self.hwnd_main, True)
            time.sleep(sleep_time)

    def switch_to_normal(self):
        # 翻页/抓表链路的第一个动作 —— 代次检查放这里，脱缰线程在发出任何
        # 全局按键之前就退出。
        self._abort_if_stale("switch_to_normal")
        tabs = self.get_left_bottom_tabs()
        left, top, right, bottom = win32gui.GetWindowRect(tabs)
        x = left + 10
        y = top + 5
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
        self.input_ocr()  # 处理"检测到您正在拷贝数据"验证码（无弹窗立即返回）
        deadline = time.time() + timeout
        data = None
        while time.time() < deadline:
            if user32.GetClipboardSequenceNumber() != seq0:
                data = get_clipboard_data()
                if data:
                    break
            time.sleep(0.02)
        self._empty_clipboard()  # 读完立刻清空——剪贴板只当毫秒级中转点
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
                # PostMessage：确定按钮的 handler 若再弹模态框（如"验证码错误"），
                # SendMessage 会同步卡死本线程（同 2026-07-13 事故根因）。
                win32api.PostMessage(ok_btn, win32con.BM_CLICK, 0, 0)
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

        盲切：新版 xiadan 的账户下拉框给每个已登录账户注册了 Alt+1..Alt+9
        加速键（与下拉列表顺序一致）。这里只负责把窗口拉到前台并发按键，
        不核验切换结果——受控端对账户身份保持无感知，切换后由调用方用
        balance/position 做指纹核对再继续操作。"""
        _activate_window(self.hwnd_main)
        hot_key(["alt", str(slot)])
        # 切换会触发资金/持仓面板重载，稍等再放行后续操作。
        time.sleep(sleep_time * 2)
        return contract.ok({
            "slot": slot,
            "msg": (
                f"已向同花顺窗口发送 Alt+{slot}（盲切，未核验结果）。"
                "后续所有查询/下单都作用于切换后的当前账户，"
                "请先用 balance/position 核对账户身份再继续。"
            ),
        })
