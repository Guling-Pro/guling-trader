# -*- coding: utf-8 -*-

"""
Lightweight Chrome DevTools Protocol (CDP) Client
Exposes APIs to connect to a Chromium browser, execute JS expressions, and navigate tabs.
"""

import os
import sys
import json
import logging
import platform
import subprocess
import asyncio
import urllib.request
import urllib.parse
from typing import Any, Optional

logger = logging.getLogger(__name__)

def find_browser_executable() -> Optional[str]:
    """探测本地可用的 Chromium 浏览器（Edge / Chrome）路径"""
    sys_type = platform.system()
    if sys_type == "Windows":
        paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ]
        # 探测 Local AppData (用户级安装 Chrome)
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            paths.append(os.path.join(local_appdata, r"Google\Chrome\Application\chrome.exe"))
        
        for p in paths:
            if os.path.exists(p):
                return p
    elif sys_type == "Darwin":  # macOS 开发联调支持
        paths = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    return None


async def ensure_browser_debugging(port: int = 9222) -> bool:
    """确保本地浏览器在指定调试端口正常运行。如果关闭，尝试自动唤醒。"""
    url = f"http://127.0.0.1:{port}/json"
    
    # 1. 尝试探测端口是否已在线
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as res:
            if res.status == 200:
                logger.info("[CDP] 侦测到本地浏览器调试端口已在线 (Port: %d)", port)
                return True
    except Exception:
        pass

    # 2. 端口不在线，尝试探测本地可用的浏览器可执行文件
    logger.info("[CDP] 调试端口 %d 未响应，尝试拉起本地主浏览器...", port)
    browser_exe = find_browser_executable()
    if not browser_exe:
        raise RuntimeError("本地未检测到可用的 Edge 或 Chrome 浏览器安装路径，请手动安装后重试。")

    # 3. 以调试端口启动主默认浏览器配置
    cmd = [
        browser_exe,
        f"--remote-debugging-port={port}",
        "--new-window",
        "https://xueqiu.com"
    ]
    logger.info("[CDP] 执行自启动命令: %s", " ".join(cmd))
    
    # 静默异步拉起浏览器进程
    if platform.system() == "Windows":
        # Windows 特有：不显示 CMD 控制台窗口，且不阻塞当前进程
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 4. 轮询等待端口响应（最多等待 8 秒）
    for i in range(16):
        await asyncio.sleep(0.5)
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as res:
                if res.status == 200:
                    logger.info("[CDP] 浏览器调试窗口已成功自动唤醒！")
                    return True
        except Exception:
            pass
            
    raise RuntimeError(f"浏览器已拉起但未能成功在端口 {port} 开启调试，请确认端口未被占用。")


async def get_or_create_tab(target_url_keyword: str, default_nav_url: str, port: int = 9222) -> str:
    """寻找匹配关键字的标签页，若不存在则新建一个并返回其 webSocketDebuggerUrl"""
    await ensure_browser_debugging(port)
    
    url = f"http://127.0.0.1:{port}/json"
    
    # 1. 获取所有打开的 Tab 列表
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as res:
            res_body = res.read().decode('utf-8')
            tabs = json.loads(res_body)
    except Exception as e:
        raise RuntimeError(f"获取浏览器标签页列表失败: {e}")

    # 2. 检索匹配关键字的活动 Tab
    for t in tabs:
        t_type = t.get("type")
        t_url = t.get("url", "")
        t_ws = t.get("webSocketDebuggerUrl")
        if t_type == "page" and target_url_keyword in t_url and t_ws:
            logger.info("[CDP] 寻找到匹配的标签页: %s, URL: %s", t.get("title"), t_url)
            return t_ws

    # 3. 未找到匹配的页，直接通过 HTTP 接口命令浏览器新建一个 Tab
    logger.info("[CDP] 未寻找到包含 '%s' 的标签页，正在新建...", target_url_keyword)
    new_tab_url = f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote_plus(default_nav_url)}"
    try:
        req = urllib.request.Request(new_tab_url, method="PUT")
        with urllib.request.urlopen(req, timeout=3.0) as res:
            res_body = res.read().decode('utf-8')
            tab = json.loads(res_body)
            t_ws = tab.get("webSocketDebuggerUrl")
            if t_ws:
                # 给予页面半秒缓冲初始化时间
                await asyncio.sleep(0.5)
                return t_ws
            raise RuntimeError("新建标签页返回的数据中缺失调试 WebSocket 链接")
    except Exception as e:
        raise RuntimeError(f"向浏览器发送新建标签页指令失败: {e}")


class CdpConnection:
    """高内聚的 Chromium CDP 调试套接字连接"""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: Any = None
        self._next_id = 1

    async def connect(self) -> None:
        from websockets.asyncio.client import connect
        logger.info("[CDP] 正在连接 WebSocket 调试端口: %s", self.ws_url)
        self.ws = await connect(self.ws_url, ping_interval=None)

    async def close(self) -> None:
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def send_cmd(self, method: str, params: Optional[dict] = None) -> dict[str, Any]:
        """向浏览器发送 CDP 协议命令并阻塞等待其 ID 对应的响应"""
        if not self.ws:
            raise RuntimeError("CDP WebSocket 未建立连接")

        cmd_id = self._next_id
        self._next_id += 1
        payload = {
            "id": cmd_id,
            "method": method,
            "params": params or {}
        }
        
        await self.ws.send(json.dumps(payload, ensure_ascii=False))
        
        # 轮询响应流
        async for msg in self.ws:
            res = json.loads(msg)
            if res.get("id") == cmd_id:
                return res
        raise RuntimeError("CDP 通信已被关闭，未收到期望的命令回执")

    async def execute_js(self, expression: str) -> Any:
        """在页面上下文中注入并运行一段 JavaScript，返回其 JSON 值"""
        res = await self.send_cmd("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        
        # 检查 CDP 层错误
        if "error" in res:
            raise RuntimeError(f"CDP 协议级报错: {res.get('error')}")
            
        result = res.get("result", {})
        exception_details = result.get("exceptionDetails")
        if exception_details:
            exc_msg = exception_details.get("exception", {}).get("description", "JavaScript Exception")
            raise RuntimeError(f"页面内部运行 JS 报错: {exc_msg}")
            
        return result.get("result", {}).get("value")
