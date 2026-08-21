"""本机周期性快照委托表 → diff → 经 WS 主动推 order_event 事件。

设计要点见 docs/superpowers/plans/2026-06-28-order-event-push.md。
纯函数（build_snapshot / diff_snapshots / in_trading_session）与异步薄壳分离，
便于离线单测。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from datetime import datetime, time as dtime
from typing import Any, Deque, Optional

from . import config, contract
from .ths.rows import ST_CANCELED, ST_FILLED, ST_PARTIAL, ST_REJECTED, is_in_flight

logger = logging.getLogger(__name__)

IDLE_INTERVAL_DEFAULT = 300   # 空闲（无未完成委托）轮询周期：5 分钟。验证码顾虑→分钟级
ACTIVE_INTERVAL_DEFAULT = 60  # 有未完成委托挂着时提速：1 分钟（为及时抓成交）
SEND_RETRY_INTERVAL_DEFAULT = 5  # 主动事件发送失败后短间隔重试；不重新读取/操作交易窗口。
FRAME_TYPE = "order_event"

# 契约 v2 规范化后的键（不再是 THS 原始表头）。
# 注意数据源：order_watch 读的是 orders_active_all（含终态），不是对外的
# orders_active——后者按 C3 过滤掉终态行，用它会把 filled/canceled 事件全丢掉。
COL_CODE = "证券代码"
COL_OP = "方向"
COL_ORDER_QTY = "委托数量"
COL_ORDER_PRICE = "委托价"
COL_FILLED_QTY = "已成数量"
COL_AVG_PRICE = "成交均价"
COL_ENTRUST_NO = "entrust_no"
COL_STATE = "状态"
COL_NOTE = "柜台备注"

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
    """把 orders_active_all 返回解析为 {entrust_no: order_state}。非 succeed/空 → {}。"""
    snap: dict[str, dict] = {}
    if not contract.is_succeed(active_result or {}):
        return snap
    for row in (active_result.get("data") or []):
        eno = str(row.get(COL_ENTRUST_NO) or "").strip()
        if not eno:
            continue
        snap[eno] = {
            "entrust_no": eno,
            "stock_no": row.get(COL_CODE) or "",
            "op": row.get(COL_OP) or "",
            "order_qty": _to_int(row.get(COL_ORDER_QTY)),
            "order_price": row.get(COL_ORDER_PRICE),
            "filled_qty": _to_int(row.get(COL_FILLED_QTY)),
            "avg_price": row.get(COL_AVG_PRICE),
            "state": row.get(COL_STATE) or "未知",
            "note": row.get(COL_NOTE) or "",
        }
    return snap


def _is_full(o: dict) -> bool:
    return o["order_qty"] > 0 and o["filled_qty"] >= o["order_qty"]


def _classify_new(o: dict) -> str:
    # 状态取自契约枚举（由柜台备注结构化而来），不再在这里做二次文本匹配。
    if o.get("state") in (ST_CANCELED, ST_REJECTED):
        return "canceled"
    if o.get("state") == ST_FILLED or _is_full(o):
        return "filled"
    if o.get("state") == ST_PARTIAL or o["filled_qty"] > 0:
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
        if (o.get("state") in (ST_CANCELED, ST_REJECTED)
                and before.get("state") not in (ST_CANCELED, ST_REJECTED)):
            events.append(_make_event("canceled", o, agent_entrust_nos))
            continue
        if o["filled_qty"] > before["filled_qty"]:
            name = "filled" if _is_full(o) else "partially_filled"
            events.append(_make_event(name, o, agent_entrust_nos))
    return events


def _is_open(o: dict) -> bool:
    """该委托是否仍未完成（可能继续成交）。与 orders_active 的在飞判据同源。"""
    return is_in_flight(o.get("state") or "未知", o["order_qty"], o["filled_qty"])


def next_interval(snapshot: dict, idle_secs: int, active_secs: int) -> int:
    """有未完成委托挂着 → active（提速）；否则 idle（降频）。"""
    return active_secs if any(_is_open(o) for o in snapshot.values()) else idle_secs


async def _send_frame(client, frame: dict) -> bool:
    """生产客户端必须显式返回 ``True`` 才算已写入。"""
    send_result = client.send_frame(frame)
    if inspect.isawaitable(send_result):
        send_result = await send_result
    return send_result is True


def _pending_events(client, pending: Optional[Deque[dict]]) -> Deque[dict]:
    """取得看门狗的待发 FIFO；兼容直接调用内部薄壳的旧测试/调用方。"""
    if pending is not None:
        return pending
    queue = getattr(client, "_order_watch_pending_events", None)
    if queue is None:
        queue = deque()
        setattr(client, "_order_watch_pending_events", queue)
    return queue


async def _flush_pending_events(client, pending: Deque[dict]) -> bool:
    """按 FIFO 重发未确认写入的帧；成功帧只出队一次。"""
    try:
        while pending:
            frame = pending[0]
            if not await _send_frame(client, frame):
                logger.warning("order_watch 待发事件未写入（下轮重试）")
                return False
            pending.popleft()
            logger.info("order_watch 发送成功 %s entrust=%s seq=%s",
                        frame["event"], frame["entrust_no"], frame["seq"])
    except Exception as e:
        logger.warning("order_watch 待发事件发送异常（下轮重试）：%s", e)
        return False
    return True


async def _poll_once(
    backend,
    client,
    prev: Optional[dict],
    seq: int,
    pending: Optional[Deque[dict]] = None,
) -> tuple[Optional[dict], int, bool]:
    """单轮：先送待发帧，再取委托快照并排入新事件。

    ``pending`` 是进程内 FIFO。每个事件分配序号后不再改变，只有成功写入当前
    WebSocket 才出队；观察基线在事件入队时推进，以免失败后重新构造已成功帧。
    断线期间不执行任何交易 RPC，只保留通知帧。
    """
    pending = _pending_events(client, pending)

    # 先清空上轮失败留下的帧。失败时不要读取 THS 表或改变基线，避免同一事件
    # 被重新构造、已成功帧被重复提交，或在失败窗口内丢失原始观察结果。
    if pending and not await _flush_pending_events(client, pending):
        return prev, seq, False

    async with backend.win_lock:
        # 全量表（含终态）：终态行正是 filled/canceled 事件的来源。
        active = await backend.orders_active_all()
    if not contract.is_succeed(active or {}):
        return prev, seq, False                      # 未绑定/验证码/读失败 → 跳过本轮
    cur = build_snapshot(active)
    if prev is None:
        logger.info("order_watch 基线建立：%d 笔委托", len(cur))
        return cur, seq, True                         # 重启只建基线，不补发历史
    events = diff_snapshots(prev, cur, backend.agent_entrust_nos)
    for eno in prev:
        if eno not in cur:
            logger.warning("order_watch 委托 %s 已从委托表消失，保守起见未发事件", eno)

    # 快照只能在所有 diff 事件都已排入 FIFO 后推进。这样单批的首帧已成功、
    # 后帧失败时，不会重新发送首帧；未发帧保留自己的 seq 和检测时间。
    try:
        for ev in events:
            seq += 1
            ev["seq"] = seq
            ev["ts"] = time.time()
            pending.append(ev)

        if not await _flush_pending_events(client, pending):
            return cur, seq, False
    except Exception as e:
        logger.warning("order_watch 发送事件失败（下轮重试）：%s", e)
        return cur, seq, False
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
    pending: Deque[dict] = deque()
    interval = idle_secs
    logger.info("order_watch_task 启动（空闲 %ds / 活跃 %ds）", idle_secs, active_secs)
    while True:
        try:
            await asyncio.sleep(interval)
            snap = state.snapshot()
            if snap.get("connection_state") != "CONNECTED":
                logger.debug("order_watch 跳过：连接状态 %s", snap.get("connection_state"))
                interval = min(active_secs, SEND_RETRY_INTERVAL_DEFAULT) if pending else idle_secs
                continue
            if not snap.get("enable_ths_plugin", True):
                logger.debug("order_watch 跳过：THS 插件已禁用")
                interval = min(active_secs, SEND_RETRY_INTERVAL_DEFAULT) if pending else idle_secs
                continue
            # 断线期间积压的事件不需要访问交易窗口，连接恢复后立即按原 seq
            # 重发；这条路径只发送通知帧，绝不触发买卖或撤单 RPC。
            if pending and not await _flush_pending_events(client, pending):
                interval = min(active_secs, SEND_RETRY_INTERVAL_DEFAULT)
                continue
            if not in_trading_session(datetime.now()):
                logger.debug("order_watch 跳过：非交易时段")
                interval = idle_secs
                continue
            prev, seq, ok = await _poll_once(backend, client, prev, seq, pending)
            if pending:
                interval = min(active_secs, SEND_RETRY_INTERVAL_DEFAULT)
            else:
                interval = next_interval(prev, idle_secs, active_secs) if (ok and prev) else idle_secs
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("order_watch_task 异常：%s", e)
            interval = idle_secs
