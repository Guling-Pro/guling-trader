"""截图 + OCR 自选股 可行性验证（新版专有）。只读，临时探针。

自选股是 CEF 渲染的网页表格，原生控件读不到、无 CDP 口、文件过期。改用截图+OCR：
截取自选股渲染窗口（CEF 需 PrintWindow(PW_RENDERFULLCONTENT) 才不黑屏），存 PNG 并跑
Tesseract，评估截图清晰度与代码识别准确度。

前置：xiadan 新版、自选股停在【自选】tab；需装 Tesseract（trader 验证码用的那个）。
用法（项目根，任意 shell，停掉 python -m trader）：
    python tools\\probe_watchlist.py
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32gui
from PIL import Image

from trader.ths import win as W

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", hex(b.hwnd_main) if b.hwnd_main else None)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定，先确认新版 xiadan 已登录、自选股停在【自选】tab")

b.switch_to_normal()
print("导航到「自选股」:", b._select_tree_node_by_text("自选股"))
time.sleep(1.2)


def capture_printwindow(hwnd, path):
    """PrintWindow(PW_RENDERFULLCONTENT=2) 截取窗口，能截到 Chromium/CEF 内容（BitBlt 常黑屏）。"""
    import win32ui
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    hdc = win32gui.GetWindowDC(hwnd)
    dc = win32ui.CreateDCFromHandle(hdc)
    cdc = dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(dc, w, h)
    cdc.SelectObject(bmp)
    ok = ctypes.windll.user32.PrintWindow(hwnd, cdc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    cdc.DeleteDC(); dc.DeleteDC(); win32gui.ReleaseDC(hwnd, hdc)
    img.save(path)
    return (ok, w, h)


# 找可见的 CEF 渲染窗口（Chrome_RenderWidgetHostHWND / CefBrowserWindow），取面积最大的
cands = []


def wk(hh, _):
    try:
        cls = win32gui.GetClassName(hh)
        if cls in ("Chrome_RenderWidgetHostHWND", "CefBrowserWindow", "Chrome_WidgetWin_0") \
                and win32gui.IsWindowVisible(hh):
            r = win32gui.GetWindowRect(hh)
            cands.append((hh, cls, (r[2] - r[0]) * (r[3] - r[1]), r))
    except Exception:
        pass
    return True


win32gui.EnumChildWindows(b.hwnd_main, wk, None)
cands.sort(key=lambda x: -x[2])
print("\nCEF 渲染窗口候选（按面积）:")
for hh, cls, area, r in cands[:5]:
    print(f"  hwnd=0x{hh:X} {cls} {r[2]-r[0]}x{r[3]-r[1]} rect={r}")

out = os.path.join(os.getcwd(), "watchlist_shot.png")
target = cands[0][0] if cands else b.hwnd_main
print(f"\n截取 hwnd=0x{target:X} → {out}")
res = capture_printwindow(target, out)
print("PrintWindow 结果(ok,w,h):", res, "（ok=1 且非黑屏才有效）")

# OCR
print("\n=== Tesseract OCR（前 1500 字符）===")
try:
    import pytesseract
    img = Image.open(out)
    txt = pytesseract.image_to_string(img, lang="chi_sim+eng")
    print(txt[:1500])
except Exception as e:
    print("OCR 失败:", e, "\n（若是 tesseract 未装：装 Tesseract 后重跑；PNG 已存，可先肉眼看清不清晰）")

print(f"\n>>> 打开 {out} 肉眼看：自选股表格截清楚了吗（不是黑屏/半张）？")
print("    并贴回上面 OCR 文本：代码/名称认得准不准。据此定截图+OCR 方案是否可行。")
