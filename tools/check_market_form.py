"""市价委托【填单+设策略】自检——只填不提交（绝不下单）。临时探针。

验证市价委托路径的两个关键机制，不发单：
1. 证券代码/数量 填值（0x408 / 0x40A，与 F1/F2 同 ID）；
2. 委托策略用【键盘数字】切到五档即成剩撤（买入发"1"、卖出发"4"）。
读回确认，绝不点提交。你再肉眼核对：代码/名称/数量对不对、委托策略是否变成五档即成剩撤。

前置：手动点开【市价委托 └ 买入】或【市价委托 └ 卖出】面板（脚本按面板标题自动判买卖）。
用法（项目根，任意 shell）：
    python tools\\check_market_form.py [证券代码] [数量]
默认：000970 100
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32con
import win32gui
import win32process

from trader.ths import win as W

code = sys.argv[1] if len(sys.argv) > 1 else "000970"
qty = sys.argv[2] if len(sys.argv) > 2 else "100"

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
if not b.hwnd_main:
    raise SystemExit("!! 未绑定，先确认新版 xiadan 已登录")

hwnd = b.get_right_hwnd()

# 按可见的面板标题判定买/卖
title = ""
def _tw(h, _):
    global title
    try:
        if win32gui.IsWindowVisible(h):
            t = (win32gui.GetWindowText(h) or "").strip()
            if t in ("市价买入", "市价卖出"):
                title = t
    except Exception:
        pass
    return True
win32gui.EnumChildWindows(b.hwnd_main, _tw, None)
if title not in ("市价买入", "市价卖出"):
    raise SystemExit("!! 当前不是市价买入/卖出面板，请先手动点开【市价委托 └ 买入/卖出】再运行")
side = "买入" if title == "市价买入" else "卖出"
strat_key = "1" if side == "买入" else "4"
print(f"面板 = {title}  → 策略键 = {strat_key}（五档即成剩撤）")

MARKET_CODE_ID, MARKET_AMOUNT_ID, MARKET_STRATEGY_COMBO_ID = 0x408, 0x40A, 0x605

# 1) 填证券代码 + 数量（复用 set_text WM_CHAR 直发）
W._activate_window(b.hwnd_main)
code_edit = b._find_input(hwnd, MARKET_CODE_ID)
W.set_text(code_edit, code)
time.sleep(0.4)
amt_edit = b._find_input(hwnd, MARKET_AMOUNT_ID)
W.set_text(amt_edit, str(qty))
time.sleep(0.3)

# 2) 委托策略：聚焦 ComboBox → 发数字键（键盘切换，触发同花顺策略变更）
combo = b._find_ctrl_by_id(hwnd, MARKET_STRATEGY_COMBO_ID, cls="ComboBox", visible=True) \
    or b._find_ctrl_by_id(hwnd, MARKET_STRATEGY_COMBO_ID)
CB_GETCURSEL, CB_GETCOUNT = 0x0147, 0x0146
before_idx = win32gui.SendMessage(combo, CB_GETCURSEL, 0, 0)
cnt = win32gui.SendMessage(combo, CB_GETCOUNT, 0, 0)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
my_tid = kernel32.GetCurrentThreadId()
tgt_tid, _ = win32process.GetWindowThreadProcessId(combo)
attached = False
try:
    if my_tid != tgt_tid:
        attached = bool(user32.AttachThreadInput(my_tid, tgt_tid, True))
    user32.SetFocus(combo)
    # WM_CHAR 数字 → 标准 ComboBox 增量匹配"以该数字开头"的项（1-.../4-...）
    user32.SendMessageW(combo, win32con.WM_CHAR, ord(strat_key), 0)
finally:
    if attached:
        user32.AttachThreadInput(my_tid, tgt_tid, False)
time.sleep(0.4)
after_idx = win32gui.SendMessage(combo, CB_GETCURSEL, 0, 0)

print(f"\n证券代码 0x{MARKET_CODE_ID:X} 回读: {W.get_text(code_edit)!r}")
print(f"数量     0x{MARKET_AMOUNT_ID:X} 回读: {W.get_text(amt_edit)!r}")
print(f"委托策略 ComboBox 0x{MARKET_STRATEGY_COMBO_ID:X}: 共{cnt}项, 发'{strat_key}'键前index={before_idx} → 后index={after_idx}")
exp = 0 if side == "买入" else 3  # 买入五档=index0, 卖出五档=index3
print(f"期望选中 index={exp}（{side}的五档即成剩撤）→ {'✓ 命中' if after_idx == exp else '✗ 未命中，键盘法可能要换方式(见下)'}")

print("""
>>> 【只填了单，未提交】。请肉眼核对界面：
    ① 证券代码/名称/数量对不对；② 委托策略是否显示「五档即成剩撤」。
    确认后点「重填」或切走即可，不会下单。把上面输出 + 界面情况贴回。
    若 after_index 没到期望：键盘 WM_CHAR 对这个 ComboBox 不生效，改用真实点击下拉+点项，
    或 CB_SETCURSEL(index) 兜底——告诉我，我调。""")
