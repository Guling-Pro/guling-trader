"""验证市价委托路径可行性（只读，绝不下单/不按回车）。临时探针。

查三件事，决定 2026-07-04-ths-market-order.md 计划是否成立：
1. 「市价委托」面板是【原生控件】还是【CEF 网页】(像自选股那样不可控件化)？
2. 「委托策略」下拉是【标准 ComboBox】还是【同花顺自绘】(决定 CB_SETCURSEL 是否可用)？
3. F1 买入面板有没有【原生订单类型/市价选项】(万一市价委托是 CEF 的替代方案)？

用法（项目根，任意 shell，新版 xiadan、已登录；停掉 python -m trader 避免抢窗口）：
    python tools\\probe_market.py
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
    raise SystemExit("!! 未绑定，先确认新版 xiadan 已登录")


def dump_cef():
    """整窗里可见的 CEF 渲染窗口——有说明该面板是网页渲染。"""
    hits = []

    def wk(h, _):
        try:
            c = win32gui.GetClassName(h)
            if c in ("Chrome_RenderWidgetHostHWND", "CefBrowserWindow", "Chrome_WidgetWin_0") \
                    and win32gui.IsWindowVisible(h):
                r = win32gui.GetWindowRect(h)
                hits.append((c, (r[2] - r[0]), (r[3] - r[1])))
        except Exception:
            pass
        return True

    win32gui.EnumChildWindows(b.hwnd_main, wk, None)
    return hits


def dump_right_controls():
    """get_right_hwnd 下的控件：id/类名/文字/可见——看有没有原生 Edit/ComboBox。"""
    hwnd = b.get_right_hwnd()
    rows = []

    def wk(h, _):
        try:
            rows.append((
                win32gui.GetDlgCtrlID(h) & 0xFFFF,
                win32gui.GetClassName(h),
                (win32gui.GetWindowText(h) or "").strip()[:20],
                int(bool(win32gui.IsWindowVisible(h))),
            ))
        except Exception:
            pass
        return True

    if hwnd:
        win32gui.EnumChildWindows(hwnd, wk, None)
    return hwnd, rows


def report(label):
    time.sleep(1.0)
    cef = dump_cef()
    hwnd, rows = dump_right_controls()
    print(f"\n===== {label}  right_hwnd={hex(hwnd & 0xFFFFFFFF) if hwnd else None} =====")
    print(f"可见 CEF 渲染窗口: {cef or '无'}  {'← 网页渲染!' if cef else ''}")
    # 只打印有意义的控件（Edit/ComboBox/Button/带文字的 Static）
    interesting = [r for r in rows if r[1] in ("Edit", "ComboBox", "ComboBoxEx32", "Button")
                   or "ComboBox" in r[1] or "策略" in r[2] or "市价" in r[2]
                   or "五档" in r[2] or "类型" in r[2] or "限价" in r[2] or "对手" in r[2]]
    print(f"关键控件({len(interesting)}/{len(rows)}):")
    for cid, cls, txt, vis in interesting:
        print(f"  id=0x{cid:04X}  {cls:<16} vis={vis}  {txt!r}")
    edits = [r for r in interesting if r[1] == "Edit" and r[3]]
    combos = [r for r in interesting if "ComboBox" in r[1]]
    print(f"→ 可见 Edit {len(edits)} 个, ComboBox {len(combos)} 个")


# ① 市价委托面板
b.switch_to_normal()
print("\n导航「市价委托」:", b._select_tree_node_by_text("市价委托"))
report("市价委托 面板")

# ② F1 买入面板（对照：有没有原生订单类型/市价选项）
b.switch_to_normal()
W.hot_key(["F1"])
report("F1 买入 面板")

print("""
=== 判读 ===
- 市价委托面板：有【可见 CEF】且【无可见 Edit】→ 是网页(计划作废，需改道)；
               有原生 Edit + ComboBox → 原生(计划成立)。
- ComboBox 类名是 'ComboBox' → 标准，CB_SETCURSEL 可用；带 'Custom'/自绘类 → 要键盘选。
- F1 面板若也有 ComboBox / 含"市价""类型"的控件 → 可能有原生市价选项(替代方案)。
把两段输出贴回。""")
