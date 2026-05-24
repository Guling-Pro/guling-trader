# 桌面 Co-pilot 与浏览器 RPA 扩展技术规格书 (Desktop RPA Co-pilot Suite Specification)

> **Status**: SPECIFICATION / PROPOSAL  
> **Target Audience**: 开源社区开发者、量化交易开发者  
> **Compatibility**: 任何兼容 MCP (Model Context Protocol) 协议的 AI 客户端 (Cursor, Claude Desktop, openclaw 等)

---

## 1. 概述与背景

`guling-trader` 作为一个开源的 Windows 桌面交易执行端，核心能力是通过本地模拟操作对接同花顺 `xiadan.exe`。然而，在量化实盘或 AI Agent 交易中，**交易执行**与**社交分享/实盘复盘**往往是强相关的。

本技术规格书定义了 `guling-trader` 的 **Desktop Co-pilot (桌面 RPA 扩展包)** 规格。该模块旨在通过本地浏览器接管技术，允许 AI Agent 借助标准 MCP 协议，在用户**已手动登录**的本地浏览器网页（如雪球网）中，安全地进行发帖、同步实盘记录等 RPA 动作。

---

## 2. 解耦设计：三层架构

本设计完全与特定的云端大脑（如 `yu-agent`）或特定的聊天网关（如微信）**解耦**。任何第三方 MCP 客户端或本地 Python 脚本，都可以直接调用本模块提供的工具。

```
+-------------------------------------------------------+
|                 MCP Host / AI Agent                   |
|      (Claude Desktop / Cursor / Local Python Script)   |
+-------------------------------------------------------+
                           |  标准 MCP 协议 (stdio / ws)
                           v
+-------------------------------------------------------+
|                    guling-trader                      |
|         (Windows Daemon / 本地 MCP 服务器)              |
|  +-------------------------------------------------+  |
|  |             RPA Co-pilot Suite (本模块)         |  |
|  +-------------------------------------------------+  |
+-------------------------------------------------------+
                           |  CDP (端口 9222) / Win32 OS 键鼠模拟
                           v
+-------------------------------------------------------+
|                    本地 Web 浏览器                    |
|          (Chrome / Edge - 用户已手动登录雪球)           |
+-------------------------------------------------------+
```

---

## 3. 标准 MCP 工具定义 (Tool Schemas)

启用本扩展后，`guling-trader` 将在标准的 MCP 接口中额外暴露以下工具。完整 JSON Schema 定义如下：

### 3.1 `xueqiu_publish_review` (发布雪球复盘/调仓记录)

用于在已登录的雪球网页中发布文本状态。

```json
{
  "name": "xueqiu_publish_review",
  "description": "通过本地已登录的浏览器，在雪球网发布实盘复盘、调仓日志或运营推文（安全拟真，免账号密码）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "content": {
        "type": "string",
        "description": "发布的内容文案。支持包含 $个股名称(代码)$ 格式的雪球 Hashtag 以蹭讨论区流量。"
      },
      "semi_manual": {
        "type": "boolean",
        "default": true,
        "description": "是否开启半人工模式。为 true 时，文案填入后将停留在‘发布’按钮并弹窗提示用户，等待用户手动点击发布；为 false 时自动点击发布。"
      }
    },
    "required": ["content"]
  }
}
```

---

## 4. 技术实现规格 (Technical Implementation)

为了兼顾**绝对安全**与**防爬风控规避**，本规格书提供两种物理级拟真 RPA 路径：

### 路径 A：CDP (Chrome DevTools Protocol) 协议接管（首选方案）

利用 Chromium 浏览器原生的开发者调试接口，对 DOM 树进行高精度操控。由于 **Microsoft Edge** 与 **Google Chrome** 均采用 **Chromium** 内核，它们共享完全相同的 CDP 接口规范。本工具对两者进行天然兼容。

#### 1. 继承主浏览器登录态 (Zero Separate Logins)
为了彻底避免“每次调试都要在独立沙箱里重新登录（输入验证码/扫码）”的繁琐体验，`guling-trader` **直接连接和操控用户日常正在使用的主浏览器**。
* **无沙箱隔离**：直接共享用户日常的 Session、Cookie 和登录状态。用户只要平时在浏览器里正常登录了雪球网，`guling-trader` 即可无缝继承该登录态，**完全不需要进行任何二次登录**。

#### 2. 最优开发/调试实践：快捷方式添加调试参数
为了让运行的主浏览器能被探测到，用户仅需在日常使用时开启调试接口（一劳永逸）：
1. 右键点击日常使用的 **Edge**（或 **Chrome**）桌面快捷方式，选择 **“属性”**。
2. 在 **“目标” (Target)** 输入框末尾追加：` --remote-debugging-port=9222`（注意前面有个空格）。
3. 确定保存。此后日常双击该快捷方式上网即可自动开启调试端口，完全不影响您的任何上网体验，但 Agent 调试时可以瞬间无缝对接。

#### 3. 浏览器关闭状态的自动唤醒机制 (Auto-Wakeup to Default Profile)
若 MCP 调用发生时，本地调试端口 `9222` 未处于监听状态（即浏览器已被关闭）：
`guling-trader` 会自动扫描 Windows 系统中的默认路径寻找浏览器，以**继承默认配置窗口**的方式拉起您的日常主浏览器，实现 100% 免二次登录：

* **Edge (Windows 默认预装，推荐)**: `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`
* **Chrome**: `C:\Program Files\Google\Chrome\Application\chrome.exe`

**自启动运行命令行**：
```cmd
msedge.exe --remote-debugging-port=9222 --new-window https://xueqiu.com
```

*(注：由于没有指定隔离的 `--user-data-dir`，这会直接打开您的默认主浏览器窗口，并自动恢复您日常的全部登录态。)*
2. **Session 共享**：
   用户在此浏览器窗口中正常手动登录雪球网。该调试窗口共享用户的完整登录 Session 和 Cookie，`guling-trader` 不需要也不接触任何账号密码。
3. **DOM 操作逻辑**：
   * 本地通过 Websocket 连接 `http://127.0.0.1:9222/json` 获取活动 Tab 列表。
   * 锁定 URL 匹配 `xueqiu.com` 的标签页（若无则自动新建标签页导航至 `https://xueqiu.com`）。
   * 注入 JS 脚本定位发布框 DOM 节点：
     ```javascript
     document.querySelector('.Home_post_textarea__xxx').value = "Content";
     ```
   * 触发发布按钮的 `click()` 事件（或在半人工模式下，高亮该按钮引导用户确认）。

### 路径 B：Win32 OS 句柄与模拟粘贴（Fallback 备用方案）

当浏览器未开启调试端口时，退化为系统级物理按键模拟，以实现 100% 拟真。

1. **窗口定位**：
   通过 Win32 API 寻找类名为 `Chrome_WidgetWin_1` 且标题包含 `雪球` 的窗口句柄，使用 `SetForegroundWindow` 将其唤醒并置顶。
2. **物理焦点激活**：
   使用快捷键或基于 OCR 控件坐标定位，双击雪球首页的发帖输入框。
3. **安全粘贴与发布**：
   * 将待发文案写入 Windows 系统剪贴板（Clipboard）。
   * 向该窗口句柄发送 `Ctrl + V` 按键序列。
   * 发送 `Tab` 或 `Enter`（或基于图像识别定位发布按钮进行点击）。

---

## 5. 开源社区本地沙盒验证 (Smoke Test / Stand-alone Dev)

社区开发者可以在不依赖任何云端控制台的情况下，通过本地 Python 环境独立调试此 RPA 能力：

1. **环境准备**：
   确保本地已安装 Python 3.11+ 并安装开发依赖：
   ```bash
   pip install pywin32
   ```
2. **测试脚本 `test_rpa.py`**：
   在本地创建以下文件，运行即可自验“半人工发帖”动作：

```python
import time
import win32gui
import win32con
import ctypes

def focus_xueqiu_browser():
    def callback(hwnd, extra):
        title = win32gui.GetWindowText(hwnd)
        classname = win32gui.GetClassName(hwnd)
        if "雪球" in title and "Chrome" in classname:
            extra.append(hwnd)
        return True
    
    hwnds = []
    win32gui.EnumWindows(callback, hwnds)
    if not hwnds:
        print("[-] 未在桌面上找到已打开雪球网的 Chrome 浏览器！")
        return None
    
    hwnd = hwnds[0]
    # 唤醒并置顶
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    print(f"[+] 成功定位并置顶雪球浏览器窗口，句柄: {hwnd}")
    return hwnd

if __name__ == "__main__":
    print("[*] 启动本地 RPA 独立测试...")
    hwnd = focus_xueqiu_browser()
    if hwnd:
        time.sleep(1)
        # 此处可以继续注入模拟粘贴或 CDP 操作
        print("[+] 沙盒测试完成！")
```

3. **运行测试**：
   在浏览器打开雪球网首页，然后运行 `python test_rpa.py`，观察浏览器是否被成功自动置顶激活。
