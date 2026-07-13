"""同花顺弹窗【结构测绘】诊断工具：dump 当前所有弹窗的控件树（只读，不点击）。

用途：验证 DialogSentry（src/trader/ths/dialogs.py）的结构化处置在当前
xiadan 版本/皮肤上是否可行——弹窗的按钮是不是原生 Button、文本在不在
Static 里、有没有 Edit（验证码特征）。**在 xiadan 里随便弄出一个弹窗
（如设置里触发的提示框）后运行本脚本**，把输出发回来核对。

用法（项目根，任意 shell，弹窗保持打开）：
    python tools\\ths_dialog_dump.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32gui
import win32process
import win32api
import win32con

from trader.ths import win as W
from trader.ths.dialogs import DialogSentry, normalize_button_label, choose_button

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", b.hwnd_main)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定到下单窗口，先确认已打开并登录")

tid, pid = win32process.GetWindowThreadProcessId(b.hwnd_main)
tops = []
win32gui.EnumThreadWindows(tid, lambda h, acc: acc.append(h) or True, tops)
print(f"\n主窗口线程 tid={tid} 共 {len(tops)} 个顶层窗口：\n")

for h in tops:
    vis = win32gui.IsWindowVisible(h)
    ena = win32gui.IsWindowEnabled(h)
    cls = win32gui.GetClassName(h)
    title = win32gui.GetWindowText(h)
    mark = " ← 主窗口" if h == b.hwnd_main else ""
    print(f"[top] hwnd=0x{h & 0xFFFFFFFF:X} cls={cls!r} vis={vis} ena={ena} title={title!r}{mark}")
    if h == b.hwnd_main or not (vis and ena):
        continue

    def walker(ch, depth_holder):
        cls_c = win32gui.GetClassName(ch)
        cid = win32api.GetWindowLong(ch, win32con.GWL_ID)
        txt = win32gui.GetWindowText(ch)
        vis_c = win32gui.IsWindowVisible(ch)
        print(f"    child hwnd=0x{ch & 0xFFFFFFFF:X} cls={cls_c!r} id=0x{cid & 0xFFFF:X} "
              f"vis={vis_c} text={txt!r}")
        return True

    try:
        win32gui.EnumChildWindows(h, walker, None)
    except Exception as e:
        print(f"    (EnumChildWindows: {e} —— 可能是无子控件的自绘弹窗)")

print("\n=== DialogSentry 视角（将如何处置，仅演算不点击） ===")
for dlg in DialogSentry(b).scan():
    labels = list(dlg.buttons)
    pick = "input_ocr(含Edit)" if dlg.has_edit else (
        f"click:{choose_button(labels)}" if choose_button(labels) else "enter/wm_close 兜底")
    print(f"hwnd=0x{dlg.hwnd & 0xFFFFFFFF:X} title={dlg.title!r}")
    print(f"  text={dlg.text!r}")
    print(f"  buttons={labels} has_edit={dlg.has_edit} → 动作: {pick}")
