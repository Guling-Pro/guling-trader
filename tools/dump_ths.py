"""同花顺下单窗口诊断脚本（只读，不下单）。

用途：判断"新版/旧版"界面切换后，控件类名链是否还能被现有自动化定位——
即回答"新版是否支持、要不要改代码"。切到某个版本后运行，把输出贴给维护者即可。

用法（项目根目录，cmd）：
    set PYTHONPATH=src
    python tools/dump_ths.py

对照读法：
- 第 4 节三个 get_*_hwnd 都非 0        → 该界面直接可用；
- 有的为 0，但类名分布里仍有 SysTreeView32 且 Afx*NNNs 后缀变了（NNN≠140）
                                       → 只是 MFC 版本号变了，改成前缀匹配即可（修法2）；
- 类名里根本没有 SysTreeView32、全是陌生自绘类
                                       → 新版重写了 UI，标准控件自动化不可行。
"""
import collections

import win32gui
import win32process

try:
    import psutil
except Exception:  # 诊断脚本，缺 psutil 不致命
    psutil = None


def exe_of(pid: int) -> str:
    if not psutil:
        return "?"
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "?"


# 1) 列出所有可见顶层窗口——新版若改了窗口标题，靠这个也能认出下单窗口
print("=== 顶层可见窗口（标题 / 类名 / 进程）===")
tops: list[tuple[int, str, str, int, str]] = []


def _top_cb(h, _):
    if not win32gui.IsWindowVisible(h):
        return True
    title = win32gui.GetWindowText(h) or ""
    if not title.strip():
        return True
    cls = win32gui.GetClassName(h)
    _, pid = win32process.GetWindowThreadProcessId(h)
    tops.append((h, title, cls, pid, exe_of(pid)))
    return True


win32gui.EnumWindows(_top_cb, None)
for h, title, cls, pid, exe in tops:
    print(f"hwnd={h:>10}  cls={cls:<24} exe={exe:<18} title={title!r}")

# 2) 挑候选：进程名含 xiadan，或标题含交易相关关键词
_kw = ("交易", "股票", "下单", "委托", "持仓")
cands = [x for x in tops if "xiadan" in x[4].lower() or any(k in x[1] for k in _kw)]
print("\n=== 候选下单窗口 ===")
for h, title, cls, pid, exe in cands:
    print(f"hwnd={h}  cls={cls}  exe={exe}  title={title!r}")
if not cands:
    print("（未识别到候选——若确已打开下单窗口，请把上面顶层窗口列表整段贴出来）")

# 3) 对每个候选 dump 子孙控件的类名分布，看 Afx 后缀 / 有无 SysTreeView32
for h, title, cls, pid, exe in cands:
    print(f"\n===== 控件类名分布  hwnd={h}  title={title!r} =====")
    cnt: collections.Counter = collections.Counter()

    def _wk(c, _):
        cnt[win32gui.GetClassName(c)] += 1
        return True

    try:
        win32gui.EnumChildWindows(h, _wk, None)
    except Exception as e:
        print("  枚举失败:", e)
        continue
    for k, n in sorted(cnt.items()):
        print(f"{n:4}  {k}")

# 4) 用现有绑定链探测——旧版取基线 / 新版看是否断链
print("\n=== 现有控件链探测（基于 window_title 前缀匹配）===")
from trader.ths import win as thswin  # noqa: E402

thswin.setup("网上股票交易系统5.0", "", "")
b = thswin.WinThsBackend()
b.bind_client()
print("bind hwnd_main =", b.hwnd_main)
if b.hwnd_main:
    print("get_tree_hwnd        =", b.get_tree_hwnd())
    print("get_right_hwnd       =", b.get_right_hwnd())
    print("get_left_bottom_tabs =", b.get_left_bottom_tabs())
    print("  ↑ 0 = 该链在当前界面断了(需修法2)；非 0 = 该界面可用")
else:
    print("!! 未按 window_title 前缀绑定到窗口——新版标题可能变了，"
          "对照第 1/2 节的实际标题告诉维护者。")
