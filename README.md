# 📈 guling-trader (股灵交易助手)

[![GPL-3.0 License](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](https://github.com/suny911/guling-trader/blob/main/LICENSE)
[![Python Version](https://img.shields.io/badge/Python-3.11+-brightgreen.svg)](https://python.org)
[![OS Support](https://img.shields.io/badge/OS-Windows-blue.svg)](#)
[![Virtual Machine](https://img.shields.io/badge/Mac--VM-Parallels%20Compatible-green.svg)](#)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Compatible-orange.svg)](#)
[![Latest Release](https://img.shields.io/github/v/release/suny911/guling-trader?label=Release&color=success)](https://github.com/suny911/guling-trader/releases/latest)
[![股灵 guling.pro](https://img.shields.io/badge/股灵-guling.pro-7C3AED.svg)](https://guling.pro)

> 📦 **[下载最新版 guling-trader.exe](https://github.com/suny911/guling-trader/releases/latest)**　|　🌐 **[股灵官网 guling.pro](https://guling.pro)**

**让 AI 直接帮你炒 A 股。** —— 同花顺 (THS) A 股实盘的 **MCP 交易 Skill**，把券商下单变成 **AI Agent** 可直接调用的工具，一套面向实盘的 **trading harness**。

> 🔑 关键词：同花顺 · THS · xiadan.exe · A股实盘 · MCP · AI Agent · Trading Skill · harness-trading · 自动交易

`guling-trader` 是跑在 Windows 上的**交易执行端**：它通过模拟键鼠自动控制你已登录的同花顺独立委托客户端（`xiadan.exe`），把买入、卖出、查持仓、查资金等操作，变成 AI 可以直接调用的标准 MCP 工具。它本身不是 AI、也不碰你的密码——只负责"听 AI 指令、去同花顺上点鼠标"。

接入后，你可以直接对 AI 说：

> "帮我对手价买入 100 股贵州茅台。"
>
> "看一下我现在持仓，盈亏怎么样？"
>
> "把没成交的招商银行买单撤了。"

---

## 🧭 我该走哪条路？

三条路殊途同归，按门槛从低到高选一条即可：

| 你的情况 | 路径 | 数据走向 | 难度 |
|---|---|---|---|
| 想最省事，且有 guling.pro 内测邀请 | [**A · 托管版**](#路径-a--gulingpro-托管版最简单) | 经 guling.pro 云 | ★ |
| 用 openclaw / Cursor / Claude 等任意 AI | [**B · 云端自助**](#路径-b--openclaw--任意-ai-客户端云端自助) | 经 mcp.guling.pro 云 | ★★ |
| 极客，要数据 100% 不出自己设备 | [**C · 本地直连**](#路径-c--本地直连stdio--tailscale--进阶预览--设计中) 🚧 | 仅你自己的设备 | ★★★ |

> 三条路的**第一步完全一样**：先把 Windows 交易端跑起来（下一节）。之后再按你选的路径继续。

---

## 第一步（所有路径通用）：配好 Windows 交易端

> 你需要一台 **7×24 运行的 Windows 机器**：物理机、云 VPS，或 Mac 上的虚拟机（强烈推荐 **Parallels Desktop**，极其稳定）。

1. **登录同花顺**：打开同花顺独立委托客户端（`xiadan.exe`），登录你的证券账户，**界面切到"旧版"风格**，停留在下单主页面（请勿最小化窗口）。
2. **运行交易助手**：从 [GitHub Releases](https://github.com/suny911/guling-trader/releases) 下载 `guling-trader.exe`（单文件免安装），双击运行。
   * 首次启动会**自动静默安装图形识别环境**（Tesseract OCR），全程无感。
   * 启动后屏幕会展示一个 **6 位数配对码**（5 分钟有效）。**记住这个码**，下一步要用。
3. **把屏幕缩放设为 100%**：Windows 的 DPI 缩放若是 125%/150%，可能导致助手点错位置。

✅ 现在交易端已在线、等待配对。往下选你的路径。

---

## 路径 A · guling.pro 托管版（最简单）

> ⚠️ guling.pro 目前**内测中，需邀请码**。请 **私信作者** 获取邀请资格。

拿到邀请、登录 guling.pro 之后，把 Windows 屏幕上的 **6 位配对码**连同一句话发给 guling.pro 的助手，例如：

> "帮我绑定交易助手，配对码是 482-739。"

绑定成功即可直接用自然语言交易。**你不需要自己配置任何 MCP 客户端**——托管助手已经接好了一切。

---

## 路径 B · openclaw / 任意 AI 客户端（云端自助）

适用于 openclaw、Cursor、Claude Desktop 等任何支持 MCP 的 AI。你几乎不用动手——把活交给 AI：

把下面这个网址连同一句话发给你的 AI 助手：

> `https://mcp.guling.pro`
>
> "照这个文档帮我接入股灵交易。"

AI 会自动抓取该网址里的安装向导，一步步带你完成：**用 6 位码换永久凭证 → 把 MCP 服务器挂到你的客户端 → 跑通验证**。这个网址就是唯一入口和权威步骤来源，README 不再重复细节（以免过时）。

> **背后原理（一句话）**：向 `https://mcp.guling.pro/pair` 用 6 位码换一个永久 `agent_token`，再带请求头 `Authorization: Bearer <token>` 把 MCP 服务器 `https://mcp.guling.pro` 挂上，即已配对。
>
> **为什么要经过 `mcp.guling.pro`？** 你家里的 Windows 交易端和你的 AI 客户端通常各自在内网，彼此找不到对方——需要一台**有公网地址的服务器**当"汇合中转点"。自己买公网服务器对多数人太麻烦，所以 **guling.pro 免费提供了这条中转隧道**（`mcp.guling.pro`）当现成的汇合点，方便大家直接用：它只**加密转发**你的鼠标键盘操作指令，让你免去自建公网服务器，开箱即用、安全又简单。
>
> 如果连这条公益隧道也不想经过、要数据 **100% 不出自己设备**，请走下面的 [路径 C](#路径-c--本地直连stdio--tailscale--进阶预览--设计中)。

---

## 路径 C · 本地直连（stdio + Tailscale）— 🚧 进阶预览 / 设计中

> **状态：设计已定，relay 代码尚未入库。** 本节为预览，落地后转正。完整设计见 [`docs/local_only_stdio_mcp_setup.md`](docs/local_only_stdio_mcp_setup.md)。

面向对隐私要求最高的用户：**交易数据全程不经过任何云服务器**。

* **拓扑**：Windows 交易端 ↔ 你的 Mac（通过 [Tailscale](https://tailscale.com/) 私有内网直连）；Mac 上的 AI（Cursor / Claude / openclaw）通过本地 **stdio** 与一个轻量 relay 进程通信，全程不出你的设备。
* **我们提供**：一个 `mcp/` 文件夹 + Python relay 脚本（`mcp_local_relay.py`），帮你在本地把 stdio ↔ WS 桥接起来。
* **你需要自己做**：安装并组好 Tailscale 网络；编辑交易端配置文件（正式 exe 在**与 `guling-trader.exe` 同级**的 `guling-trader-data\config.json`），把 `ws_endpoint` 填成你 Mac 的 Tailscale 地址即可——**只填域名或 IP[:端口]，无需写协议和路径**，例如：
  ```json
  { "ws_endpoint": "100.x.x.x:8080" }
  ```

### TODO（路径 C 落地清单）

- [ ] 新增 `mcp/` 目录与 `mcp_local_relay.py`（stdio ↔ 本地 WS 桥接）
- [ ] relay 的安装/运行说明，以及与 Cursor / Claude / openclaw 的 stdio 对接示例
- [ ] Tailscale 内网端到端联调，并把本节从"预览"转正

---

## 🔒 安全设计

* **🔑 不碰密码**：你在同花顺官方软件上自行登录，本助手仅模拟键鼠操作，不接触任何账号和交易密码。
* **🛡️ 纯主动连出**：不监听任何端口、不需要端口转发，像浏览器一样主动向外建立加密连接，防范外界入侵。
* **⏹️ 一键切断**：随时关闭同花顺或本助手，或右键托盘选择"解除配对"，即可彻底断开 AI 控制。

---

## ⚠️ 常见问题

<details>
<summary><b>验证码识别失败？</b></summary>

首次启动会自动安装 Tesseract OCR。若自动安装因网络问题失败，手动在 PowerShell（管理员）中执行：

```powershell
winget install UB-Mannheim.TesseractOCR
```

然后重启交易助手。

</details>

<details>
<summary><b>发了交易命令同花顺没反应？</b></summary>

请确认 Windows 的**屏幕 DPI 缩放比例为 100%**。125% 或 150% 的放大可能导致助手点错位置。

</details>

<details>
<summary><b>可以锁屏或最小化远程桌面窗口吗？</b></summary>

不可以。如果使用 RDP 远程桌面，关闭/最小化窗口会导致 Windows 停止屏幕渲染，助手无法截图和模拟点击。请保持远程桌面窗口处于打开状态。

</details>

<details>
<summary><b>AI 客户端重启后又要重新配对？</b></summary>

说明永久凭证没写进配置。确保 MCP 配置里带上了 `Authorization: Bearer <你的 agent_token>` 请求头（路径 B 的换码返回里会给到），而不是只在某次会话里临时生效。

</details>

---

<details>
<summary>💻 开发者附录 (Developer's Annex — 点击展开)</summary>

### MCP 工具接口

结构定义见 `docs/tools_schema.json`；配对成功后解锁全部 8 个交易工具：

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `balance` | 查询资金余额 | — |
| `position` | 获取持仓列表 | — |
| `orders_active` | 当日未成交委托 | — |
| `orders_filled` | 当日已成交记录 | — |
| `settlement` | 交割单 | `date_range`: 近一周/近一月/近三月/近一年 |
| `buy` | **买入（实盘）** | `stock_no`, `amount`, `price`(可选), `client_order_id`(可选) |
| `sell` | **卖出（实盘）** | `stock_no`, `amount`, `price`(可选), `client_order_id`(可选) |
| `cancel` | 撤销未成交单 | `entrust_no` |

> 未配对状态下仅暴露 `pair_with_code` 一个工具（聊天内配对的后备方式）；路径 B 推荐用 `/pair` 换码在前的方式。完整帧协议（握手 / call / reply / reject / 心跳）见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

### 本地编译与打包

```powershell
pip install -e .[build]
pyinstaller --onefile --windowed --name guling-trader -m trader
# 产物输出于 dist\guling-trader.exe
```

</details>

---

## 🤝 致谢与开源协议

* 基于 **GPL-3.0-or-later** 协议开源，继承上游 [crazyAttributor/ths-auto-trade](https://github.com/crazyAttributor/ths-auto-trade) 协议约束。
* 核心控制库 `src/trader/ths/win.py` 中对同花顺 `xiadan.exe` 经数万次试错摸索出的控件序列、句柄哈希及 OCR 偏移坐标，**全部完整保留并致以最高敬意**。

---

## ⚠️ 风险免责声明

**本软件仅供技术交流及模拟测试使用**，不构成任何投资建议。用户因配置不当、DPI 缩放偏移、网络延迟、大模型幻觉下单等导致的任何资产亏损，**作者及开源贡献者不承担任何责任**。实盘前请务必使用 `--diagnose` 诊断命令自验，并在小资金账户完成充分测试。
