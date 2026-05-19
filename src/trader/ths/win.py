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
import logging
import os
import platform
import re
import sys
import time
from typing import Any, Optional

import pytesseract
from PIL import Image, ImageFilter, ImageGrab

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
window_title: str = ""


def setup(window_title_value: str, tesseract_cmd: str) -> None:
    """Apply runtime configuration. Call once at server startup before using ThsAuto."""
    global window_title
    window_title = window_title_value
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    logger.info(
        "thsauto setup: window_title=%r tesseract_cmd=%r",
        window_title,
        tesseract_cmd or "<from PATH>",
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


def ocr_rect(bbox):
    img = ImageGrab.grab(bbox=bbox)
    img.save("ret.png")
    import cv2

    image = cv2.imread("ret.png")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    scale_factor = 2
    resized_img = cv2.resize(gray, None, fx=scale_factor, fy=scale_factor, interpolation=cv2.INTER_CUBIC)
    pil_img = Image.fromarray(resized_img)
    smoothed_img = pil_img.filter(ImageFilter.GaussianBlur(radius=1))
    sharpened_img = smoothed_img.filter(ImageFilter.SHARPEN)
    import numpy as np

    processed_img = np.array(sharpened_img)
    cv2.imwrite("ret1.png", processed_img)
    text = pytesseract.image_to_string(Image.fromarray(processed_img), lang=r"chi_sim+eng")
    return text.strip()


def get_clipboard_data():
    win32clipboard.OpenClipboard()
    try:
        data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    return data


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


class WinThsBackend:
    def __init__(self):
        self.hwnd_main = None

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
            ctrl = win32gui.GetDlgItem(hwnd, cid)
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
            ctrl = win32gui.GetDlgItem(hwnd, 0x417)
            self.copy_table(ctrl)
            data = get_clipboard_data()
            if data:
                return {"code": 0, "status": "succeed", "data": parse_table(data)}
            time.sleep(sleep_time)
        return {"code": 1, "status": "failed"}

    def get_gupiao(self):
        for retry in range(retry_time):
            self.switch_to_normal()
            hot_key(["F4"])
            self.refresh()
            hwnd = self.get_right_hwnd()
            ctrl = win32gui.GetDlgItem(hwnd, 0x417)
            self.copy_table(ctrl)
            data = get_clipboard_data()
            if data:
                return {"code": 0, "status": "succeed", "data": parse_table(data)}
            time.sleep(sleep_time)
        return {"code": 1, "status": "failed"}

    def get_active_orders(self):
        for retry in range(retry_time):
            self.switch_to_normal()
            _activate_window(self.hwnd_main)
            hot_key(["F1"])
            hot_key(["F8"])
            self.refresh()
            hwnd = self.get_right_hwnd()
            ctrl = win32gui.GetDlgItem(hwnd, 0x417)
            self.copy_table(ctrl)
            data = get_clipboard_data()
            if data:
                return {"code": 0, "status": "succeed", "data": parse_table(data)}
            time.sleep(sleep_time)
        return {"code": 1, "status": "failed"}

    def get_filled_orders(self):
        self.switch_to_normal()
        _activate_window(self.hwnd_main)
        hot_key(["F2"])
        hot_key(["F7"])
        self.refresh()
        hwnd = self.get_right_hwnd()
        ctrl = win32gui.GetDlgItem(hwnd, 0x417)
        self.copy_table(ctrl)
        data = None
        retry = 0
        while not data and retry < retry_time:
            retry += 1
            time.sleep(sleep_time)
            data = get_clipboard_data()
        if data:
            return {"code": 0, "status": "succeed", "data": parse_table(data)}
        return {"code": 1, "status": "failed"}

    def _lookup_entrust_no(self, stock_no, op_keyword, amount, price, timeout=4.0):
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
        # Three Enters: form-submit + confirm dialog + dismiss result popup.
        # Kept verbatim from upstream — empirical across xiadan versions.
        hot_key(["enter"])
        hot_key(["enter"])
        hot_key(["enter"])
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
            self.capture_window(captcha_static, "ocr.png")
            try:
                raw_image = Image.open("ocr.png")
                image = self._preprocess_captcha(raw_image)
                image.save("ocr_proc.png")
            except Exception:
                logger.exception("ocr attempt=%d preprocess failed", attempt)
                image = Image.open("ocr.png")
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
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)

        win32gui.DeleteObject(bmp.GetHandle())
        dc.DeleteDC()
        cdc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)

        img.save(file_name)

    # ------------------------------------------------------------------
    # Async surface for PH-061 dispatcher.
    #
    # The 7 whitelist methods called by trader.dispatcher.handle_call.
    # Sync pywin32 work is wrapped in asyncio.to_thread so the trader
    # event loop (ws_client, tray) stays responsive.
    # ------------------------------------------------------------------

    async def balance(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.get_balance)

    async def position(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.get_position)

    async def orders_active(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.get_active_orders)

    async def orders_filled(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.get_filled_orders)

    async def buy(
        self,
        stock_no: str,
        amount: int,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._do_buy, stock_no, amount, price if price is not None else 0
        )

    async def sell(
        self,
        stock_no: str,
        amount: int,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._do_sell, stock_no, amount, price if price is not None else 0
        )

    async def cancel(self, entrust_no: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._do_cancel, entrust_no)
