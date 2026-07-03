"""探测 xiadan 的内嵌 CEF(Chromium) 是否暴露 remote-debugging 调试端口。只读，临时探针。

新版 xiadan 的自选股是 CEF 渲染的网页，数据来自内部 HTTP 请求。若 CEF 开了调试端口，
就能用 CDP(和 cdp_client.py 连雪球同一套)直接读网页 DOM 或拦网络，拿到实时自选股。
本探针枚举 xiadan.exe 及其子进程监听的本地端口，逐个测 CDP 的 /json/version 接口。

用法（项目根，任意 shell）：
    python tools\\probe_watchlist.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

try:
    import psutil
except Exception:
    print("需要 psutil：pip 装过就有。缺了就 py -3.11 -m pip install psutil")
    raise SystemExit(1)

# 1) 找 xiadan.exe 及其所有子进程（CEF 的 browser/renderer 子进程）
targets = {}
for p in psutil.process_iter(["name", "pid", "ppid"]):
    try:
        nm = (p.info["name"] or "").lower()
        if nm == "xiadan.exe":
            targets[p.info["pid"]] = p
    except Exception:
        pass
# 加上 xiadan 的子进程（CEF 子进程名可能是 xiadan.exe / HevExecute / cef 等）
all_procs = {p.pid: p for p in psutil.process_iter(["name", "pid", "ppid"])}
for pid, p in list(all_procs.items()):
    try:
        if p.info.get("ppid") in targets:
            targets[pid] = p
    except Exception:
        pass

print("xiadan 相关进程:", {pid: (p.info["name"]) for pid, p in targets.items()} or "（未找到 xiadan.exe）")

# 2) 枚举这些进程监听的本地端口
ports = set()
try:
    for c in psutil.net_connections(kind="tcp"):
        if c.status == psutil.CONN_LISTEN and c.pid in targets and c.laddr:
            ports.add(c.laddr.port)
except Exception as e:
    print("枚举端口失败（可能需要管理员权限）:", e)

print("监听端口:", sorted(ports) or "（无）")

# 3) 逐个测 CDP 接口
print("\n=== 逐端口测 CDP /json/version ===")
hit = []
for port in sorted(ports):
    for host in ("127.0.0.1", "localhost"):
        url = f"http://{host}:{port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                body = r.read().decode("utf-8", "ignore")
            if "Browser" in body or "webSocketDebuggerUrl" in body or "Chrome" in body:
                print(f"  ★ {url} → 是 CDP！{body[:200]}")
                hit.append(port)
            else:
                print(f"    {url} → 有响应但不像 CDP: {body[:80]}")
            break
        except Exception:
            pass

if hit:
    print(f"\n✅ 发现 CDP 端口 {hit} —— 可走【路①】：CDP 读自选股网页。再测 /json 列出页面：")
    for port in hit:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
                pages = json.loads(r.read())
            for pg in pages[:20]:
                print(f"    - title={pg.get('title')!r} url={pg.get('url')!r}")
        except Exception as e:
            print("  列页面失败:", e)
else:
    print("\n❌ 没探到 CDP 调试端口 —— xiadan 的 CEF 没开 remote-debugging。")
    print("   那路①走不通（除非能让它带 --remote-debugging-port 启动，较侵入）。")
    print("   把上面【监听端口】贴回，我再判断有没有别的内部接口可利用；否则退回路②(MITM 抓包)。")
