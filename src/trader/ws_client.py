"""WebSocket 客户端：连接、状态机、指数退避重连"""
import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, TYPE_CHECKING

from websockets.asyncio.client import connect

# PyInstaller --onefile 不会自动用 Windows 系统证书 store；Python ssl 模块默认查
# certifi 包提供的 cacert.pem 来验 wss 证书。这里显式构造 SSL context 指向 certifi
# 的 CA bundle，绕开 "unable to get local issuer certificate" 错误。
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # certifi 没装时 fallback 到系统默认 — 在某些环境（含 macOS dev）够用
    _SSL_CONTEXT = ssl.create_default_context()

from . import config, dispatcher, handshake
from .ths.win import WinThsBackend

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

WS_ENDPOINT = "wss://api.guling.pro/api/trader-tunnel"
WS_ENDPOINT_DEV = "ws://localhost:8000/api/trader-tunnel"


class ConnectionState(str, Enum):
    """交易员端连接状态"""

    UNPAIRED = "UNPAIRED"
    DIALING = "DIALING"
    AWAITING_BIND = "AWAITING_BIND"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class PendingRPC:
    """待处理的 RPC 调用"""

    method: str
    params: dict[str, Any]
    future: asyncio.Future


def _format_rpc_log(
    method: str, params: dict, result: Any = None, error: str = None
) -> str:
    """格式化单行 Agent 操作日志: [Agent] method args → result/✗ error"""
    if method in ("buy", "sell"):
        stock = params.get("stock_no", "?")
        amount = params.get("amount", "?")
        price = params.get("price")
        args = f"{stock}×{amount}" + (f"@{price}" if price is not None else "")
    elif method == "cancel":
        args = str(params.get("entrust_no", "?"))
    else:
        args = ""

    prefix = f"[Agent] {method}" + (f" {args}" if args else "")

    if error:
        return f"{prefix} → ✗ {error}"

    if result is None:
        return prefix

    if isinstance(result, dict) and result.get("code") == 0:
        if method == "balance":
            avail = result.get("available_cash") or result.get("available")
            tail = f"可用 {avail}" if avail is not None else "OK"
        elif method in ("buy", "sell"):
            oid = result.get("entrust_no") or result.get("order_id")
            tail = f"委托号 {oid}" if oid else "OK"
        elif method == "cancel":
            tail = "已撤单"
        else:
            tail = "OK"
    else:
        tail = "OK"

    return f"{prefix} → {tail}"


class WsClient:
    """WebSocket 客户端管理"""

    def __init__(
        self,
        dev_url: Optional[str] = None,
        on_state_change: Optional[Callable[[ConnectionState], None]] = None,
        on_pair_pending: Optional[Callable[[str, Any], None]] = None,
        on_rpc_log: Optional[Callable[[str], None]] = None,
        backend: Optional[WinThsBackend] = None,
    ):
        self.endpoint = dev_url or WS_ENDPOINT
        self.state = ConnectionState.UNPAIRED
        self.ws: Optional[Any] = None
        self.pending_rpcs: dict[str, PendingRPC] = {}
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.on_pair_pending = on_pair_pending
        self.on_state_change = on_state_change
        self.on_rpc_log = on_rpc_log
        self.backend = backend or WinThsBackend()

    def _set_state(self, new_state: ConnectionState) -> None:
        """更新状态并触发回调"""
        if self.state != new_state:
            self.state = new_state
            if self.on_state_change:
                self.on_state_change(new_state)

    async def run(self) -> None:
        """主循环：连接 → 握手 → 消息处理 → 重连"""
        while True:
            # 每次重连都重新读 config——支持运行期清 config 触发 pair_init
            cfg = config.load()
            try:
                self._set_state(ConnectionState.DIALING)
                logger.info("正在连接到 %s...", self.endpoint)

                async with connect(
                    self.endpoint,
                    ping_interval=15,
                    ping_timeout=30,
                    ssl=_SSL_CONTEXT if self.endpoint.startswith("wss://") else None,
                ) as ws:  # type: ignore
                    self.ws = ws
                    self.reconnect_delay = 1.0
                    logger.info("已连接到服务器")

                    # 记录握手前是否已配对——决定握手成功后的状态
                    was_paired = cfg.has_paired()
                    result = await handshake.perform_handshake(ws, cfg)

                    if not result.success:
                        logger.error("握手失败：%s", result.error)
                        self._set_state(ConnectionState.UNPAIRED)
                        if result.should_clear_config:
                            config.TraderConfig(device_id=cfg.device_id)
                            config.save(config.TraderConfig(device_id=cfg.device_id))
                        await asyncio.sleep(30)
                        continue

                    if result.reason == "evicted_by_other_session":
                        logger.warning("被其他设备会话踢出，冷却 30 秒...")
                        await asyncio.sleep(30)
                        continue

                    # pair_init 成功 → 收到 pair_pending，等 bind_ok 才 CONNECTED
                    # resume 成功 → 收到 welcome，直接 CONNECTED
                    if was_paired:
                        self._set_state(ConnectionState.CONNECTED)
                    else:
                        self._set_state(ConnectionState.AWAITING_BIND)
                        # 把 pair_pending 的 code/expires_at 推给上层（main_window）
                        if result.pair_pending and self.on_pair_pending:
                            self.on_pair_pending(
                                result.pair_pending.get("code"),
                                result.pair_pending.get("expires_at"),
                            )
                    logger.info("握手成功，状态：%s", self.state)

                    await self._main_loop(ws)

            except asyncio.CancelledError:
                logger.info("WS 客户端已取消")
                break
            except Exception as e:
                logger.error("连接出错：%s，%d 秒后重连...", e, self.reconnect_delay)
                self._set_state(ConnectionState.DISCONNECTED)
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(
                    self.reconnect_delay * 2, self.max_reconnect_delay
                )

    async def _main_loop(self, ws: "ClientConnection") -> None:  # type: ignore
        """主消息处理循环"""
        try:
            async for raw_msg in ws:
                try:
                    frame = json.loads(raw_msg)
                    await self._handle_frame(frame)
                except Exception as e:
                    logger.error("处理帧出错：%s，原始数据：%s", e, raw_msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("主循环出错：%s", e)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """处理接收到的帧"""
        frame_type = frame.get("type")

        if frame_type == "pair_pending":
            self._set_state(ConnectionState.AWAITING_BIND)
            code = frame.get("code")
            expires_at = frame.get("expires_at")
            logger.info("配对码已生成：%s", code)
            # 新码到来时，通知上层更新 pairing_code + expires_at + 清除 refreshing
            if self.on_pair_pending:
                self.on_pair_pending(code, expires_at)

        elif frame_type == "bind_ok":
            # 关键持久化：bind_ok 帧里的 agent_token plaintext 是唯一一次出现，
            # 必须立刻保存到本地 config.json，否则 trader 重启就丢，下次启动
            # has_paired() = False → pair_init → server reject "device_already_paired"
            # → 死循环。
            account_name = frame.get("account_name")
            agent_token = frame.get("agent_token")
            session_id = frame.get("session_id")
            logger.info("配对成功 account=%s session=%s", account_name, session_id)

            try:
                from datetime import datetime
                cfg = config.load()
                cfg.agent_token = agent_token
                cfg.account_name = account_name
                cfg.paired_at = datetime.now().isoformat()
                config.save(cfg)
                logger.info("✓ 已持久化 agent_token 到 config.json")
            except Exception as e:
                logger.exception("⚠ 保存 agent_token 到 config 失败：%s", e)

            self._set_state(ConnectionState.CONNECTED)

        elif frame_type == "welcome":
            logger.info("欢迎消息：%s", frame)
            self._set_state(ConnectionState.CONNECTED)

        elif frame_type == "reject":
            reason = frame.get("reason")
            logger.warning("握手被拒绝：%s", reason)

        elif frame_type == "call":
            rpc_id = frame.get("id")
            method = frame.get("method")
            params = frame.get("params", {})
            logger.info("收到 RPC call：id=%s, method=%s", rpc_id, method)
            try:
                # dispatcher.handle_call 已返回完整 reply 帧（type/id/ok/result|error）。
                # 直接转发，不要再包一层 {ok:true, result:...}——否则外层永远 ok:true，
                # 真实失败被掩盖，成功结果也多嵌一层导致下游解析错位。
                reply = await dispatcher.handle_call(frame, self.backend)
                if self.on_rpc_log:
                    if reply.get("ok"):
                        self.on_rpc_log(
                            _format_rpc_log(method, params, result=reply.get("result"))
                        )
                    else:
                        self.on_rpc_log(
                            _format_rpc_log(method, params, error=reply.get("error"))
                        )
            except Exception as e:
                reply = {"type": "reply", "id": rpc_id, "ok": False, "error": str(e)}
                if self.on_rpc_log:
                    self.on_rpc_log(_format_rpc_log(method, params, error=str(e)))
            if self.ws:
                await self.ws.send(json.dumps(reply, ensure_ascii=False))

    async def send_frame(self, frame: dict[str, Any]) -> None:
        """发送帧"""
        if not self.ws:
            logger.warning("WebSocket 未连接，无法发送帧")
            return
        try:
            await self.ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            logger.error("发送帧出错：%s", e)

    def is_connected(self) -> bool:
        """是否已连接"""
        return self.state == ConnectionState.CONNECTED
