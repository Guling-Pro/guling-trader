# guling-trader

Windows 桌面客户端：通过 WS 隧道连接 [guling.pro](https://guling.pro) 后端 agent，驱动同花顺独立委托客户端 `xiadan.exe` 执行 A 股交易。

GPL-3.0 · [Releases](https://github.com/suny911/guling-trader/releases) · [Issues](https://github.com/suny911/guling-trader/issues)

---

## 架构

trader 出站 wss 连 `guling.pro/api/trader-tunnel`，认证后服务器侧的 LLM tool 调用通过这根隧道下行。**trader 端无任何监听端口**——不需要公网入站、不需要端口转发。

```
Windows / wine 任意主机                     guling.pro (yu-agent server)
┌─────────────────────────────────┐         ┌──────────────────────────────────┐
│ xiadan.exe（已登录同花顺）        │         │ FastAPI                          │
│  ↑ Win32 / pywin32              │         │  /api/trader-tunnel  (WS)         │
│ ths/win.py                      │         │    ↓                              │
│  ↑                              │         │  ConnectionRegistry              │
│ dispatcher / ws_client          │ ◀ wss ▶ │  (user, account) → ws            │
│  ↑                              │         │    ↓                              │
│ tray (pystray) / 配对码弹窗      │         │  trade_ths tool handlers          │
│                                 │         │    ↓                              │
│ guling-trader.exe (PyInstaller) │         │  LLM agent                        │
└─────────────────────────────────┘         └──────────────────────────────────┘
```

---

## 用户安装与配对

### 1. 准备环境（Windows 真机或 wine）

```powershell
winget install Python.Python.3.11
winget install UB-Mannheim.TesseractOCR
```

mac mini / Linux 用户：装 [CrossOver](https://www.codeweavers.com/crossover) 或 wine，在 wine prefix 里完成上面两步。**不保证所有 wine 配置都能跑 xiadan.exe**——用 `--diagnose` 模式自验（下方）。

### 2. 下载 guling-trader.exe

从 [GitHub Releases](https://github.com/suny911/guling-trader/releases) 拿最新版本（每个 tag 自动 build）。

或本地编译：

```powershell
git clone https://github.com/suny911/guling-trader.git
cd guling-trader
pip install -e .[build]
pyinstaller --onefile --windowed --name guling-trader -m trader
# 产物：dist\guling-trader.exe
```

### 3. 登录同花顺

启动 xiadan.exe → 登录你的券商账户 → 切换到「旧版」交易客户端（控件 ID 是按旧版逆向的）→ 停在主页。

### 4. 启动 trader 并配对

```powershell
.\guling-trader.exe
```

托盘右下角出现 guling-trader 图标，状态色：
- 灰 = UNPAIRED / DISCONNECTED
- 黄 = DIALING / AWAITING_BIND
- 绿 = CONNECTED
- 红 = fatal（token 被 reject / account 被远程 remove / xiadan 进程异常）

**首次配对**：

1. 右键 tray → 「配对码...」→ 弹窗显示 6 位码 `XXX-YYY` + 5 分钟倒计时 + 一键复制
2. 复制码到 [guling.pro](https://guling.pro) 对话窗，告诉 agent：
   ```
   加交易终端 482-739
   或：加交易终端 482-739，名字叫 主账户
   ```
3. agent 调 `add_trading_account` tool → 服务器派 agent_token → trader 转绿
4. 之后任何调下单 / 查询 tool（`ths_buy / ths_balance / ths_position` 等）都通过这台 trader

**重启**：trader 重启后自动用持久化的 `agent_token` resume，**不再需要重新配对**。

### 5. 验证

guling.pro 对话窗问 agent：
```
列出我的交易账户
查我账户1的余额
```

应能看到账户在线状态 + 余额数字。

---

## --diagnose 模式（wine 兼容性自验）

```powershell
.\guling-trader.exe --diagnose
```

不连服务器，只跑本地 Win32 检测：

- ✓/✗ xiadan.exe 窗口是否找到
- ✓/✗ tesseract 是否可调
- ✓/✗ balance() 是否能返回数据（验证基础 Win32 控件可达）
- ✓/✗ position() 是否能返回数据

常见问题：
- xiadan.exe 找不到 → 确认登录 + 切到「旧版」 + 主页面 active
- tesseract 找不到 → `winget install UB-Mannheim.TesseractOCR` 或手动指定路径
- 控件 ID 不匹配 → 你不是申万宏源券商，需要对照 `ths/const.py` 调整

---

## 风险与限制

- **控件 ID 强绑定**：基于申万宏源 + 同花顺旧版 xiadan 验证。换券商 / 新版客户端会失效
- **OCR 弹窗坐标**：写死在 `ths/win.py`，DPI 必须 100%
- **RDP 锁屏 = 失效**：保持 RDP 窗口不最小化，或用 `tscon` 把 session 转 console
- **agent_token**：明文存在你机器的 `%APPDATA%\guling-trader\config.json`，机器被入侵即泄露
- **Mac 原生不支持**：trader 跑 mac 显示 tray 但 ths backend 抛 NotImplementedError。Mac 用户走 wine（CrossOver / wine prefix）

---

## 开发

```
src/trader/
├── main.py            # 启动入口 + argparse
├── config.py          # config.json 读写
├── bootstrap.py       # 首次启动：生成 device_id、找 xiadan/tesseract
├── ws_client.py       # WebSocket 客户端 + 状态机 + 重连
├── handshake.py       # pair_init / resume 握手
├── dispatcher.py      # 收 server call frame → backend method → 发 reply
├── tray.py            # pystray Icon + 菜单 + 状态色
├── ui_dialogs.py      # tkinter 配对码弹窗 / 状态窗
└── ths/
    ├── const.py       # 按键码表 + 控件 ID 常量
    └── win.py         # WinThsBackend：994 行 Win32 实装 + async wrapper
```

### CI

`.github/workflows/build.yml` — push tag `v*` 自动在 windows-latest runner 上 PyInstaller 打包 + 发 GitHub Release（含 sha256）。手动触发用 workflow_dispatch（仅产 artifact，不发 release）。

### 协议契约

trader ↔ server 信道协议（hello/pair_init/bind_ok/welcome/reject/call/reply/event 帧格式）定义在 server 侧的 [PH-061 spec](https://github.com/suny911/guling-trader/blob/main/docs/PROTOCOL.md)（待提取）。本仓只实现 client 端，server 端独立维护。

---

## License & 致谢

**GPL-3.0-or-later** — 与上游 [crazyAttributor/ths-auto-trade](https://github.com/crazyAttributor/ths-auto-trade) 保持一致（GPL 传染性）。

`src/trader/ths/win.py` 的 Win32 控件 ID、F1/F2/F8 热键序列、OCR 截图坐标全部来自上游——这些是作者对着申万宏源 xiadan.exe 试错试出来的经验数据，是这套代码真正的价值，我们没有重写也无意 reinvent。换券商可能需要调整控件 ID。
