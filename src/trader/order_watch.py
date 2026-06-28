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

from . import config

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


def _is_open(o: dict) -> bool:
    """该委托是否仍未完成（可能继续成交）。"""
    if "已撤" in o["note"] or "已成" in o["note"]:
        return False
    if o["order_qty"] > 0 and o["filled_qty"] >= o["order_qty"]:
        return False
    return True


def next_interval(snapshot: dict, idle_secs: int, active_secs: int) -> int:
    """有未完成委托挂着 → active（提速）；否则 idle（降频）。"""
    return active_secs if any(_is_open(o) for o in snapshot.values()) else idle_secs


async def _poll_once(backend, client, prev: Optional[dict], seq: int) -> tuple[Optional[dict], int, bool]:
    """单轮：取委托快照 → diff → 发帧。返回 (new_prev, new_seq, ok)。"""
    async with backend.win_lock:
        active = await backend.orders_active()
    if not active or active.get("code") != 0:
        return prev, seq, False                      # 未绑定/验证码/读失败 → 跳过本轮
    cur = build_snapshot(active)
    if prev is None:
        logger.info("order_watch 基线建立：%d 笔委托", len(cur))
        return cur, seq, True                         # 重启只建基线，不补发历史
    events = diff_snapshots(prev, cur, backend.agent_entrust_nos)
    for eno in prev:
        if eno not in cur:
            logger.warning("order_watch 委托 %s 已从委托表消失，保守起见未发事件", eno)
    for ev in events:
        seq += 1
        ev["seq"] = seq
        ev["ts"] = time.time()
        await client.send_frame(ev)
        logger.info("order_watch 推送 %s entrust=%s source=%s filled=%s",
                    ev["event"], ev["entrust_no"], ev["source"], ev["filled_qty"])
    return cur, seq, True


async def order_watch_task(state, client) -> None:
    """自适应盯委托表 → 事件驱动推送。与 _ths_polling_task 同样 exception-safe。

    验证码顾虑 → 分钟级:空闲 idle（默认 5min）,有未完成委托时提速 active（默认 1min）。
    """
    backend = client.backend
    cfg = config.load()
    idle_secs = cfg.order_watch_idle_secs or IDLE_INTERVAL_DEFAULT
    active_secs = cfg.order_watch_active_secs or ACTIVE_INTERVAL_DEFAULT
    prev: Optional[dict] = None
    seq = 0
    interval = idle_secs
    logger.info("order_watch_task 启动（空闲 %ds / 活跃 %ds）", idle_secs, active_secs)
    while True:
        try:
            await asyncio.sleep(interval)
            snap = state.snapshot()
            if snap.get("connection_state") != "CONNECTED":
                interval = idle_secs
                continue
            if not snap.get("enable_ths_plugin", True):
                interval = idle_secs
                continue
            if not in_trading_session(datetime.now()):
                interval = idle_secs
                continue
            prev, seq, ok = await _poll_once(backend, client, prev, seq)
            interval = next_interval(prev, idle_secs, active_secs) if (ok and prev) else idle_secs
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("order_watch_task 异常：%s", e)
            interval = idle_secs
