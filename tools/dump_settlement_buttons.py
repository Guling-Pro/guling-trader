"""导航到交割单面板，dump 时段按钮(近一周/近一月/近三月/近一年/自定义)的控件 ID。

用途：拿到这些按钮的控件 ID，把 settlement 的时段切换从"按文字匹配"改成"按 ID +
可见性"，彻底去掉文字匹配。只读，不下单。

用法（项目根，任意 shell）：
    python tools\\dump_settlement_buttons.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32gui

from trader.ths import win as W

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", b.hwnd_main)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定到下单窗口，先确认已打开并登录")

# 导航到交割单面板（用刚改好的标签导航）
b._goto_settlement_panel()

TARGETS = ("近一周", "近一月", "近三月", "近一年", "自定义", "过滤", "汇总")
print("\n=== 交割单面板 时段/操作按钮（id / 类名 / 文字 / 可见 / 可用）===")
rows = []


def walker(h, _):
    try:
        txt = (win32gui.GetWindowText(h) or "").strip()
        if any(t in txt for t in TARGETS):
            rows.append((
                win32gui.GetDlgCtrlID(h) & 0xFFFF,
                win32gui.GetClassName(h),
                txt,
                int(bool(win32gui.IsWindowVisible(h))),
                int(bool(win32gui.IsWindowEnabled(h))),
            ))
    except Exception:
        pass
    return True


win32gui.EnumChildWindows(b.hwnd_main, walker, None)
for cid, cls, txt, vis, en in rows:
    print(f"  id=0x{cid:04X}  {cls:<10} vis={vis} en={en}  {txt!r}")

print("\n重点看【vis=1 en=1】那几行的 id —— 那就是交割单面板当前可用的时段按钮 ID。")
print("把这份贴回，我就把 date_range→ID 映射死，去掉文字匹配。")
