"""同花顺下单窗口【架构测绘】：类名链 + 左侧树菜单全量 dump（只读，不下单）。

用途：把窗口的控件骨架和左侧 SysTreeView32 菜单的完整层级/文字打出来，作为
"控件架构 + 菜单布局"的权威记录，供设计稳健导航（按树节点选择而非脆弱文字匹配）
以及排查新旧皮肤差异使用。

用法（项目根，任意 shell）：
    python tools\\dump_arch.py
"""
import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import win32api
import win32gui
import win32process

from trader.ths import win as W

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", b.hwnd_main)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定到下单窗口，先确认已打开并登录")

# ---- 1. 控件骨架（类名链解析结果）----
tree = b.get_tree_hwnd()
print("\n=== 控件骨架 ===")
print(f"get_tree_hwnd(左侧树)     = 0x{tree & 0xFFFFFFFF:X}  cls={win32gui.GetClassName(tree) if tree else '-'}")
right = b.get_right_hwnd()
print(f"get_right_hwnd(右侧面板)  = 0x{right & 0xFFFFFFFF:X}")
tabs = b.get_left_bottom_tabs()
print(f"get_left_bottom_tabs(底tab)= 0x{tabs & 0xFFFFFFFF:X}")

# ---- 2. 左侧树菜单全量 dump（跨进程读 TVITEM 文字，带层级缩进）----
print("\n=== 左侧树菜单（层级 / 文字）===")
if not tree:
    raise SystemExit("!! 未定位到 SysTreeView32，树菜单无法 dump")

# 关键：先像 settlement 那样 switch_to_normal() 激活/归位窗口再读。冷读(脚本刚绑定、
# 未与窗口交互)常把 TVITEM 文字读成空——settlement 之所以能读出，是它导航前做了
# switch_to_normal + 激活，让树被完全实现(realized)。
import time
try:
    b.switch_to_normal()
    time.sleep(0.3)
except Exception as e:
    print("  switch_to_normal 失败(忽略):", e)
# 重新取一次树句柄（激活后更稳）
tree = b.get_tree_hwnd() or tree

_, pid = win32process.GetWindowThreadProcessId(tree)
PROCESS_VM = 0x0008 | 0x0010 | 0x0020
MEM = 0x1000 | 0x2000
PAGE_RW = 0x04
k32 = ctypes.windll.kernel32
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]

h_proc = win32api.OpenProcess(PROCESS_VM, False, pid)
bufsize = 512
remote_text = k32.VirtualAllocEx(int(h_proc), None, bufsize, MEM, PAGE_RW)
remote_item = k32.VirtualAllocEx(int(h_proc), None, ctypes.sizeof(W._TVITEMW), MEM, PAGE_RW)
print(f"  [诊断] tree=0x{tree & 0xFFFFFFFF:X} pid={pid} h_proc={int(h_proc)} "
      f"remote_text={remote_text} remote_item={remote_item}")

_diag = {"n": 0}


def read_text(hitem):
    item = W._TVITEMW()
    item.mask = W.TVIF_TEXT
    item.hItem = hitem
    item.pszText = remote_text
    item.cchTextMax = bufsize // 2
    wpm = k32.WriteProcessMemory(int(h_proc), remote_item, ctypes.byref(item),
                                 ctypes.sizeof(item), None)
    rc = win32gui.SendMessage(tree, W.TVM_GETITEMW, 0, remote_item)
    buf = (ctypes.c_char * bufsize)()
    rpm = k32.ReadProcessMemory(int(h_proc), remote_text, buf, bufsize, None)
    txt = buf.raw.decode("utf-16-le", "ignore").split("\x00", 1)[0]
    if _diag["n"] < 3:  # 前 3 个节点打印原始返回值，读空时可据此定位
        _diag["n"] += 1
        print(f"  [诊断#{_diag['n']}] hitem={hitem} WPM={wpm} SendMsg={rc} RPM={rpm} "
              f"raw16={buf.raw[:16].hex()} txt={txt!r}")
    return txt


count = 0


def walk(hitem, depth):
    global count
    while hitem:
        txt = read_text(hitem).replace(" ", "").replace("　", "")
        print(f"  {'  ' * depth}[{depth}] {txt!r}")
        count += 1
        child = win32gui.SendMessage(tree, W.TVM_GETNEXTITEM, W.TVGN_CHILD, hitem)
        if child:
            walk(child, depth + 1)
        hitem = win32gui.SendMessage(tree, W.TVM_GETNEXTITEM, W.TVGN_NEXT, hitem)


walk(win32gui.SendMessage(tree, W.TVM_GETNEXTITEM, W.TVGN_ROOT, 0), 0)
print(f"\n共 {count} 个树节点。")
print("提示：把这份菜单层级贴回，即可据此设计按节点路径导航(如 查询→交割单)，"
      "替代脆弱的整串文字匹配。")
