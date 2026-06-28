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


def _active(rows):
    return {"code": 0, "status": "succeed", "data": rows}


def test_build_snapshot_parses_real_headers():
    snap = order_watch.build_snapshot(_active([
        {
            "证券代码": "600519", "操作": "买入", "委托数量": "100",
            "委托价格": "1700.000", "成交数量": "0", "成交均价": "",
            "合同编号": "12345", "备注": "已报",
        },
    ]))
    assert set(snap) == {"12345"}
    o = snap["12345"]
    assert o["stock_no"] == "600519"
    assert o["op"] == "买入"
    assert o["order_qty"] == 100
    assert o["order_price"] == "1700.000"
    assert o["filled_qty"] == 0
    assert o["note"] == "已报"


def test_build_snapshot_skips_rows_without_entrust_no():
    snap = order_watch.build_snapshot(_active([{"证券代码": "600519", "合同编号": ""}]))
    assert snap == {}


def test_build_snapshot_empty_on_error_code():
    assert order_watch.build_snapshot({"code": 1, "msg": "读取失败"}) == {}
    assert order_watch.build_snapshot(None) == {}


def _order(eno, qty, filled, note, code="600519", op="买入", price="1700.000", avg=""):
    return {
        "entrust_no": eno, "stock_no": code, "op": op,
        "order_qty": qty, "order_price": price,
        "filled_qty": filled, "avg_price": avg, "note": note,
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
    s1 = {"1": _order("1", 100, 60, "部成", avg="1699.500")}
    s2 = {"1": _order("1", 100, 100, "已成", avg="1699.800")}

    e1 = order_watch.diff_snapshots(s0, s1, set())
    assert [e["event"] for e in e1] == ["partially_filled"]
    assert e1[0]["filled_qty"] == 60
    assert e1[0]["avg_price"] == "1699.500"

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

    async def orders_active(self):
        item = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return item


class WatchFakeClient:
    def __init__(self, backend):
        self.backend = backend
        self.sent: list[dict] = []

    async def send_frame(self, frame):
        self.sent.append(frame)


def test_first_round_builds_baseline_no_emit():
    backend = WatchFakeBackend([_active([
        {"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
         "成交数量": "0", "成交均价": "", "合同编号": "1", "备注": "已报"},
    ])])
    client = WatchFakeClient(backend)

    async def drive():
        prev, seq, ok = await order_watch._poll_once(backend, client, None, 0)
        return prev, seq, ok

    prev, seq, ok = asyncio.run(drive())
    assert ok is True
    assert set(prev) == {"1"}
    assert client.sent == []          # 重启只建基线，不补发历史


def test_second_round_emits_fill_with_seq_and_ts():
    r0 = _active([{"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
                   "成交数量": "0", "成交均价": "", "合同编号": "1", "备注": "已报"}])
    r1 = _active([{"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
                   "成交数量": "100", "成交均价": "1699.800", "合同编号": "1", "备注": "已成"}])
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
    backend = WatchFakeBackend([{"code": 1, "msg": "验证码弹窗"}])
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


def test_send_frame_failure_does_not_advance_baseline():
    """Regression: if send_frame raises, baseline should not advance; next round retries."""
    r0 = _active([{"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
                   "成交数量": "0", "成交均价": "", "合同编号": "1", "备注": "已报"}])
    r1 = _active([{"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
                   "成交数量": "100", "成交均价": "1699.800", "合同编号": "1", "备注": "已成"}])
    backend = WatchFakeBackend([r0, r1])

    # Client that raises on first send_frame call
    class FailingClient:
        def __init__(self, backend):
            self.backend = backend
            self.sent = []
            self._call_count = 0

        async def send_frame(self, frame):
            self._call_count += 1
            if self._call_count == 1:
                raise RuntimeError("simulated send_frame failure")
            self.sent.append(frame)

    async def drive():
        failing_client = FailingClient(backend)

        # Round 1: establish baseline from r0
        prev, seq, ok = await order_watch._poll_once(backend, failing_client, None, 0)
        assert ok is True
        assert set(prev) == {"1"}
        baseline_snapshot = prev

        # Round 2: try to send filled event (r0→r1), but send_frame raises
        prev_after, seq_after, ok_after = await order_watch._poll_once(
            backend, failing_client, prev, seq
        )
        assert ok_after is False, "should return False when send fails"
        assert (
            prev_after is baseline_snapshot
        ), "baseline should not advance on send failure"
        assert len(failing_client.sent) == 0, "no events should be sent on failure"

        # Round 3: same baseline with new working client re-emits the event
        working_client = WatchFakeClient(backend)
        prev_retry, seq_retry, ok_retry = await order_watch._poll_once(
            backend, working_client, prev_after, seq_after
        )
        assert ok_retry is True, "should succeed on retry"
        assert len(working_client.sent) == 1, "event should be sent on retry"
        assert working_client.sent[0]["event"] == "filled"
        assert working_client.sent[0]["entrust_no"] == "1"

    asyncio.run(drive())
