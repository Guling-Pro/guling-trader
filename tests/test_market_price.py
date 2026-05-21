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
