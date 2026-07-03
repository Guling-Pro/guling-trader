"""定位并校验 xiadan 的自选股文件 SelfStockInfo.json（新版自选股来源）。只读，临时探针。

自选股列表在 xiadan 里是 CEF(内嵌Chromium) 渲染的网页，原生控件读不到；但 xiadan 会把
自选股维护到本地 SelfStockInfo.json。本探针从 xiadan.exe 进程路径出发搜这个文件，报路径、
更新时间、解析出的自选股数量与前几只，确认它是否可用作 get_watchlist 的数据源。

用法（项目根，任意 shell）：
    python tools\\probe_watchlist.py
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

try:
    import psutil
except Exception:
    psutil = None

# 1) 找 xiadan.exe 的安装目录
roots = set()
if psutil:
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if (p.info["name"] or "").lower() == "xiadan.exe" and p.info["exe"]:
                roots.add(os.path.dirname(p.info["exe"]))
        except Exception:
            pass
# 常见同花顺安装目录兜底
for guess in (r"C:\同花顺软件", r"C:\Program Files\同花顺", r"C:\Program Files (x86)\同花顺",
              r"D:\同花顺软件", os.path.expanduser(r"~\同花顺")):
    if os.path.isdir(guess):
        roots.add(guess)

print("搜索根目录:", roots or "（未找到 xiadan 进程/安装目录，请手动指认）")

# 2) 在这些根目录下递归找 SelfStockInfo.json
found = []
for r in roots:
    for f in glob.glob(os.path.join(r, "**", "SelfStockInfo.json"), recursive=True):
        found.append(f)

if not found:
    print("\n未找到 SelfStockInfo.json。请在 xiadan 图标右键→打开文件位置，把含账户号的目录路径告诉我。")
    sys.exit(0)

print(f"\n=== 找到 {len(found)} 个 SelfStockInfo.json ===")
for f in found:
    try:
        age = int(time.time() - os.path.getmtime(f))
    except Exception:
        age = -1
    print(f"\n路径: {f}")
    print(f"更新: {age} 秒前  ({'新鲜' if 0 <= age < 3600 else '偏旧，可能 xiadan 不刷新它'})")
    try:
        raw = json.loads(open(f, encoding="utf-8").read())
        print(f"解析: {len(raw)} 条自选股；前 5 条 = {raw[:5]}")
    except Exception as e:
        print("解析失败:", e)

print("\n把路径 + 更新秒数贴回。若'新鲜'，get_watchlist 就读这个文件。")
