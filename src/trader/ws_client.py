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

WS_ENDPOINT = "wss://mcp.guling.pro/api/trader-tunnel"
WS_ENDPOINT_DEV = "ws://localhost:8000/api/trader-tunnel"

_AUDIT_REDACTED = "<redacted>"
_AUDIT_SECRET_KEY_PARTS = (
    "token", "authorization", "password", "passwd", "secret", "captcha",
    "verification_code", "confirmation_token", "pairing_code", "credential",
    "api_key", "access_key", "private_key", "otp", "pin",
)


def _redact_for_audit(value: Any, key: str = "") -> Any:
    """返回可写入本地诊断日志的副本，绝不保留认证或验证码明文。"""
    normalized_key = str(key).lower().replace("-", "_")
    if any(part in normalized_key for part in _AUDIT_SECRET_KEY_PARTS):
        return _AUDIT_REDACTED
    if isinstance(value, dict):
        is_pair_pending = value.get("type") == "pair_pending"
        redacted = {}
        for child_key, child in value.items():
            child_key = str(child_key)
            if is_pair_pending and child_key == "code":
                redacted[child_key] = _AUDIT_REDACTED
            else:
                redacted[child_key] = _redact_for_audit(child, child_key)
        return redacted
    if isinstance(value, list):
        return [_redact_for_audit(child) for child in value]
    if isinstance(value, tuple):
        return [_redact_for_audit(child) for child in value]
    return value


def _audit_json(value: Any) -> str:
    """稳定的单行审计表示，既能检索也不会因未知对象中断业务。"""
    return json.dumps(
        _redact_for_audit(value), ensure_ascii=False, sort_keys=True, default=str,
    )


def _normalize_endpoint(value: Optional[str]) -> Optional[str]:
    """把用户填的"域名 / IP"补全成完整 WS 连接地址。

    用户只需填 ``mcp.guling.pro`` 或 ``192.168.1.10:8080``——无需关心 ``ws/wss``
    协议、``/api/trader-tunnel`` 内部路径，多打或漏打斜杠也无所谓。

    规则：域名 → ``wss://``，IP / localhost → ``ws://``（想强制协议就自带前缀）；
    路径一律重置为 ``/api/trader-tunnel``。
    """
    if not value:
        return None
    v = value.strip()
    if not v:
        return None

    # 1. 取出用户可能显式指定的协议
    scheme = ""
    if "://" in v:
        scheme, v = v.split("://", 1)
        scheme = scheme.lower()

    # 2. 只保留 host[:port]，丢掉用户多打的路径与斜杠
    host = v.split("/", 1)[0].strip().strip("/")
    if not host:
        return None

    # 3. 未显式指定协议时按 host 类型推断：IP / localhost 用 ws，域名用 wss
    if scheme not in ("ws", "wss"):
        bare_host = host.split(":", 1)[0]
        is_ip_or_local = bare_host == "localhost" or all(
            c.isdigit() or c == "." for c in bare_host
        )
        scheme = "ws" if is_ip_or_local else "wss"

    return f"{scheme}://{host}/api/trader-tunnel"


class SessionRejectedException(Exception):
    """会话运行中收到服务端的拒绝（如踢出、被限流锁死、Token失效）"""

    def __init__(self, reason: str):
        super().__init__(f"Session rejected: {reason}")
        self.reason = reason


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

    if isinstance(result, dict) and result.get("status") == "succeed":
        payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        if method == "balance":
            avail = payload.get("可用金额")
            tail = f"可用 {avail}" if avail is not None else "OK"
        elif method in ("buy", "sell"):
            oid = payload.get("entrust_no")
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
        self.dev_url = dev_url
        cfg = config.load()
        self.endpoint = (
            _normalize_endpoint(self.dev_url)
            or _normalize_endpoint(cfg.ws_endpoint)
            or WS_ENDPOINT
        )
        self.state = ConnectionState.UNPAIRED
        self.ws: Optional[Any] = None
        # 每次成功建立连接递增。主动事件写入期间若连接已经切换，旧连接上的
        # send 即使返回也不能视为当前会话投递成功，交给看门狗保留原帧重放。
        self._connection_generation = 0
        self.pending_rpcs: dict[str, PendingRPC] = {}
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.on_pair_pending = on_pair_pending
        self.on_state_change = on_state_change
        self.on_rpc_log = on_rpc_log
        self.backend = backend or WinThsBackend()
        # 在飞的 call 任务强引用（防 GC 提前回收），完成即自清。
        self._call_tasks: set[asyncio.Task] = set()

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
            self.endpoint = (
                _normalize_endpoint(self.dev_url)
                or _normalize_endpoint(cfg.ws_endpoint)
                or WS_ENDPOINT
            )
            try:
                self._set_state(ConnectionState.DIALING)
                logger.info("正在连接到 %s...", self.endpoint)

                async with connect(
                    self.endpoint,
                    ping_interval=30,
                    ping_timeout=60,
                    ssl=_SSL_CONTEXT if self.endpoint.startswith("wss://") else None,
                ) as ws:  # type: ignore
                    self.ws = ws
                    self._connection_generation += 1
                    try:
                        # 确认令牌只对生成它的连接代次有效，重连后不能继续授权旧快照。
                        dispatcher.clear_external_cancel_confirmations()
                        self.reconnect_delay = 1.0
                        logger.info("已连接到服务器")

                        # 记录握手前是否已配对——决定握手成功后的状态
                        was_paired = cfg.has_paired()
                        result = await handshake.perform_handshake(ws, cfg)

                        if not result.success:
                            logger.error("握手失败：%s", result.error)
                            self._set_state(ConnectionState.UNPAIRED)
                            if result.should_clear_config:
                                latest_cfg = config.load()
                                latest_cfg.agent_token = None
                                latest_cfg.account_name = None
                                latest_cfg.paired_at = None
                                config.save(latest_cfg)
                            # 握手失败（如网络、服务不可用）退避 5 秒重试
                            await asyncio.sleep(5)
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
                    finally:
                        # 不能留下已经退出上下文的 socket：否则看门狗会把它误当作可写。
                        if self.ws is ws:
                            self.ws = None
                        if self.state in (
                            ConnectionState.DIALING,
                            ConnectionState.AWAITING_BIND,
                            ConnectionState.CONNECTED,
                        ):
                            self._set_state(ConnectionState.DISCONNECTED)

                # 远端正常关闭不会抛异常；仍按退避重连，避免紧循环占满 CPU。
                if self.state == ConnectionState.DISCONNECTED:
                    logger.info("WebSocket 已关闭，%d 秒后重连...", self.reconnect_delay)
                    await asyncio.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(
                        self.reconnect_delay * 2, self.max_reconnect_delay
                    )

            except asyncio.CancelledError:
                logger.info("WS 客户端已取消")
                break
            except SessionRejectedException as e:
                reason = e.reason
                logger.error("会话连接运行中被拒绝（reason=%s），进入冷却...", reason)
                self._set_state(ConnectionState.DISCONNECTED)
                
                if reason == "evicted_by_other_session":
                    logger.warning("被其他设备会话踢出，冷却 30 秒...")
                    await asyncio.sleep(30)
                elif reason == "brute_force_blocked":
                    logger.warning("因配对码多次校验失败被锁定，冷却 60 秒...")
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(30)
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
                    await self._handle_frame(frame, ws)
                except SessionRejectedException:
                    raise
                except Exception as e:
                    # 原始文本可能含 bind_ok 的 agent_token；解析失败时只保留长度。
                    logger.exception("处理帧出错：%s（原始帧长度=%d）", e, len(raw_msg))
        except asyncio.CancelledError:
            raise
        except SessionRejectedException:
            raise
        except Exception as e:
            logger.error("主循环出错：%s", e)

    async def _handle_frame(self, frame: dict[str, Any], origin_ws: Any = None) -> None:
        """处理接收到的帧。origin_ws=收到该帧的那条连接（用于回执归属校验）。"""
        frame_type = frame.get("type")
        logger.info("[WS<-] received=%s", _audit_json(frame))

        if frame_type == "pair_pending":
            self._set_state(ConnectionState.AWAITING_BIND)
            code = frame.get("code")
            expires_at = frame.get("expires_at")
            logger.info("配对码已生成（已脱敏） expires_at=%s", expires_at)
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
            logger.info("欢迎消息已接收")
            self._set_state(ConnectionState.CONNECTED)

        elif frame_type == "reject":
            reason = frame.get("reason")
            logger.warning("会话连接被服务器拒绝（reason=%s）", reason)
            
            # 立即清理无效凭证，防止重连循环 (PROTOCOL.md §2)
            if reason in ("token_invalid", "account_removed"):
                cfg = config.load()
                logger.warning("Token 失效或账户已移除，清空本地 agent_token 配置...")
                cfg.agent_token = None
                cfg.account_name = None
                cfg.paired_at = None
                config.save(cfg)
                
            raise SessionRejectedException(reason)

        elif frame_type == "call":
            # 后台 task 执行：单笔 RPC 卡住/变慢时，消息循环必须继续跑——否则
            # 连用于核单的 orders_active/orders_filled 都进不来（2026-07-13 事故：
            # 一笔卡死瘫痪整个受控端）。执行顺序不受影响：交易/查询本就由
            # backend.win_lock（FIFO）串行。
            task = asyncio.create_task(self._process_call(frame, origin_ws))
            self._call_tasks.add(task)
            task.add_done_callback(self._call_tasks.discard)

    async def _process_call(self, frame: dict[str, Any], origin_ws: Any = None) -> None:
        """执行一个 call 帧并回发 reply（在独立 task 中运行）。

        origin_ws=收到该帧的连接。一笔 RPC 最长可跑 25s，其间完全可能断线重连；
        若发送时 self.ws 已换成新连接，这条回执就是**跨会话错投**——它的 id 属于
        旧会话，发到新连接上归属无从保证（能否配到别的请求头上取决于网关的 id
        策略，不能靠对端兜底）。宁可丢弃并留痕：调用方那边本就已超时。
        """
        rpc_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params", {})
        logger.info("[RPC] dispatch_begin id=%s method=%s", rpc_id, method)
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
            logger.exception("[RPC] dispatch_exception id=%s method=%s", rpc_id, method)
            reply = {"type": "reply", "id": rpc_id, "ok": False, "error": str(e)}
            if self.on_rpc_log:
                self.on_rpc_log(_format_rpc_log(method, params, error=str(e)))
        logger.info(
            "[WS->] reply_ready id=%s method=%s reply=%s",
            rpc_id, method, _audit_json(reply),
        )
        if origin_ws is not None and self.ws is not origin_ws:
            logger.warning(
                "丢弃跨连接回执：id=%s method=%s（执行期间已重连，回执归属无法保证）",
                rpc_id, method)
            return
        if not self.ws:
            logger.warning("[WS->] reply_not_written id=%s method=%s（连接不存在）", rpc_id, method)
            return
        try:
            await self.ws.send(json.dumps(reply, ensure_ascii=False))
        except Exception:
            logger.exception("[WS->] reply_write_failed id=%s method=%s", rpc_id, method)
            raise
        logger.info("[WS->] reply_written id=%s method=%s（仅本地写入成功）", rpc_id, method)

    @staticmethod
    def _socket_is_closing(ws: Any) -> bool:
        """兼容 websockets 版本与测试替身，判断 socket 是否已不可写。"""
        state = getattr(ws, "state", None)
        state_name = str(getattr(state, "name", state)).upper()
        return bool(getattr(ws, "closed", False)) or state_name in ("CLOSING", "CLOSED")

    async def send_frame(self, frame: dict[str, Any]) -> bool:
        """发送主动事件帧；仅当前连接完整写入时返回 ``True``。

        此返回值表示本地 WebSocket 写入结果，不是网关业务确认。网络在写入后
        中断时无法证明对端是否收到，因此调用方会按同一事件/序号保守重放。
        """
        ws = self.ws
        generation = self._connection_generation
        logger.info("[WS->] active_frame_attempt=%s", _audit_json(frame))
        if ws is None:
            logger.warning("WebSocket 未连接，无法发送帧 type=%s", frame.get("type"))
            return False

        if self._socket_is_closing(ws):
            logger.warning("WebSocket 已关闭，无法发送帧 type=%s", frame.get("type"))
            return False
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            logger.error("发送帧出错 type=%s：%s", frame.get("type"), e)
            return False

        logger.info("[WS->] active_frame_written type=%s（仅本地写入成功）", frame.get("type"))

        # send() 可在 await 时让出控制权；若此期间 run() 已断开并换了连接，
        # 旧会话上的写入不能推进本地事件基线。
        if self.ws is not ws or self._connection_generation != generation:
            logger.warning("发送帧期间连接已切换，保留事件重试 type=%s", frame.get("type"))
            return False
        if self._socket_is_closing(ws):
            logger.warning("发送帧后连接已关闭，保留事件重试 type=%s", frame.get("type"))
            return False
        return True

    def is_connected(self) -> bool:
        """是否已连接"""
        return self.state == ConnectionState.CONNECTED
