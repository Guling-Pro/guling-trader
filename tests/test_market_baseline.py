"""市价单回执基线：读不到成交表就不许下单。

市价单的成交量/均价靠下单前后的成交表差分得出（认「after 里 before 没有的行」）。
基线拿不到时若以空基线继续，当日同股同向的历史成交会被算成本次成交——污染的是
真钱 sizing 的输入，而市价单发出去无法回收。所以：基线失败=硬失败、绝不提交。
"""
from trader.ths import win as w
from trader.ths.win import WinThsBackend


def _backend(monkeypatch, pre_result):
    b = WinThsBackend()
    b.hwnd_main = 1
    calls = []
    monkeypatch.setattr(b, "switch_to_normal", lambda: None)
    monkeypatch.setattr(b, "get_filled_orders", lambda: pre_result)
    monkeypatch.setattr(b, "_select_tree_path",
                        lambda path, **kwargs: calls.append(("navigate", path)) or True)
    monkeypatch.setattr(w, "_activate_window", lambda hwnd: None)
    monkeypatch.setattr(w, "sleep_time", 0)
    return b, calls


def test_market_order_aborts_when_baseline_unreadable(monkeypatch):
    from trader import contract
    b, calls = _backend(monkeypatch, contract.fail(
        contract.CODE_READ_FAILED, contract.CLS_READ_FAILED, "读取数据失败"))
    r = b._submit_market_trade("买入", "300458", 500)
    assert r["status"] == "failed"
    assert r["data"]["submitted"] is False
    assert "基线" in r["error"]["message"]
    assert calls == [], "基线失败后绝不能继续走到下单面板"


def test_wrong_table_baseline_also_aborts(monkeypatch):
    """表头校验拦下的错表同样算基线失败——错表当基线比没有基线更糟。"""
    from trader import contract
    b, calls = _backend(monkeypatch, contract.fail(
        contract.CODE_TABLE_MISMATCH, contract.CLS_TABLE_MISMATCH,
        "成交查询：抓到的不是本次请求的表（命中他表特征列 ['股票余额']）",
        data={"got_columns": ["股票余额"]}))
    r = b._submit_market_trade("卖出", "300458", 500)
    assert r["status"] == "failed"
    assert calls == []


def test_empty_filled_table_is_a_valid_market_order_baseline(monkeypatch):
    """成功读取到空成交表就是合法基线，不能误判为读取失败。"""
    from trader import contract
    b, calls = _backend(monkeypatch, contract.ok([]))
    monkeypatch.setattr(
        b, "_select_tree_path",
        lambda path, **kwargs: calls.append(("navigate", path)) or False,
    )

    r = b._submit_market_trade("买入", "300458", 500)

    assert r["status"] == "failed"
    assert "未能导航到市价委托面板" in r["error"]["message"]
    assert calls == [("navigate", ("市价买入",))], "空成交表必须通过基线校验，继续进入下单面板"


def test_verified_no_header_empty_active_table_is_valid_limit_baseline(monkeypatch):
    """同花顺空委托表不复制表头时，已通过验证码核验的空结果可作空基线。"""
    from trader import contract

    b = WinThsBackend()
    monkeypatch.setattr(b, "get_active_orders_all", lambda: contract.ok([]))
    b._last_grid_verified_empty.add("active_orders")

    baseline, error = b._read_limit_order_baseline()

    assert error is None
    assert baseline == set()


def test_unverified_no_header_active_table_still_aborts_limit_baseline(monkeypatch):
    """普通读取失败不能借空表语义绕过限价单的归属保护。"""
    from trader import contract

    b = WinThsBackend()
    monkeypatch.setattr(b, "get_active_orders_all", lambda: contract.ok([]))

    baseline, error = b._read_limit_order_baseline()

    assert baseline is None
    assert error["status"] == "failed"
    assert error["code"] == "table_mismatch"


def test_market_sell_uses_verified_top_level_entry(monkeypatch):
    """卖出也必须选中实机顶层“市价卖出”，不得落到普通卖出 F2。"""
    from trader import contract
    b, calls = _backend(monkeypatch, contract.ok([]))
    monkeypatch.setattr(
        b, "_select_tree_path",
        lambda path, **kwargs: calls.append(("navigate", path)) or False,
    )

    r = b._submit_market_trade("卖出", "300458", 500)

    assert r["status"] == "failed"
    assert calls == [("navigate", ("市价卖出",))]
