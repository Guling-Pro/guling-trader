"""截图 + OCR 抓自选股【代码列】+ 滚屏翻页凑全（新版专有）。只读，临时探针。

要点：
- DPI 感知 PrintWindow 截 CEF（2x 屏也不截半张）；
- OCR 用坐标框：只取【最左那一簇】6 位数字 = 代码列，天然排除右侧数字列（主力净额/
  总金额等）产生的假 6 位数；
- 向 CEF 发滚轮翻页，去重累计，直到连续两屏无新增。

前置：新版 xiadan、自选股停「自选」tab、装了 Tesseract（验证码那个）。
用法（项目根，任意 shell，停掉 python -m trader）：
    python tools\\probe_watchlist.py
"""
import ctypes
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32con
import win32gui
import win32ui
from PIL import Image

from trader.ths import win as W

try:
    from trader.installer.tesseract import detect_tesseract
    _tess = detect_tesseract() or ""
except Exception:
    _tess = ""
print("tesseract_cmd =", _tess or "<PATH>")
W.setup("网上股票交易系统5.0", _tess, "")

import pytesseract
from pytesseract import Output

b = W.WinThsBackend()
b.bind_client()
if not b.hwnd_main:
    raise SystemExit("!! 未绑定，先确认新版 xiadan 已登录、自选股停「自选」tab")

b.switch_to_normal()
print("导航到「自选股」:", b._select_tree_node_by_text("自选股"))
time.sleep(1.0)


def capture(hwnd):
    """DPI 感知 PrintWindow(PW_RENDERFULLCONTENT)，返回 PIL.Image。"""
    user32 = ctypes.windll.user32
    old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)) if hasattr(
        user32, "SetThreadDpiAwarenessContext") else None
    try:
        l, t, r, btm = win32gui.GetWindowRect(hwnd)
        w, h = r - l, btm - t
        hdc = win32gui.GetWindowDC(hwnd)
        dc = win32ui.CreateDCFromHandle(hdc)
        cdc = dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(dc, w, h)
        cdc.SelectObject(bmp)
        user32.PrintWindow(hwnd, cdc.GetSafeHdc(), 2)
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bmp.GetHandle())
        cdc.DeleteDC(); dc.DeleteDC(); win32gui.ReleaseDC(hwnd, hdc)
        return img
    finally:
        if old is not None:
            user32.SetThreadDpiAwarenessContext(old)


def codes_in(img):
    """用 OCR 坐标框取【最左一簇】6 位数字 = 代码列。"""
    d = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=Output.DICT)
    got = [(t.strip(), d["left"][i]) for i, t in enumerate(d["text"])
           if re.fullmatch(r"\d{6}", (t or "").strip())]
    if not got:
        return []
    min_x = min(x for _, x in got)
    return [t for t, x in got if x <= min_x + 120]  # 容差 120 物理像素


def scroll_down(hwnd, notches=3):
    r = win32gui.GetWindowRect(hwnd)
    x, y = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
    delta = (-120 * notches) & 0xFFFF
    win32gui.SendMessage(hwnd, win32con.WM_MOUSEWHEEL, delta << 16, (y << 16) | (x & 0xFFFF))


# 找 CEF 渲染窗口
cands = []
win32gui.EnumChildWindows(b.hwnd_main, lambda hh, _: cands.append(hh) or True
                          if win32gui.GetClassName(hh) in (
                              "Chrome_RenderWidgetHostHWND", "CefBrowserWindow")
                          and win32gui.IsWindowVisible(hh) else True, None)
target = cands[0] if cands else b.hwnd_main

# 滚到底为止：不预设总数（不知道用户有多少只），靠"连续两屏无新增=到底了"终止。
# range 上限只是防死循环的安全阀，设得足够大以免长列表被截断。
seen, order, dry = set(), [], 0
for rnd in range(200):
    cs = codes_in(capture(target))
    new = [c for c in cs if c not in seen]
    for c in cs:
        if c not in seen:
            seen.add(c); order.append(c)
    print(f"round {rnd}: 本屏 {len(cs)} 只，新增 {len(new)}，累计 {len(order)}")
    dry = dry + 1 if not new else 0
    if dry >= 2:  # 连续两屏无新增 → 已滚到底
        print("（连续两屏无新增，判定已到底）")
        break
    scroll_down(target)
    time.sleep(0.5)

print("\n=== 自选股代码（去重，按出现顺序）===")
print(order)
print(f"共 {len(order)} 只")
print("\n>>> 对照你自选股实际数量，看凑全没、有没有串进非代码。")
