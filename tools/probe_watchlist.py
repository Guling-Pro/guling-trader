"""探查 xiadan「自选股」面板结构（新版专有；旧版无此菜单）。只读，临时探针。

用途：确认自选股面板是不是可 Ctrl+C 的 CVirtualGridCtrl、有哪些列，决定 get_watchlist
怎么实现。摸清后本脚本即可删除。

用法（项目根，任意 shell，需停掉 python -m trader 避免抢窗口）：
    python tools\\probe_watchlist.py
"""
import collections
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

# 1) 标签导航到「自选股」树节点
b.switch_to_normal()
ok = b._select_tree_node_by_text("自选股")
print("导航到「自选股」节点:", ok, "（False = 旧版无此菜单 / 树文字没读到）")
time.sleep(1.0)

hwnd = b.get_right_hwnd()
print("right_hwnd =", hex(hwnd & 0xFFFFFFFF) if hwnd else None)

# 2) 右面板控件类名分布（看是不是标准 CVirtualGridCtrl 表格）
if hwnd:
    cnt: collections.Counter = collections.Counter()

    def wk(h, _):
        cnt[win32gui.GetClassName(h)] += 1
        return True

    win32gui.EnumChildWindows(hwnd, wk, None)
    print("\n=== 右面板控件类名分布 ===")
    for k, n in sorted(cnt.items()):
        print(f"  {n:3}  {k}")

# 3) 试着按表格读一次（复用 _find_grid + read_table_text）
grid = b._find_grid(hwnd) if hwnd else 0
print("\n_find_grid =", hex(grid & 0xFFFFFFFF) if grid else None)
if grid:
    data = b.read_table_text(grid)
    if data:
        print("=== 自选股表格文本（前 1000 字符）===")
        print(data[:1000])
    else:
        print("read_table_text 返回空（自选股可能不是可拷表格，需换方案）")

print("\n把上面输出贴回：确认自选股是不是可拷 CVirtualGridCtrl、含哪些列（代码/名称/现价…）。")
