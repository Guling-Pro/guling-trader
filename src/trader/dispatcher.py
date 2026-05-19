"""RPC 分派：call frame → backend method → reply frame"""
import json
import logging
from typing import Any, Optional

from .ths.win import WinThsBackend

logger = logging.getLogger(__name__)

METHOD_WHITELIST = {"balance", "position", "orders_active", "orders_filled", "buy", "sell", "cancel"}


async def handle_call(
    frame: dict[str, Any],
    backend: WinThsBackend,
) -> dict[str, Any]:
    """处理 RPC call 帧，返回 reply 帧"""
    frame_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params", {})

    reply = {"type": "reply", "id": frame_id}

    if method not in METHOD_WHITELIST:
        reply["ok"] = False
        reply["error"] = f"方法 '{method}' 不支持"
        return reply

    try:
        if method == "balance":
            logger.info("[RPC] method=balance, frame_id=%s", frame_id)
            result = await backend.balance()
            logger.info("[RPC] balance → code=%s", result.get("code"))
        elif method == "position":
            result = await backend.position()
        elif method == "orders_active":
            result = await backend.orders_active()
        elif method == "orders_filled":
            result = await backend.orders_filled()
        elif method == "buy":
            stock_no = params.get("stock_no")
            amount = params.get("amount")
            price = params.get("price")
            client_order_id = params.get("client_order_id")
            result = await backend.buy(stock_no, amount, price, client_order_id)
        elif method == "sell":
            stock_no = params.get("stock_no")
            amount = params.get("amount")
            price = params.get("price")
            client_order_id = params.get("client_order_id")
            result = await backend.sell(stock_no, amount, price, client_order_id)
        elif method == "cancel":
            entrust_no = params.get("entrust_no")
            result = await backend.cancel(entrust_no)
        else:
            result = {"code": 1, "error": "内部错误"}

        if isinstance(result, dict) and result.get("code") == 0:
            reply["ok"] = True
            reply["result"] = result
        else:
            reply["ok"] = False
            reply["error"] = result.get("error", "未知错误")

    except Exception as e:
        logger.error("处理 RPC '%s' 出错：%s", method, e)
        reply["ok"] = False
        reply["error"] = str(e)

    return reply
