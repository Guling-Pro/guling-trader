"""市价单成交回执匹配（_match_market_fill）纯函数测试。

五档即成剩撤下完不留 orders_active，且可能部分成交 → 回执必须查成交表(orders_filled)
拿真实成交量/均价。这里只测前后差分 + 汇总逻辑，不触碰 Win32。
"""
from trader.ths.win import _match_market_fill


def _row(code, op, qty, price, amt, sn):
    return {"证券代码": code, "操作": op, "成交数量": qty,
            "成交均价": price, "成交金额": amt, "成交编号": sn}


def test_full_fill_single_row():
    before = []
    after = [_row("600000", "证券买入", "100", "12.340", "1234.00", "A1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "filled"
    assert r["filled_amount"] == 100
    assert r["avg_price"] == 12.34
    assert r["op"] == "买入"


def test_partial_fill_multi_row_weighted_avg():
    # 请求 300，两笔成交共 200 → 部分成交；均价按金额/数量加权。
    before = [_row("600000", "证券买入", "999", "9.999", "9989.00", "OLD")]
    after = [
        _row("600000", "证券买入", "999", "9.999", "9989.00", "OLD"),
        _row("600000", "证券买入", "100", "12.000", "1200.00", "A1"),
        _row("600000", "证券买入", "100", "12.500", "1250.00", "A2"),
    ]
    r = _match_market_fill(before, after, "600000", "买入", 300)
    assert r["status"] == "partially_filled"
    assert r["filled_amount"] == 200
    assert r["avg_price"] == 12.25  # (1200+1250)/200


def test_no_match_returns_unknown():
    before = []
    after = [_row("000001", "证券买入", "100", "10.000", "1000.00", "X1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "unknown"
    assert r["filled_amount"] == 0


def test_ignores_opposite_op_same_code():
    before = []
    after = [_row("600000", "证券卖出", "100", "12.000", "1200.00", "S1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "unknown"
