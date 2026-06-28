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
