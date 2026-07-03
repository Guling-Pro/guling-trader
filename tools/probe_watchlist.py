"""探查 xiadan「自选股」面板：自选/持仓 tab + 两张 grid 的结构（新版专有）。只读，临时探针。

自选股面板顶部有「自选 | 持仓」两个 tab，各对应一张 CVirtualGridCtrl。需先切到「自选」
tab 再读对应 grid。本探针 dump tab 控件 + 两张 grid 的 id/可见/矩形，并读一次当前可见 grid，
据此实现 get_watchlist。摸清后即删。

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
print("hwnd_main =", b.hwnd_main)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定到下单窗口，先确认已打开并登录（新版）")

b.switch_to_normal()
print("导航到「自选股」节点:", b._select_tree_node_by_text("自选股"))
time.sleep(1.0)

hwnd = b.get_right_hwnd()
print("right_hwnd =", hex(hwnd & 0xFFFFFFFF) if hwnd else None)

# dump: 自选/持仓 tab 相关控件 + 所有 CVirtualGridCtrl + CCustomTabCtrl（含矩形，便于必要时坐标点击）
print("\n=== 自选/持仓 tab 与表格控件（id / 类名 / 文字 / 可见 / 矩形）===")
rows = []


def wk(h, _):
    try:
        cls = win32gui.GetClassName(h)
        txt = (win32gui.GetWindowText(h) or "").strip()
        keep = cls in ("CVirtualGridCtrl", "CCustomTabCtrl") or any(
            t in txt for t in ("自选", "持仓")
        )
        if keep:
            rows.append((
                win32gui.GetDlgCtrlID(h) & 0xFFFF, cls, txt,
                int(bool(win32gui.IsWindowVisible(h))),
                win32gui.GetWindowRect(h),
            ))
    except Exception:
        pass
    return True


if hwnd:
    win32gui.EnumChildWindows(hwnd, wk, None)
for cid, cls, txt, vis, rect in rows:
    print(f"  id=0x{cid:04X}  {cls:<18} vis={vis}  rect={rect}  {txt!r}")

# 读一次当前可见 grid 的前几行（看现在停在哪个 tab）
grid = b._find_grid(hwnd) if hwnd else 0
print("\n_find_grid（当前可见）=", hex(grid & 0xFFFFFFFF) if grid else None)
if grid:
    data = b.read_table_text(grid)
    head = "\n".join((data or "").splitlines()[:3])
    print("当前可见 grid 前 3 行：\n" + head)
    print("\n↑ 若表头是 代码/名称/涨幅/现价 → 是【自选】；若是 股票余额/参考成本价 → 是【持仓】。")

print("\n把上面贴回：我看两张 grid 的 id/可见性 + tab 结构，据此让 get_watchlist 先切「自选」再读。")
