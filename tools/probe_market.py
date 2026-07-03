"""dump 当前右侧面板控件（只读，不导航、不下单）。临时探针。

用途：你【手动点开「市价委托 └ 买入」面板】后运行，dump 该面板的原生控件 ID + 委托策略
下拉类型，供市价委托计划 Task 4（真机控件测绘）落地。确认原生(非CEF)、拿到：
- 证券代码 Edit id、买入数量 Edit id、买入 Button id
- 委托策略 ComboBox 的 id / 类名 / 当前选中项 + 各项文字与索引

用法（项目根，任意 shell）：
    1) 在 xiadan 左树点开「市价委托」→「买入」，确认右侧是「市价买入」面板；
    2) python tools\\probe_market.py
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32con
import win32gui

from trader.ths import win as W

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", hex(b.hwnd_main) if b.hwnd_main else None)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定，先确认新版 xiadan 已登录")

# 不导航——dump 你手动打开的当前右侧面板
hwnd = b.get_right_hwnd()
print("right_hwnd =", hex(hwnd & 0xFFFFFFFF) if hwnd else None)

# 1) 整窗有没有可见 CEF（有=网页渲染）
cef = []


def cwk(h, _):
    try:
        if win32gui.GetClassName(h) in (
            "Chrome_RenderWidgetHostHWND", "CefBrowserWindow", "Chrome_WidgetWin_0"
        ) and win32gui.IsWindowVisible(h):
            cef.append(win32gui.GetClassName(h))
    except Exception:
        pass
    return True


win32gui.EnumChildWindows(b.hwnd_main, cwk, None)
print("可见 CEF 渲染窗口:", cef or "无 → 原生面板 ✓")

# 2) dump 面板控件
rows = []


def wk(h, _):
    try:
        rows.append((
            h,
            win32gui.GetDlgCtrlID(h) & 0xFFFF,
            win32gui.GetClassName(h),
            (win32gui.GetWindowText(h) or "").strip()[:24],
            int(bool(win32gui.IsWindowVisible(h))),
        ))
    except Exception:
        pass
    return True


if hwnd:
    win32gui.EnumChildWindows(hwnd, wk, None)

print("\n=== 面板控件（Edit / ComboBox / Button / 带文字）===")
for h, cid, cls, txt, vis in rows:
    if cls in ("Edit", "Button") or "ComboBox" in cls or txt:
        print(f"  hwnd=0x{h:06X} id=0x{cid:04X}  {cls:<16} vis={vis}  {txt!r}")

# 3) 每个 ComboBox 的项数 + 当前选中索引（只读整数，跨进程安全；不读文字——CB_GETLBTEXT
#    跨进程会往目标进程写内存、有崩溃风险。文字以你的截图为准，或真实现里用 VirtualAllocEx 读）
CB_GETCOUNT, CB_GETCURSEL = 0x0146, 0x0147
combos = [r for r in rows if "ComboBox" in r[2]]
print(f"\n=== ComboBox（委托策略下拉，{len(combos)} 个）===")
for h, cid, cls, txt, vis in combos:
    cnt = win32gui.SendMessage(h, CB_GETCOUNT, 0, 0)
    cur = win32gui.SendMessage(h, CB_GETCURSEL, 0, 0)
    print(f"  id=0x{cid:04X}  {cls}  vis={vis}  共{cnt}项  当前选中index={cur}  当前文字={txt!r}")

print("""
=== 我要的信息（在【市价买入】和【市价卖出】各跑一次，贴回）===
1. "可见 CEF" 是不是「无」（确认原生）；
2. 证券代码 Edit 的 id、买入/卖出数量 Edit 的 id、买入/卖出 Button 的 id；
3. 委托策略 ComboBox 的 id/类名（是不是标准 'ComboBox'）+ 项数 + 当前 index。
   （下拉各项文字你已截图，买入面板也麻烦展开截一张。）""")
