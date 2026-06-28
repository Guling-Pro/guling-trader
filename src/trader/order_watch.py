"""本机周期性快照委托表 → diff → 经 WS 主动推 order_event 事件。

设计要点见 docs/superpowers/plans/2026-06-28-order-event-push.md。
纯函数（build_snapshot / diff_snapshots / in_trading_session）与异步薄壳分离，
便于离线单测。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from typing import Any, Optional

logger = logging.getLogger(__name__)

IDLE_INTERVAL_DEFAULT = 300   # 空闲（无未完成委托）轮询周期：5 分钟。验证码顾虑→分钟级
ACTIVE_INTERVAL_DEFAULT = 60  # 有未完成委托挂着时提速：1 分钟（为及时抓成交）
FRAME_TYPE = "order_event"

# THS 真实表头（逐字）
COL_CODE = "证券代码"
COL_OP = "操作"
COL_ORDER_QTY = "委托数量"
COL_ORDER_PRICE = "委托价格"
COL_FILLED_QTY = "成交数量"
COL_AVG_PRICE = "成交均价"
COL_ENTRUST_NO = "合同编号"
COL_NOTE = "备注"

_MORNING = (dtime(9, 30), dtime(11, 30))
_AFTERNOON = (dtime(13, 0), dtime(15, 0))


def in_trading_session(now: datetime) -> bool:
    """A 股交易时段判断（不含节假日；节假日由非交易日无委托变化天然兜住）。"""
    if now.weekday() >= 5:          # 周六/周日
        return False
    t = now.time()
    return (_MORNING[0] <= t <= _MORNING[1]) or (_AFTERNOON[0] <= t <= _AFTERNOON[1])


def _to_int(value: Any) -> int:
    try:
        return int(str(value if value is not None else "0").strip().replace(",", "") or "0")
    except (ValueError, TypeError):
        return 0


def build_snapshot(active_result: Optional[dict]) -> dict[str, dict]:
    """把 orders_active 返回解析为 {合同编号: order_state}。code!=0/空 → {}。"""
    snap: dict[str, dict] = {}
    if not active_result or active_result.get("code") != 0:
        return snap
    for row in active_result.get("data", []) or []:
        eno = (row.get(COL_ENTRUST_NO) or "").strip()
        if not eno:
            continue
        snap[eno] = {
            "entrust_no": eno,
            "stock_no": (row.get(COL_CODE) or "").strip(),
            "op": (row.get(COL_OP) or "").strip(),
            "order_qty": _to_int(row.get(COL_ORDER_QTY)),
            "order_price": (row.get(COL_ORDER_PRICE) or "").strip(),
            "filled_qty": _to_int(row.get(COL_FILLED_QTY)),
            "avg_price": (row.get(COL_AVG_PRICE) or "").strip(),
            "note": (row.get(COL_NOTE) or "").strip(),
        }
    return snap


def _is_full(o: dict) -> bool:
    return o["order_qty"] > 0 and o["filled_qty"] >= o["order_qty"]


def _classify_new(o: dict) -> str:
    if "已撤" in o["note"]:
        return "canceled"
    if _is_full(o):
        return "filled"
    if o["filled_qty"] > 0:
        return "partially_filled"
    return "placed"


def _make_event(event_name: str, o: dict, agent_entrust_nos: set[str]) -> dict:
    return {
        "type": FRAME_TYPE,
        "event": event_name,
        "source": "agent" if o["entrust_no"] in agent_entrust_nos else "external",
        "entrust_no": o["entrust_no"],
        "stock_no": o["stock_no"],
        "op": o["op"],
        "order_qty": o["order_qty"],
        "order_price": o["order_price"],
        "filled_qty": o["filled_qty"],
        "avg_price": o["avg_price"],
        "note": o["note"],
    }


def diff_snapshots(prev: dict[str, dict], cur: dict[str, dict],
                   agent_entrust_nos: set[str]) -> list[dict]:
    """对比两轮快照，返回 order_event 列表（不含 seq/ts）。"""
    events: list[dict] = []
    for eno, o in cur.items():
        before = prev.get(eno)
        if before is None:
            events.append(_make_event(_classify_new(o), o, agent_entrust_nos))
            continue
        if "已撤" in o["note"] and "已撤" not in before["note"]:
            events.append(_make_event("canceled", o, agent_entrust_nos))
            continue
        if o["filled_qty"] > before["filled_qty"]:
            name = "filled" if _is_full(o) else "partially_filled"
            events.append(_make_event(name, o, agent_entrust_nos))
    return events
