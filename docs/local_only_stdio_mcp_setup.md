# guling-trader 局域网/私有化 “本地 Stdio MCP + 本地 WS 中转” 接入方案

本方案完全符合您的两大硬性约束：
1. **Windows 交易端不作为服务端，不暴露任何端口，不开发 Windows 端的 SSE 服务**：Windows 机器上的 `guling-trader.exe` 依然保持纯粹的**“主动向外发起连接”的 WebSocket 客户端（Outbound-only WS Client）**身份，避免任何公网暴露和安全防火墙隐患。
2. **合理的本地复杂化安装**：对于不希望使用 `guling.pro` 云服务的本地私有化用户，他们需要**下载本地 MCP 程序代码进行本机的 stdio 配置**。

这是目前最安全、也最符合极客开源习惯的**“100% 本地纯离线”**方案。

---

## 一、 核心架构设计：Mac 端运行“本地 WS 桥接器”

在这种设计下，我们在 Mac 宿主机上运行一个极轻量级的 **“本地 WS 桥接器” 脚本（`mcp_local_relay.py`）**。

* **Windows 客户端 (`guling-trader.exe`)**：将 WebSocket 连接终点从云端 `mcp.guling.pro` 改为**用户 Mac 的局域网 IP 地址**（在 `config.json` 的 `ws_endpoint` 填完整 URL，如 `ws://192.168.31.100:8080/api/trader-tunnel`），主动连入 Mac。
* **Mac 控制端（Agent/Cursor）**：通过本地 **stdio（标准输入输出）** 与 `mcp_local_relay.py` 进程进行高并发通信。

```
 ┌───────────────────────────────────────────┐           ┌───────────────────────────┐
 │               用户 Mac 宿主机              │           │       Windows 虚拟机       │
 │                                           │           │                           │
 │  ┌─────────────┐         ┌─────────────┐  │           │      ┌─────────────┐      │
 │  │ 本地 Agent  │         │ mcp_local_  │  │           │      │   guling-   │      │
 │  │ (Cursor /   │<───────>│  relay.py   │<─┼───────────┼──────│  trader.exe │      │
 │  │Claude Code) │  stdio  │  (WS 服务)  │  │  WS 局域网 │      │  (WS 客户端) │      │
 │  └─────────────┘         └─────────────┘  │   长连接  │      └─────────────┘      │
 └───────────────────────────────────────────┘           └───────────────────────────┘
                                                        (Windows 主动连出，不暴露任何端口)
```

---

## 二、 用户具体的“合理复杂配置流程”

不希望经过官方云中转的本地用户，需要按照以下步骤进行手动配置：

### 第一步：局域网与跨网网络连接（核心枢纽）

根据用户的运行环境，选择最适合的网络对接方式：

*   **场景 A：本地虚拟机（Mac 本地跑 Parallels / VMware）**
    这是最简单的场景。Parallels 等虚拟机会自动为 Windows 分配一个局域网 IP，且 **Windows 虚拟机可以直接连入 Mac 宿主机**。
    *   在 Windows 虚拟机内，直接连入 Mac 虚拟网卡的 IP（通常在 Parallels 下为 `10.211.55.2`，或直接使用 Mac 本地的局域网 IP）。
    *   无需安装任何额外组网软件，零成本通车。

*   **场景 B：跨网远程托管（云 Windows VPS 或 异地 Windows 电脑）**
    对于 Windows 交易机在远端云服务器，而 Agent 在异地 Mac 本地的场景，由于炒股用户不适合折腾复杂的公网 IP 映射或 Cloudflare 隧道，**推荐使用 Tailscale 进行虚拟组网**：
    1.  **极速安装**：在 Mac 和 Windows 交易机上分别下载并安装 [Tailscale](https://tailscale.com/)（提供完全图形化的双端 GUI 客户端）。
    2.  **一键登录**：两端分别用同一个 Google、GitHub 或 Microsoft 账号登录，Tailscale 会自动为两台机器分配专属的虚拟局域网 IP（例如 Mac 分配到 `100.82.115.55`，Windows 分配到 `100.82.115.42`）。
    3.  **安全内网**：两端直接通过此 IP 进行 WireGuard 金融级加密直连，彻底免去了配置公网 IP、修改云服务器防火墙安全组等繁琐步骤，完全免受公网扫描。

---

### 第二步：在 Mac 本地下载并配置 Agent (stdio 模式)
1. 下载或克隆您的 `guling-trader` 开源仓库到 Mac 本地。
2. 在 Cursor 或 Claude Desktop 配置文件中添加一个**本地 stdio 进程**，指向下载好的桥接脚本：
   ```json
   "mcpServers": {
     "guling-trader-local": {
       "command": "python3",
       "args": [
         "/Users/您的用户名/guling-trader/src/trader/mcp_local_relay.py",
         "--port", "8080"
       ]
     }
   }
   ```
   *此时，Mac 上的 Agent 启动，`mcp_local_relay.py` 会在 Mac 本地（或 Tailscale 虚拟网卡上）开启 8080 端口的 WebSocket 监听，等待 Windows 接入。*

### 第三步：配置 Windows 客户端主动连入 Mac
1. 打开 Windows 虚拟机或局域网机器上的 `guling-trader.exe` 的 `config.json` 配置文件。
2. 将服务连接终点修改为**运行 Agent 的 Mac 宿主机 IP 地址**（若是 Tailscale，填入 Mac 的 Tailscale IP）：
   ```json
   {
     "ws_endpoint": "ws://100.82.115.55:8080"
   }
   ```

3. 启动 `guling-trader.exe`，Windows 客户端会主动发起 TCP 连接，连入 Mac 宿主机的 `8080` 端口。
4. 两端通过局域网/虚拟机桥接网络直接接通，无任何第三方中转，100% 本地运行！

---

## 三、 Mac 端 `mcp_local_relay.py` 极简实现原理

这个脚本作为本地 Agent 的 stdio 服务端，同时在后台维护一个轻量级的 `websockets` 异步服务器：

```python
# -*- coding: utf-8 -*-
"""mcp_local_relay.py: 本地极客用户专属，Mac 本地 stdio 桥接到局域网 Windows 客户端"""
import asyncio
import json
import sys
import logging
from websockets.asyncio.server import serve

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("local_relay")

# 保存唯一的 Windows 客户端连接
active_trader_ws = None
rpc_futures = {}

async def handler(websocket):
    """处理来自局域网 Windows 端的 WSS 连接"""
    global active_trader_ws
    active_trader_ws = websocket
    logger.info("✓ 局域网 Windows 交易端已成功连入 Mac 桥接器")
    
    try:
        async for message in websocket:
            frame = json.loads(message)
            # 如果是 RPC reply，解析并填入对应 future
            if frame.get("type") == "reply":
                call_id = frame.get("id")
                if call_id in rpc_futures:
                    rpc_futures[call_id].set_result(frame)
    except Exception as e:
        logger.error(f"Windows 端连接中断: {e}")
    finally:
        active_trader_ws = None

async def read_stdin():
    """读取 Mac 本地 Agent 发来的 stdio MCP 请求"""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        
        request = json.loads(line)
        
        # 将标准 MCP stdio JSON-RPC 转换为局域网 RPC 帧
        if active_trader_ws:
            call_id = request.get("id")
            # 注册 future 等待 Windows 回应
            future = asyncio.Future()
            rpc_futures[call_id] = future
            
            # 转发给 Windows
            await active_trader_ws.send(json.dumps({
                "type": "call",
                "id": call_id,
                "method": request.get("method"),
                "params": request.get("params", {})
            }))
            
            # 等待 Windows 响应并写回 stdio stdout
            response = await future
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            rpc_futures.pop(call_id, None)
        else:
            logger.error("⚠ 交易尚未就绪，Windows 客户端未连接")

async def main():
    # 在 Mac 本地 8080 端口启动 WS 服务，等待 Windows 连入
    async with serve(handler, "0.0.0.0", 8080) as server:
        await read_stdin()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 四、 这一局域网方案的完美闭环特性

1. **绝对符合您的商业利益**：
   * 官方 `guling.pro`：**体验极简**，全网址 SSE 零下载绑定，适合 95% 的普通付费/核心订阅用户。
   * 本地直连模式：**配置复杂**，需要用户自己配置 Mac Python 环境、执行本地 python 桥接脚本、手动配置虚拟机端口和 IP。
   * **差异化极其鲜明**，既为硬核极客留出了完全离线的开源通道，又完全不透支您的商业网关壁垒。
2. **极佳的安全设计（零暴露）**：
   * Windows 本身不运行任何网络监听服务，处于完全封闭状态，不承担任何被外界入侵的风险。
   * **连接由 Windows 发起，由 Mac 端进行接收**，局域网环境下体验稳定，安全可控。
3. **实现成本极低**：
   * 这种模式下，您根本不需要改动 `guling-trader.exe` 的核心代码逻辑，只需在其配置文件中支持修改 `ws_endpoint` 为用户 Mac 的局域网 IP 即可！
