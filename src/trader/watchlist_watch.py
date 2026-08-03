"""定时同步自选股 → diff → 经 WS 主动推 watchlist_event 事件。

自选股是新版 xiadan 内嵌 CEF 网页，只能截图+OCR 读取（见 ths/win.get_watchlist），
且只读第一屏（顶部）。按同花顺习惯，新加入的自选股出现在顶部，所以看门狗只比顶部
就能捕捉"新增"。调度用定点整点（默认 8/12/16/20，避开交易时段，覆盖盘前/午间/盘后/晚间）
而非高频轮询——自选股不会秒秒变，且每次会把 xiadan 界面切到自选股面板。

与 order_watch 一致：exception-safe、串行化 win_lock、断线/插件禁用时跳过。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

FRAME_TYPE = "watchlist_event"


def _parse_hours(spec: str) -> list[int]:
    hours = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.append(int(part))
    return sorted(set(hours)) or [8, 12, 16, 20]


def next_fire(now: datetime, hours: list[int]) -> datetime:
    """下一个整点触发时刻（今天剩余的，否则明天第一个）。"""
    for h in hours:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t > now:
            return t
    return (now + timedelta(days=1)).replace(
        hour=hours[0], minute=0, second=0, microsecond=0)


async def watchlist_watch_task(state, client) -> None:
    """定点同步自选股顶部 → 变化则推 watchlist_event。exception-safe。"""
    backend = client.backend
    cfg = config.load()
    if not getattr(cfg, "enable_watchlist_watch", True):
        logger.info("watchlist_watch 已禁用（enable_watchlist_watch=False）")
        return
    hours = _parse_hours(getattr(cfg, "watchlist_sync_hours", "8,12,16,20"))
    prev: Optional[list[str]] = None
    seq = 0
    logger.info("watchlist_watch_task 启动，定点同步整点 %s", hours)
    first = True
    while True:
        try:
            if first:
                # 启动后先等 30s（等连接/登录稳定）建立基线，也便于验证读取正常
                await asyncio.sleep(30)
                first = False
            else:
                now = datetime.now()
                nxt = next_fire(now, hours)
                wait = max(1.0, (nxt - now).total_seconds())
                logger.debug("watchlist_watch 下次同步 %s（%.0f 秒后）", nxt, wait)
                await asyncio.sleep(wait)

            snap = state.snapshot()
            if snap.get("connection_state") != "CONNECTED":
                logger.debug("watchlist_watch 跳过：连接状态 %s", snap.get("connection_state"))
                continue
            if not snap.get("enable_ths_plugin", True):
                logger.debug("watchlist_watch 跳过：THS 插件已禁用")
                continue

            async with backend.win_lock:
                res = await backend.watchlist()
            if not res or res.get("status") != "succeed":
                logger.info("watchlist_watch 跳过：读取失败 %s", (res or {}).get("msg"))
                continue
            cur = list((res.get("data") or {}).get("codes") or [])
            if prev is None:
                prev = cur
                logger.info("watchlist_watch 基线建立：顶部 %d 只", len(cur))
                continue
            if cur == prev:
                logger.debug("watchlist_watch 无变化（顶部 %d 只）", len(cur))
                continue

            # 新增出现在顶部；removed 仅供参考（顶部第一屏，可能是被新增挤下屏而非真删）
            added = [c for c in cur if c not in prev]
            seq += 1
            frame = {
                "type": FRAME_TYPE,
                "event": "changed",
                "added": added,
                "codes": cur,       # 当前顶部第一屏代码（顶部在前）
                "partial": True,    # 仅第一屏，非全量
                "seq": seq,
                "ts": time.time(),
            }
            try:
                await client.send_frame(frame)
                logger.info("watchlist_watch 推送变化：新增 %s（顶部 %d 只）", added, len(cur))
                prev = cur
            except Exception as e:
                logger.warning("watchlist_watch 发送失败（下次重试）：%s", e)  # 不推进基线
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("watchlist_watch_task 异常：%s", e)
            await asyncio.sleep(60)
