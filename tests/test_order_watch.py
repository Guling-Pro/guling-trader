"""order_watch 纯函数单测：交易时段 / 快照解析 / diff 状态机 / 轮询薄壳。

沿用本仓库测试约定：同步测试，async 用 asyncio.run 驱动，不依赖 pytest-asyncio。
"""
from datetime import datetime

from trader import order_watch


def test_in_trading_session_morning_and_afternoon():
    # 周三 10:00 / 14:00 在盘中；12:00 午休不在；08:00 盘前不在。
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 10, 0)) is True
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 14, 0)) is True
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 12, 0)) is False
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 8, 0)) is False


def test_in_trading_session_weekend_is_false():
    # 2026-06-27 是周六。
    assert order_watch.in_trading_session(datetime(2026, 6, 27, 10, 0)) is False
