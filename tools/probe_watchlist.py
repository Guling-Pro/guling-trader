"""探查 xiadan「自选股」面板：在整个窗口里定位【可见】的自选股 grid（新版专有）。只读，临时探针。

上一版发现自选股视图不在 get_right_hwnd 容器里（那里的 grid 全 vis=0、且是残留持仓表）。
本版在 hwnd_main 全窗口枚举所有 CVirtualGridCtrl，打印 hwnd/id/可见/矩形，并读出【可见】
那张的表头——表头是 代码/名称/涨幅/现价 即自选股。

运行前请把自选股面板停在【自选】tab（不是持仓）。
用法（项目根，任意 shell，停掉 python -m trader）：
    python tools\\probe_watchlist.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32gui

from trader.ths import win as W

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", hex(b.hwnd_main) if b.hwnd_main else None)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定，先确认已打开并登录（新版），且自选股停在【自选】tab")

b.switch_to_normal()
print("导航到「自选股」节点:", b._select_tree_node_by_text("自选股"))
time.sleep(1.2)

# 全窗口枚举所有 CVirtualGridCtrl
grids = []


def wk(h, _):
    try:
        if win32gui.GetClassName(h) == "CVirtualGridCtrl":
            grids.append((
                h, win32gui.GetDlgCtrlID(h) & 0xFFFF,
                int(bool(win32gui.IsWindowVisible(h))),
                win32gui.GetWindowRect(h),
            ))
    except Exception:
        pass
    return True


win32gui.EnumChildWindows(b.hwnd_main, wk, None)
print(f"\n=== 全窗口 CVirtualGridCtrl 共 {len(grids)} 张 ===")
for h, cid, vis, rect in grids:
    w, ht = rect[2] - rect[0], rect[3] - rect[1]
    print(f"  hwnd=0x{h:06X}  id=0x{cid:04X}  vis={vis}  {w}x{ht}  rect={rect}")

# 读每张【可见】grid 的表头，识别自选股
print("\n=== 各【可见】grid 表头 ===")
for h, cid, vis, rect in grids:
    if not vis:
        continue
    data = b.read_table_text(h)
    head = (data or "").splitlines()[0][:140] if data else "(空)"
    tag = "★自选股" if ("涨幅" in head and "现价" in head) else (
        "持仓" if "股票余额" in head or "参考成本价" in head else "?")
    print(f"  hwnd=0x{h:06X} id=0x{cid:04X} [{tag}]  表头: {head}")

print("\n把上面贴回：我要那张 [★自选股] grid 的 hwnd/id/大小，据此让 get_watchlist 精确定位它读取。")
