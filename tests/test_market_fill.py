"""市价单成交回执匹配（_match_market_fill）纯函数测试。

五档即成剩撤下完不留 orders_active，且可能部分成交 → 回执必须查成交表(orders_filled)
拿真实成交量/均价。这里只测前后差分 + 汇总逻辑，不触碰 Win32。
契约 v2：输入是**规范化后**的成交行（number/方向枚举），输出是统一信封。
"""
from trader.ths.win import _match_market_fill


def _row(code, op, qty, price, amt, sn):
    """规范化后的成交行（normalize_filled_row 的产物形状）。"""
    return {"证券代码": code, "方向": op, "成交数量": qty,
            "成交均价": price, "成交金额": amt, "成交编号": sn}


def test_full_fill_single_row():
    before = []
    after = [_row("600000", "买入", 100, 12.34, 1234.00, "A1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "succeed"
    assert r["data"]["fill_state"] == "filled"
    assert r["data"]["filled_amount"] == 100
    assert r["data"]["成交均价"] == 12.34
    assert r["data"]["方向"] == "买入"


def test_partial_fill_multi_row_weighted_avg():
    # 请求 300，两笔成交共 200 → 部分成交；均价按金额/数量加权。
    before = [_row("600000", "买入", 999, 9.999, 9989.00, "OLD")]
    after = [
        _row("600000", "买入", 999, 9.999, 9989.00, "OLD"),
        _row("600000", "买入", 100, 12.000, 1200.00, "A1"),
        _row("600000", "买入", 100, 12.500, 1250.00, "A2"),
    ]
    r = _match_market_fill(before, after, "600000", "买入", 300)
    assert r["status"] == "succeed"
    assert r["data"]["fill_state"] == "partially_filled"
    assert r["data"]["filled_amount"] == 200
    assert r["data"]["成交均价"] == 12.25  # (1200+1250)/200


def test_no_match_returns_unknown_outcome():
    """成交表里找不到本次成交 ⇒ 结果不可知，绝不当成功也绝不当明确失败。"""
    before = []
    after = [_row("000001", "买入", 100, 10.000, 1000.00, "X1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "failed"
    assert r["code"] == "submitted_unconfirmed"
    assert r["error"]["class"] == "unknown_outcome"
    assert r["data"]["filled_amount"] == 0


def test_ignores_opposite_op_same_code():
    before = []
    after = [_row("600000", "卖出", 100, 12.000, 1200.00, "S1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["code"] == "submitted_unconfirmed"


def test_null_numeric_row_is_skipped_not_counted_as_zero():
    """缺值是 null 不是 0：一行数量/金额读不到时跳过，不能把它算成 0 股成交。"""
    before = []
    after = [_row("600000", "买入", None, None, None, "A1"),
             _row("600000", "买入", 100, 12.00, 1200.00, "A2")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["data"]["filled_amount"] == 100
