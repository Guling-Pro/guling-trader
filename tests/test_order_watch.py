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
