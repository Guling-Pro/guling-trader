"""截图 + OCR 自选股：DPI 感知截图 + 直接 OCR 抽代码（新版专有）。只读，临时探针。

修正：PrintWindow 前切 Per-Monitor-V2 DPI（复用 capture_window 的做法），让 GetWindowRect
返回物理像素，2x 屏（Parallels/Retina）不再截半张/尺寸错。OCR 直接用（Tesseract 验证码
本就在用），全表识别 + 正则抽 6 位代码。

用法（项目根，任意 shell，新版 xiadan、自选股停「自选」tab）：
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

# 复用 trader 的 Tesseract 检测（和验证码识别用同一个），否则 setup 传空 → pytesseract
# 走 PATH 找不到。
try:
    from trader.installer.tesseract import detect_tesseract
    _tess = detect_tesseract() or ""
except Exception as _e:
    _tess = ""
print("tesseract_cmd =", _tess or "<PATH>")
W.setup("网上股票交易系统5.0", _tess, "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", hex(b.hwnd_main) if b.hwnd_main else None)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定，先确认新版 xiadan 已登录、自选股停「自选」tab")

b.switch_to_normal()
print("导航到「自选股」:", b._select_tree_node_by_text("自选股"))
time.sleep(1.2)


def capture_dpi_aware(hwnd, path):
    """DPI 感知 PrintWindow(PW_RENDERFULLCONTENT)：切 Per-Monitor-V2 让 rect 与渲染都按
    物理像素，2x 屏不再截半张；PrintWindow 能截到 Chromium/CEF（BitBlt 会黑屏）。"""
    user32 = ctypes.windll.user32
    old_ctx = None
    if hasattr(user32, "SetThreadDpiAwarenessContext"):
        try:
            old_ctx = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PMv2
        except Exception:
            old_ctx = None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        hdc = win32gui.GetWindowDC(hwnd)
        dc = win32ui.CreateDCFromHandle(hdc)
        cdc = dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(dc, w, h)
        cdc.SelectObject(bmp)
        ok = user32.PrintWindow(hwnd, cdc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bmp.GetHandle())
        cdc.DeleteDC(); dc.DeleteDC(); win32gui.ReleaseDC(hwnd, hdc)
        img.save(path)
        return ok, w, h
    finally:
        if old_ctx is not None:
            try:
                user32.SetThreadDpiAwarenessContext(old_ctx)
            except Exception:
                pass


# 找最大的可见 CEF 渲染窗口
cands = []


def wk(hh, _):
    try:
        if win32gui.GetClassName(hh) in (
            "Chrome_RenderWidgetHostHWND", "CefBrowserWindow", "Chrome_WidgetWin_0"
        ) and win32gui.IsWindowVisible(hh):
            r = win32gui.GetWindowRect(hh)
            cands.append((hh, (r[2] - r[0]) * (r[3] - r[1])))
    except Exception:
        pass
    return True


win32gui.EnumChildWindows(b.hwnd_main, wk, None)
cands.sort(key=lambda x: -x[1])
target = cands[0][0] if cands else b.hwnd_main

out = os.path.join(os.getcwd(), "watchlist_shot.png")
ok, w, h = capture_dpi_aware(target, out)
print(f"截取 hwnd=0x{target:X}  ok={ok}  size={w}x{h}  → {out}")

# 直接 OCR（Tesseract 本就在用）
import pytesseract
img = Image.open(out)
txt = pytesseract.image_to_string(img, lang="chi_sim+eng")
print("\n=== OCR 全文 ===")
print(txt)

codes = re.findall(r"\b\d{6}\b", txt)
print("\n=== 正则抽出的 6 位代码 ===")
print(codes, f"（{len(codes)} 个）")
print(f"\n>>> 看 {out} 尺寸对了没（整表都在、不是半张）；上面代码抽全了没。")
