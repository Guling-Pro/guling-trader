"""买入/卖出【表单填写】自检——只填参数，绝不提交（不下单）。

用和真实下单同一套控件查找(_find_input: 证券代码0x408/价格0x409/数量0x40A)+ set_text
把三个框填上，再回读确认。用于在新版本/新皮肤上验证下单表单字段映射是否正确，
而不需要真的发出委托（周末/收盘也能测）。**本脚本不会按下"买入/卖出"按钮。**

用法（项目根，任意 shell）：
    python tools\\check_order_form.py [buy|sell] [证券代码] [价格] [数量]
例：
    python tools\\check_order_form.py sell 000970 16.480 100
    python tools\\check_order_form.py buy  600000 7.58   100
默认：sell 000970 16.480 100
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from trader.ths import win as W

which = (sys.argv[1] if len(sys.argv) > 1 else "sell").lower()
code = sys.argv[2] if len(sys.argv) > 2 else "000970"
price = sys.argv[3] if len(sys.argv) > 3 else "16.480"
qty = sys.argv[4] if len(sys.argv) > 4 else "100"

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", b.hwnd_main)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定到下单窗口，先确认已打开并登录")

panel = "F2" if which == "sell" else "F1"
label = "卖出" if which == "sell" else "买入"
print(f"\n=== {label}表单填写自检（{panel}，只填不交）===")

b.switch_to_normal()
W._activate_window(b.hwnd_main)
W.hot_key([panel])
time.sleep(0.4)
hwnd = b.get_right_hwnd()
print(f"right_hwnd = 0x{hwnd & 0xFFFFFFFF:X}")

fields = [("证券代码", 0x408, code, False), ("价格", 0x409, price, True), ("数量", 0x40A, qty, False)]
for name, cid, val, is_price in fields:
    ctrl = b._find_input(hwnd, cid)
    if not ctrl:
        print(f"  ✗ {name} 0x{cid:04X}: 未找到可见输入框!")
        continue
    W.set_text(ctrl, str(val), is_price)
    time.sleep(0.25)
    readback = W.get_text(ctrl)
    ok = "✓" if str(val).rstrip("0").rstrip(".") in readback.replace(" ", "") or readback else "?"
    print(f"  {ok} {name} 0x{cid:04X}: 填入 {val!r}  →  回读 {readback!r}")

print("\n>>> 只填了表单、【未提交】。请在界面上肉眼核对：")
print("    ① 证券代码/价格/数量 三个框是否填对；② 证券名称是否自动带出。")
print("    确认后手动清空或切走即可，不会下单。把上面的回读结果贴回。")
