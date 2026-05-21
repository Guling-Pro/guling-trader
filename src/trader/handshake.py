"""握手流程处理：pair_init 和 resume"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

from . import config

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

CLIENT_VERSION = "0.4.6"


@dataclass
class HandshakeResult:
    """握手结果"""

    success: bool
    error: Optional[str] = None
    reason: Optional[str] = None
    should_clear_config: bool = False
    pair_pending: Optional[dict[str, Any]] = None  # pair_init 成功时含 {code, expires_at}


async def perform_handshake(
    ws: "ClientConnection",
    cfg: config.TraderConfig,
) -> HandshakeResult:
    """执行握手：根据配置选择 pair_init 或 resume"""
    try:
        if cfg.has_paired():
            return await _resume(ws, cfg)
        else:
            return await _pair_init(ws, cfg)
    except Exception as e:
        logger.error("握手异常：%s", e)
        return HandshakeResult(
            success=False,
            error=str(e),
        )


async def _pair_init(
    ws: "ClientConnection",
    cfg: config.TraderConfig,
) -> HandshakeResult:
    """首次配对：发送 pair_init，等待 pair_pending"""
    hello_frame = {
        "type": "hello",
        "mode": "pair_init",
        "device_id": cfg.device_id,
        "client_ver": CLIENT_VERSION,
    }

    logger.info("发送 pair_init：%s", hello_frame)
    await ws.send(json.dumps(hello_frame, ensure_ascii=False))

    try:
        raw_response = await asyncio.wait_for(ws.recv(), timeout=5.0)
        response = json.loads(raw_response)
        logger.info("收到握手应答：%s", response)

        frame_type = response.get("type")

        if frame_type == "pair_pending":
            return HandshakeResult(
                success=True,
                pair_pending={
                    "code": response.get("code"),
                    "expires_at": response.get("expires_at"),
                },
            )

        elif frame_type == "reject":
            reason = response.get("reason", "unknown")
            return HandshakeResult(
                success=False,
                error=f"握手被拒绝：{reason}",
                reason=reason,
            )

        else:
            return HandshakeResult(
                success=False,
                error=f"未预期的握手应答类型：{frame_type}",
            )

    except asyncio.TimeoutError:
        return HandshakeResult(
            success=False,
            error="握手超时（等待 pair_pending）",
        )


async def _resume(
    ws: "ClientConnection",
    cfg: config.TraderConfig,
) -> HandshakeResult:
    """重连：发送 resume 帧，等待 welcome 或 reject"""
    hello_frame = {
        "type": "hello",
        "mode": "resume",
        "device_id": cfg.device_id,
        "agent_token": cfg.agent_token,
        "client_ver": CLIENT_VERSION,
    }

    logger.info("发送 resume：%s（token 已隐藏）", {
        k: v if k != "agent_token" else "***"
        for k, v in hello_frame.items()
    })
    await ws.send(json.dumps(hello_frame, ensure_ascii=False))

    try:
        raw_response = await asyncio.wait_for(ws.recv(), timeout=5.0)
        response = json.loads(raw_response)
        logger.info("收到握手应答：%s", response)

        frame_type = response.get("type")

        if frame_type == "welcome":
            return HandshakeResult(success=True)

        elif frame_type == "reject":
            reason = response.get("reason", "unknown")
            logger.warning("resume 被拒绝：%s", reason)

            should_clear = reason in ("token_invalid", "account_removed")

            return HandshakeResult(
                success=False,
                error=f"resume 被拒绝：{reason}",
                reason=reason,
                should_clear_config=should_clear,
            )

        else:
            return HandshakeResult(
                success=False,
                error=f"未预期的握手应答类型：{frame_type}",
            )

    except asyncio.TimeoutError:
        return HandshakeResult(
            success=False,
            error="握手超时（等待 welcome）",
        )
