"""市价单价格回归测试（Bug 5）。

事故：LLM 未传 price（市价意图）→ trader 收到 price=None → 旧代码强转成 0 →
_submit_trade 把价格框写成 "0.000" → 同花顺无法以 0.00 挂单。

正确行为：price=None 必须原样透传到 _do_sell/_do_buy，由 _submit_trade 跳过价格框，
沿用 xiadan 自动带出的对手价（即工具描述承诺的"对手价市价单"）。

这里只验证 async sell/buy → _do_* 的参数透传，不触碰 Win32（_ensure_bound 与
asyncio.to_thread 均被打桩）。
"""
import asyncio

import pytest

from trader.ths.win import WinThsBackend


def _drive(coro_factory):
    """打桩 _ensure_bound + asyncio.to_thread，跑一次 async 方法，返回捕获到的位置参数。"""
    backend = WinThsBackend()
    backend._ensure_bound = lambda: None  # 跳过窗口绑定（否则 Mac 上无 win32gui）

    captured = {}

    async def fake_to_thread(fn, *args):
        captured["fn"] = fn
        captured["args"] = args
        return {"code": 0, "status": "succeed"}

    import trader.ths.win as win_mod
    orig = win_mod.asyncio.to_thread
    win_mod.asyncio.to_thread = fake_to_thread
    try:
        asyncio.run(coro_factory(backend))
    finally:
        win_mod.asyncio.to_thread = orig
    return backend, captured


def test_sell_market_passes_none_price():
    backend, captured = _drive(lambda b: b.sell("300459", 100, None))
    assert captured["fn"] == backend._do_sell
    assert captured["args"] == ("300459", 100, None)   # 不是 0


def test_buy_market_passes_none_price():
    backend, captured = _drive(lambda b: b.buy("600000", 100, None))
    assert captured["fn"] == backend._do_buy
    assert captured["args"] == ("600000", 100, None)


def test_sell_limit_passes_through_price():
    backend, captured = _drive(lambda b: b.sell("300459", 100, 4.11))
    assert captured["args"] == ("300459", 100, 4.11)


@pytest.mark.parametrize("bad", [0, 0.0])
def test_zero_is_never_silently_substituted_for_none(bad):
    """显式 price=0 也只会原样透传（由上层校验），dispatcher/backend 不再制造 0。"""
    backend, captured = _drive(lambda b: b.sell("300459", 100, bad))
    assert captured["args"][2] == bad


# ── 市价/限价路径分派（_do_buy/_do_sell 内部按 price 有无分流）──────────────
def _stub_submits(backend):
    """打桩两条提交路径，返回记录调用的字典。"""
    calls = {}
    backend._submit_trade = lambda *a: calls.setdefault("limit", a) or {"code": 0}
    backend._submit_market_trade = lambda *a: calls.setdefault("market", a) or {"code": 0}
    return calls


def test_do_buy_none_price_routes_to_market():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_buy("600000", 100, None)
    assert "limit" not in calls
    assert calls["market"] == ("买入", "600000", 100)


def test_do_sell_none_price_routes_to_market():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_sell("300459", 200, None)
    assert calls["market"] == ("卖出", "300459", 200)


def test_do_buy_with_price_routes_to_limit():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_buy("600000", 100, 12.34)
    assert "market" not in calls
    assert calls["limit"] == ("F1", "买入", "600000", 100, 12.34)


def test_do_sell_with_price_routes_to_limit():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_sell("300459", 200, 4.11)
    assert calls["limit"] == ("F2", "卖出", "300459", 200, 4.11)
