"""order_watch 纯函数单测：交易时段 / 快照解析 / diff 状态机 / 轮询薄壳。

沿用本仓库测试约定：同步测试，async 用 asyncio.run 驱动，不依赖 pytest-asyncio。
"""
from datetime import datetime
from types import SimpleNamespace

from trader import order_watch, watchlist_watch


def test_in_trading_session_morning_and_afternoon():
    # 周三 10:00 / 14:00 在盘中；12:00 午休不在；08:00 盘前不在。
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 10, 0)) is True
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 14, 0)) is True
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 12, 0)) is False
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 8, 0)) is False


def test_in_trading_session_weekend_is_false():
    # 2026-06-27 是周六。
    assert order_watch.in_trading_session(datetime(2026, 6, 27, 10, 0)) is False


def _active(rows):
    """orders_active_all 的契约 v2 信封（行已规范化：number + 方向/状态枚举）。"""
    return {"status": "succeed", "code": "ok", "data": rows,
            "error": None, "contract_version": "2"}


def _row(eno, qty, filled, state, code="600519", op="买入", price=1700.0, avg=None):
    """规范化后的委托行（normalize_active_row 的产物形状）。"""
    return {"client_order_id": None, "entrust_no": eno, "证券代码": code,
            "证券名称": "贵州茅台", "方向": op, "委托价": price, "委托数量": qty,
            "已成数量": filled, "成交均价": avg, "状态": state, "柜台备注": state}


def test_build_snapshot_parses_real_headers():
    snap = order_watch.build_snapshot(_active([_row("12345", 100, 0, "已报")]))
    assert set(snap) == {"12345"}
    o = snap["12345"]
    assert o["stock_no"] == "600519"
    assert o["op"] == "买入"
    assert o["order_qty"] == 100
    assert o["order_price"] == 1700.0
    assert o["filled_qty"] == 0
    assert o["state"] == "已报"


def test_build_snapshot_skips_rows_without_entrust_no():
    snap = order_watch.build_snapshot(_active([{"证券代码": "600519", "entrust_no": ""}]))
    assert snap == {}


def test_build_snapshot_empty_on_error_code():
    assert order_watch.build_snapshot(
        {"status": "failed", "code": "read_failed", "data": None,
         "error": {"class": "read_failed", "broker_msg": None, "message": "读取失败"},
         "contract_version": "2"}) == {}
    assert order_watch.build_snapshot(None) == {}


def _order(eno, qty, filled, state, code="600519", op="买入", price=1700.0, avg=None):
    return {
        "entrust_no": eno, "stock_no": code, "op": op,
        "order_qty": qty, "order_price": price,
        "filled_qty": filled, "avg_price": avg, "state": state, "note": state,
    }


def test_new_order_emits_placed():
    cur = {"1": _order("1", 100, 0, "已报")}
    evs = order_watch.diff_snapshots({}, cur, set())
    assert len(evs) == 1
    e = evs[0]
    assert e["type"] == "order_event"
    assert e["event"] == "placed"
    assert e["source"] == "external"
    assert e["entrust_no"] == "1"
    assert e["order_qty"] == 100
    assert e["filled_qty"] == 0


def test_placed_then_partial_then_full():
    s0 = {"1": _order("1", 100, 0, "已报")}
    s1 = {"1": _order("1", 100, 60, "部成", avg=1699.5)}
    s2 = {"1": _order("1", 100, 100, "已成", avg=1699.8)}

    e1 = order_watch.diff_snapshots(s0, s1, set())
    assert [e["event"] for e in e1] == ["partially_filled"]
    assert e1[0]["filled_qty"] == 60
    assert e1[0]["avg_price"] == 1699.5

    e2 = order_watch.diff_snapshots(s1, s2, set())
    assert [e["event"] for e in e2] == ["filled"]
    assert e2[0]["filled_qty"] == 100
    assert e2[0]["note"] == "已成"


def test_placed_then_canceled():
    s0 = {"1": _order("1", 100, 0, "已报")}
    s1 = {"1": _order("1", 100, 0, "已撤")}
    evs = order_watch.diff_snapshots(s0, s1, set())
    assert [e["event"] for e in evs] == ["canceled"]


def test_no_change_is_idempotent():
    s0 = {"1": _order("1", 100, 60, "部成")}
    assert order_watch.diff_snapshots(s0, dict(s0), set()) == []


def test_source_tagged_agent_when_entrust_known():
    cur = {"9": _order("9", 100, 0, "已报")}
    evs = order_watch.diff_snapshots({}, cur, {"9"})
    assert evs[0]["source"] == "agent"


def test_disappeared_order_emits_nothing():
    s0 = {"1": _order("1", 100, 0, "已报")}
    assert order_watch.diff_snapshots(s0, {}, set()) == []


# ===== Task 5: 轮询薄壳 _poll_once + next_interval(自适应) + order_watch_task =====
import asyncio


class WatchFakeBackend:
    def __init__(self, scripted):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self._scripted = list(scripted)   # 每次 orders_active 返回下一项
        self._i = 0

    async def orders_active_all(self):
        """order_watch 读全量表（含终态）——终态行正是 filled/canceled 事件的来源。"""
        item = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return item


class WatchFakeClient:
    def __init__(self, backend):
        self.backend = backend
        self.sent: list[dict] = []

    async def send_frame(self, frame):
        self.sent.append(frame)
        return True


def test_first_round_builds_baseline_no_emit():
    backend = WatchFakeBackend([_active([_row("1", 100, 0, "已报")])])
    client = WatchFakeClient(backend)

    async def drive():
        prev, seq, ok = await order_watch._poll_once(backend, client, None, 0)
        return prev, seq, ok

    prev, seq, ok = asyncio.run(drive())
    assert ok is True
    assert set(prev) == {"1"}
    assert client.sent == []          # 重启只建基线，不补发历史


def test_second_round_emits_fill_with_seq_and_ts():
    r0 = _active([_row("1", 100, 0, "已报")])
    r1 = _active([_row("1", 100, 100, "已成", avg=1699.8)])
    backend = WatchFakeBackend([r0, r1])
    client = WatchFakeClient(backend)

    async def drive():
        prev, seq, _ = await order_watch._poll_once(backend, client, None, 0)
        prev, seq, _ = await order_watch._poll_once(backend, client, prev, seq)
        return seq

    seq = asyncio.run(drive())
    assert len(client.sent) == 1
    ev = client.sent[0]
    assert ev["event"] == "filled"
    assert ev["seq"] == 1
    assert isinstance(ev["ts"], float)
    assert seq == 1


def test_read_failure_skips_round():
    backend = WatchFakeBackend([
        {"status": "failed", "code": "read_failed", "data": None,
         "error": {"class": "read_failed", "broker_msg": None, "message": "验证码弹窗"},
         "contract_version": "2"}])
    client = WatchFakeClient(backend)

    async def drive():
        return await order_watch._poll_once(backend, client, None, 0)

    prev, seq, ok = asyncio.run(drive())
    assert ok is False
    assert prev is None              # 读失败不污染基线
    assert client.sent == []


def test_next_interval_active_when_open_order():
    snap = {"1": _order("1", 100, 0, "已报")}            # 未完成 → 提速
    assert order_watch.next_interval(snap, 300, 60) == 60


def test_next_interval_active_when_partial():
    snap = {"1": _order("1", 100, 60, "部成")}           # 部成仍未完成 → 提速
    assert order_watch.next_interval(snap, 300, 60) == 60


def test_next_interval_idle_when_all_done():
    snap = {
        "1": _order("1", 100, 100, "已成"),
        "2": _order("2", 100, 0, "已撤"),
    }
    assert order_watch.next_interval(snap, 300, 60) == 300


def test_next_interval_idle_when_empty():
    assert order_watch.next_interval({}, 300, 60) == 300


def test_false_send_frame_queues_same_event_and_seq_for_reconnect_retry():
    """False keeps the observed transition in FIFO; a retry uses its original seq."""
    r0 = _active([_row("1", 100, 0, "已报")])
    r1 = _active([_row("1", 100, 100, "已成", avg=1699.8)])
    backend = WatchFakeBackend([r0, r1])

    # Runtime WsClient returns False on send failure.
    class ReconnectingClient:
        def __init__(self, backend):
            self.backend = backend
            self.sent = []
            self.connected = True

        async def send_frame(self, frame):
            self.sent.append(frame)
            return self.connected

    async def drive():
        client = ReconnectingClient(backend)

        # Round 1: establish baseline from r0
        prev, seq, ok = await order_watch._poll_once(backend, client, None, 0)
        assert ok is True
        assert set(prev) == {"1"}

        # Round 2: try to send filled event (r0→r1), but send_frame returns False.
        client.connected = False
        prev_after, seq_after, ok_after = await order_watch._poll_once(backend, client, prev, seq)
        assert ok_after is False, "should return False when send fails"
        assert prev_after == order_watch.build_snapshot(r1)
        assert seq_after == 1
        assert [frame["seq"] for frame in client.sent] == [1]

        # The same WsClient instance survives reconnect; it only replays the
        # failed frame and never reconstructs/resubmits a trading RPC.
        client.connected = True
        prev_retry, seq_retry, ok_retry = await order_watch._poll_once(
            backend, client, prev_after, seq_after
        )
        assert ok_retry is True, "should succeed on retry"
        assert len(client.sent) == 2
        assert [frame["seq"] for frame in client.sent] == [1, 1]
        assert client.sent[-1]["event"] == "filled"
        assert client.sent[-1]["entrust_no"] == "1"
        assert seq_retry == 1

    asyncio.run(drive())


def test_multi_event_failure_replays_only_unsent_frame():
    r0 = _active([])
    r1 = _active([
        _row("1", 100, 0, "已报"),
        _row("2", 200, 0, "已报", code="000001"),
    ])
    backend = WatchFakeBackend([r0, r1])

    class PartiallyFailingClient:
        def __init__(self, backend):
            self.backend = backend
            self.sent = []

        async def send_frame(self, frame):
            self.sent.append(frame)
            return len(self.sent) != 2

    async def drive():
        client = PartiallyFailingClient(backend)
        prev, seq, ok = await order_watch._poll_once(backend, client, None, 0)
        assert ok is True

        prev_after, seq_after, ok_after = await order_watch._poll_once(
            backend, client, prev, seq
        )
        assert ok_after is False
        assert prev_after == order_watch.build_snapshot(r1)
        assert seq_after == 2
        assert [frame["seq"] for frame in client.sent] == [1, 2]

        prev_retry, seq_retry, ok_retry = await order_watch._poll_once(
            backend, client, prev_after, seq_after
        )
        assert ok_retry is True
        assert prev_retry == order_watch.build_snapshot(r1)
        assert seq_retry == 2
        assert [frame["seq"] for frame in client.sent] == [1, 2, 2]

    asyncio.run(drive())


def test_watchlist_false_send_keeps_baseline_and_retries(monkeypatch):
    class WatchlistBackend:
        def __init__(self):
            self.win_lock = asyncio.Lock()
            self._results = [
                {"status": "succeed", "data": {"codes": ["600519"]}},
                {"status": "succeed", "data": {"codes": ["000001", "600519"]}},
            ]
            self._index = 0

        async def watchlist(self):
            result = self._results[min(self._index, len(self._results) - 1)]
            self._index += 1
            return result

    class RetryingClient:
        def __init__(self, backend):
            self.backend = backend
            self.sent = []

        def send_frame(self, frame):
            self.sent.append(frame)
            return len(self.sent) > 1

    class ConnectedState:
        def __init__(self):
            self._states = iter(("CONNECTED", "CONNECTED", "DISCONNECTED", "CONNECTED"))

        def snapshot(self):
            return {
                "connection_state": next(self._states),
                "enable_ths_plugin": True,
            }

    sleep_calls = 0

    async def fake_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        watchlist_watch.config,
        "load",
        lambda: SimpleNamespace(
            enable_watchlist_watch=True,
            watchlist_sync_hours="8,12,16,20",
        ),
    )
    monkeypatch.setattr(watchlist_watch.asyncio, "sleep", fake_sleep)

    backend = WatchlistBackend()
    client = RetryingClient(backend)
    asyncio.run(watchlist_watch.watchlist_watch_task(ConnectedState(), client))

    assert len(client.sent) == 2
    assert [frame["seq"] for frame in client.sent] == [1, 1]
    assert client.sent[0]["codes"] == client.sent[1]["codes"] == ["000001", "600519"]
    assert backend._index == 2, "replay must not OCR/read THS again"
