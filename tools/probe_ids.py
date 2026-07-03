"""逐面板测绘同花顺下单窗口的控件 ID（只读，不下单）。

用途（两件事一次做完）：
1. 兼容性检测：判断当前客户端（新版/旧版皮肤）是否被现有 RPA 支持——
   逐个查询面板检查旧版期望的控件 ID 是否还在，给出 SUPPORTED / MISMATCH 判定。
2. 若不匹配：dump 出各面板右区里【实际存在】的控件 ID + 类名 + 文本，
   供维护者据此把 const.py / win.py 里的 ID 重映射到新版。

用法（项目根，cmd）：
    set PYTHONPATH=src
    python tools\\probe_ids.py

注意：会用 F4/F1/F6/F8/F2/F7 等热键切换【查询】面板，不触发任何下单。
收盘/周末运行最稳。运行时最好让 `python -m trader` 先退出，避免两者同时驱动窗口。
"""
import time

import win32api
import win32con
import win32gui

from trader.ths import const
from trader.ths import win as W

W.setup("网上股票交易系统5.0", "", "")
b = W.WinThsBackend()
b.bind_client()
print("hwnd_main =", b.hwnd_main)
if not b.hwnd_main:
    raise SystemExit("!! 未绑定到下单窗口，先确认已打开并登录")


def _cid(h) -> int:
    try:
        return win32api.GetWindowLong(h, win32con.GWL_ID) & 0xFFFF
    except Exception:
        return -1


def dump_scope(root: int, label: str) -> set:
    """打印 root 下可见且(有文本/是输入或表格)的控件，返回出现过的控件 ID 集合。"""
    print(f"\n----- {label}  root=0x{root & 0xFFFFFFFF:X} -----")
    ids = set()
    rows = []

    def wk(h, _):
        cid = _cid(h)
        ids.add(cid)
        try:
            cls = win32gui.GetClassName(h)
            vis = win32gui.IsWindowVisible(h)
            txt = (win32gui.GetWindowText(h) or "").strip()
        except Exception:
            return True
        if vis and (txt or cls in ("Edit", "RICHEDIT", "CVirtualGridCtrl") or "Grid" in cls):
            rows.append((cid, cls, txt))
        return True

    try:
        win32gui.EnumChildWindows(root, wk, None)
    except Exception as e:
        print("  枚举失败:", e)
        return ids
    for cid, cls, txt in rows:
        t = txt[:44].replace("\n", " ")
        print(f"  id=0x{cid:04X}  {cls:<18} {t!r}")
    return ids


def nav(keys, label):
    b.switch_to_normal()
    for k in keys:
        W.hot_key([k])
        time.sleep(0.3)
    try:
        b.refresh()
    except Exception as e:
        print("  refresh 失败:", e)
    time.sleep(0.5)
    right = b.get_right_hwnd()
    return dump_scope(right, f"{label}  right_hwnd")


# --- 逐面板测绘 ---
bal_ids = nav(["F4"], "F4 资金")
pos_ids = nav(["F1", "F6"], "F1+F6 持仓")
act_ids = nav(["F1", "F8"], "F1+F8 未成交委托")
fil_ids = nav(["F2", "F7"], "F2+F7 当日成交")

# --- 兼容性判定 ---
print("\n==================== 兼容性判定 ====================")
exp_bal = set(const.BALANCE_CONTROL_ID_GROUP.values())
miss_bal = {k: hex(v) for k, v in const.BALANCE_CONTROL_ID_GROUP.items() if v not in bal_ids}
print(f"[资金 F4] 期望字段ID={[hex(v) for v in exp_bal]}")
print(f"          缺失={miss_bal or '无 → 资金面板 SUPPORTED'}")
for name, ids in [("持仓F1F6", pos_ids), ("未成交F1F8", act_ids), ("成交F2F7", fil_ids)]:
    ok = 0x417 in ids
    print(f"[{name}] 表格ID 0x417 {'存在 → SUPPORTED' if ok else '缺失 → MISMATCH（需重映射表格ID）'}")

print("\n提示：若上面判 MISMATCH，就在对应面板的 dump 里找带数字/表格的控件，")
print("      把它的 id=0xXXXX 告诉维护者即可精确重映射（资金字段按文本对号入座）。")
