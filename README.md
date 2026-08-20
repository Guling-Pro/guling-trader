# guling-trader

[![AGPL-3.0 License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](https://github.com/Guling-Pro/guling-trader/blob/main/LICENSE)
[![Python Version](https://img.shields.io/badge/Python-3.11+-brightgreen.svg)](https://python.org)
[![OS Support](https://img.shields.io/badge/OS-Windows-blue.svg)](#)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-orange.svg)](#)
[![Latest Release](https://img.shields.io/github/v/release/Guling-Pro/guling-trader?label=Release&color=success)](https://github.com/Guling-Pro/guling-trader/releases/latest)
[![Stock Market](https://img.shields.io/badge/Market-A%20Stock-7C3AED.svg)](#)

> **[下载最新版](https://github.com/Guling-Pro/guling-trader/releases/latest/download/guling-trader.exe)** · **[官网 guling.pro](https://guling.pro)**

---

**让 Agent 交易像移动支付一样，成为每个普通投资者触手可及的基础能力。**

guling-trader 是一个跑在 Windows 上的开源交易执行客户端。它把你登录的同花顺独立委托客户端（xiadan.exe）接入任意 AI 助手（Claude / Cursor / openclaw 等），通过 MCP（Model Context Protocol）协议暴露 9 个交易工具——市价/限价买卖、查持仓、查资金、查委托/成交、查交割单、读自选股等——让 AI 可以直接帮你研究、盯盘、下单，而整个过程中你的账号密码始终不离开同花顺官方软件。

接入后，你可以对 AI 说：
> "帮我对手价买入 100 股贵州茅台。"  
> "看一下我现在持仓和盈亏。"  
> "把没成交的招商银行买单撤了。"

---

## 什么是 Agent 交易？

**Agent 交易**是指：一个 AI 助手直接连接你的真实交易账户，通过自然语言指令为你执行买卖操作。相比传统量化（编程+回测）或手工操作，Agent 交易跳过代码和复杂配置，让任何股民都能对着 AI 说一句话、立即下单。

guling-trader 与同花顺、MCP 三合一，是这一范式落地 A 股的开源典范。无论你是用 Claude 聊天、用 Cursor 写代码还是用 openclaw 分析，只要装了 guling-trader，AI 就能帮你研究、分析、生成交易建议——**最终交易决策与执行权始终在你手中**。

---

## 怎样才能让 AI 帮我交易 A 股？

两条路都能到达，按你的技术门槛选一条：

| 场景 | 路径 | 数据流向 | 难度 |
|------|------|--------|------|
| 最简单，已有 guling.pro 邀请码 | [**A · 托管版**](#路径-a--gulingpro-托管版) | 经 guling.pro 云 | ★ |
| 用 Claude / Cursor / openclaw 等任意 AI | [**B · 云端自助**](#路径-b--任意-ai-客户端通过-mcpgulingpro-自助接入) | 经 mcp.guling.pro 云 | ★★ |

> **第一步对所有路径相同**：先把 Windows 端跑起来。

---

## 第一步：启动 Windows 交易端（所有路径通用）

你需要一台**7×24 运行的 Windows 机器**（物理机、云 VPS，或 Mac 虚拟机如 Parallels Desktop）。

1. **登录同花顺**：打开同花顺独立委托客户端（`xiadan.exe`），用你的证券账户登录，停留在下单主页。**新版 / 旧版皮肤均可**——v0.5.0 起自动适配控件，无需再手动切"旧版"。请勿最小化。
   - **建议关闭下单确认弹窗**（更快更稳）：在 xiadan 的系统设置中把「委托前确认/下单确认提示」类选项关掉（不同券商版本措辞略有差异）。关不掉的弹窗（验证码、废单提示、风险警示等）不用管——助手会自动处理并把弹窗内容记录进回执。

2. **运行交易助手**：从 [GitHub Releases](https://github.com/Guling-Pro/guling-trader/releases/latest/download/guling-trader.exe) 下载 `guling-trader.exe`（单文件免安装），双击运行。
   - 首次启动会自动静默安装 Tesseract OCR（图形识别环境），无感进行。
   - 启动后屏幕会显示一个 **6 位数配对码**（如 `482-739`，5 分钟有效）。记住这个码，下一步用。

3. **调整屏幕缩放**：确保 Windows 显示设置里 DPI 缩放为 **100%**。125% 或 150% 可能导致助手点错位置。

✅ 现在交易端已在线、等待配对。选择你的路径继续。

---

## 路径 A · guling.pro 托管版

> ⚠️ 目前内测中，需邀请码。**私信获取邀请资格**。

登录 guling.pro 后，把你屏幕上的 **6 位配对码** 连同一句话发给托管助手：

> "帮我绑定交易助手，配对码是 482-739。"

绑定成功即可直接用自然语言交易。你不需要自己配置任何 MCP 客户端——托管助手已接好一切。

---

## 路径 B · 任意 AI 客户端通过 mcp.guling.pro 自助接入

适用于 Claude Desktop、Cursor、openclaw 以及任何支持 MCP 的 AI 客户端。

**把下面这句话和网址一起发给你的 AI 助手：**

> 网址：`https://mcp.guling.pro`  
> 你的话：「照这个文档帮我接入股灵交易。」

AI 会自动抓取该网址的安装向导，一步步完成配对和接入——**用 6 位码换永久凭证 → 挂载 MCP 服务器 → 验证通过**。这个网址是唯一入口和权威步骤来源。

**原理一句话**：向 `https://mcp.guling.pro/pair` 用 6 位码换一个永久 `agent_token`，再用 `Authorization: Bearer <token>` 请求头把 MCP 服务器挂上即可。

**为什么要经过 mcp.guling.pro？** 你家里的 Windows 端和 AI 客户端通常各自在内网，彼此找不到——需要一台公网服务器当"汇合点"。guling.pro 免费提供这条中转隧道，只加密转发你的指令，让你免去自建公网服务器的麻烦，开箱即用、安全又简单。

---

## 如何确保交易安全？

| 特性 | 说明 |
|------|------|
| 🔑 **不碰密码** | 你在同花顺官方软件自行登录；助手仅模拟键鼠操作，不接触任何账号密码。 |
| 🛡️ **纯主动连出** | 不监听任何端口、不需要端口转发；像浏览器一样主动向外建立加密连接，防范入侵。 |
| ⏹️ **一键切断** | 随时关闭同花顺或助手，或在右键托盘选"解除配对"，彻底断开 AI 控制。 |

---

## 常见问题

<details>
<summary><b>验证码识别失败？</b></summary>

首次启动自动安装 Tesseract OCR。若自动安装因网络问题失败，在 PowerShell（管理员）中手动执行：

```powershell
winget install UB-Mannheim.TesseractOCR
```

然后重启交易助手。

</details>

<details>
<summary><b>我在 macOS 或 Linux，怎样用 guling-trader？</b></summary>

guling-trader 交易端只支持 Windows。但你可以：

- **Mac + Parallels Desktop**（推荐）：在虚拟机内装 Windows，放在家里或办公室 24 小时开机。
- **Windows 云 VPS**：租一台阿里云、腾讯云等云服务器，天然 24/7、无需自备硬件。

AI 助手部分（Claude、Cursor 等）在任何系统都能跑；只需确保你的 Windows 交易端能保持常开。

</details>

<details>
<summary><b>发了交易命令，同花顺没反应？</b></summary>

请确认 Windows 的**屏幕 DPI 缩放为 100%**。125% 或 150% 的放大可能导致助手点错位置。

</details>

<details>
<summary><b>我在用远程桌面（RDP），能锁屏或最小化窗口吗？</b></summary>

不能。关闭或最小化 RDP 窗口会导致 Windows 停止屏幕渲染，助手无法截图和点击。请保持 RDP 窗口处于打开状态。

</details>

<details>
<summary><b>重启 AI 客户端后，需要重新配对吗？</b></summary>

不需要。说明你的 `agent_token` 已正确写进客户端配置（通过 `Authorization: Bearer` 请求头）。若每次重启都失效，检查配置文件里是否真的保存了完整的 token。

</details>

<details>
<summary><b>AI 说"Windows 交易端未在线"，怎么办？</b></summary>

1. 确认 `guling-trader.exe` 仍在运行（检查任务管理器或任务栏托盘）。
2. 确认同花顺仍处于登录状态、界面可见（未最小化）。
3. 如果用的是路径 B（云端自助），检查网络连接是否正常。
4. 重启 guling-trader.exe 后再试。

</details>

<details>
<summary><b>AI 发来下单指令，但半分钟都没反应，为什么？</b></summary>

通常是网络延迟或同花顺响应超时。检查：

1. 网络连接是否稳定。
2. 同花顺是否在卡顿（CPU 使用率过高、界面反应慢）。
3. 稍等片刻后重试，或重启交易助手。

如果持续超时，向 GitHub Issues 提交诊断日志（见下方开发者附录）。

</details>

---

## MCP 工具接口速查表

配对成功后解锁全部 9 个交易工具。完整 Schema 见 [`docs/tools_schema.json`](docs/tools_schema.json)。

| 工具 | 用途 | 关键参数 |
|------|------|---------|
| `balance` | 查询资金余额 | — |
| `position` | 获取持仓列表 | — |
| `orders_active` | 当日未成交委托 | — |
| `orders_filled` | 当日已成交记录 | — |
| `settlement` | 交割单查询 | `date_range`：近一周/近一月/近三月/近一年 |
| `watchlist` | 读同花顺自选股代码（新版）| —（顶部第一屏，按同花顺习惯最新在顶部）|
| `buy` | 买入（实盘） | `stock_no`, `amount`, `price`(不传=五档即成剩撤市价单/传=限价挂单), `client_order_id`(可选) |
| `sell` | 卖出（实盘） | `stock_no`, `amount`, `price`(不传=五档即成剩撤市价单/传=限价挂单), `client_order_id`(可选) |
| `cancel` | 撤销未成交单 | `entrust_no` |

> 未配对时仅暴露 `pair_with_code` 一个工具；完整帧协议（握手、call、reply、reject、心跳）见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

---

<details>
<summary>💻 开发者附录（点击展开）</summary>

### 本地编译与打包

```powershell
# 安装依赖（包括 build 额外包）
pip install -e .[build]

# 编译成单文件 exe
pyinstaller --onefile --windowed --name guling-trader -m trader

# 输出路径：dist\guling-trader.exe
```

### 诊断与调试

启动时添加 `--diagnose` 标志以输出详细日志（含屏幕截图、点击坐标、OCR 结果）：

```powershell
guling-trader.exe --diagnose
```

诊断日志保存在 `guling-trader-data\trader.log`；提交 Issues 时请附带相关日志。

### 项目结构

```
guling-trader/
├── src/trader/
│   ├── __init__.py          # 版本定义
│   ├── main.py              # 应用入口 & --diagnose 标志处理
│   ├── main_window.py       # 主 UI（tkinter）
│   ├── tray.py              # 系统托盘菜单
│   ├── bootstrap.py         # 启动引导
│   ├── brand.py             # 品牌/版本管理
│   ├── config.py            # 配置加载
│   ├── dispatcher.py        # 9 个交易工具定义（FALLBACK_TOOLS_SCHEMA）
│   ├── handshake.py         # WebSocket 握手逻辑
│   ├── ws_client.py         # WebSocket 客户端
│   ├── ui_dialogs.py        # UI 对话框
│   ├── installer/           # OCR 依赖自动安装
│   ├── ths/
│   │   ├── const.py         # 同花顺常量定义
│   │   └── win.py           # 同花顺 xiadan.exe 控制 + OCR 识别（核心）
│   └── ...
├── docs/
│   ├── PROTOCOL.md          # MCP 帧协议文档
│   ├── tools_schema.json    # 工具 Schema
│   ├── local_only_stdio_mcp_setup.md  # 本地私有化接入设计稿（暂未开放）
│   └── specs/               # 规格文档
├── pyproject.toml           # 项目元数据和依赖
├── LICENSE                  # GPL-3.0
└── guling-trader-data/      # 运行时数据目录
    └── trader.log           # 诊断日志输出位置
```

</details>

---

## 开源协议

基于 **AGPL-3.0-or-later** 协议开源——即使将本项目修改后作为网络服务（SaaS）对外提供，也须一并公开你的修改源码。

## 商标与品牌

代码以 AGPL-3.0 开源，但 **「股灵」「Guling」「guling.pro」名称与 logo 为商标**。基于本项目二次开发或分发时，请保留出处链接；**未经授权，不得以「股灵 / Guling」名义发布、不得冒用品牌**——衍生作品请使用你自己的名称。

---

## ⚠️ 风险免责

**本软件为开源工具，不构成任何投资建议、不提供交易保证**。用户因配置不当、DPI 缩放偏移、网络延迟、大模型幻觉下单等导致的任何资产亏损，**实盘风险完全由用户自行承担**；作者及开源贡献者不承担任何责任。

**实盘前必做**：
1. 使用 `--diagnose` 命令自验交易流程（确保屏幕识别、点击准确）。
2. 在小资金账户完成充分测试，确认工作无误后再用大资金。

---

## 名字的含义

「股」是 A 股，「灵」取自图灵（Alan Turing）——智能的源头。把图灵智能，带给每一个普通投资者。

---

## 相关资源

- **官网**：https://guling.pro
- **组织**：https://github.com/Guling-Pro
- **MCP 接入指南**：https://mcp.guling.pro
- **问题反馈**：[GitHub Issues](https://github.com/Guling-Pro/guling-trader/issues)

---

<div align="right">

**Guling Pro · 股灵**  
开源、诚实、为每个投资者服务

</div>
