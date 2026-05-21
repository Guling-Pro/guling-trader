"""dispatcher.handle_call 信封 + 状态透传回归测试。

覆盖 2026-05-21 汤姆猫卖单事故的两个根因：
- Bug 4：reply 帧曾被 ws_client 双层包裹 → 外层永远 ok:true，掩盖真实失败。
  这里锁定 dispatcher 只产出"单层"reply 帧（含 id/ok/result|error），ws_client
  直接转发即可。
- Bug 1：非 code:0 的结果曾一律塌缩成"未知错误"。这里锁定 code/status/msg 被透传，
  且 code:2（已提交未确认）给出明确不要重复下单的语义。

均为同步测试，用 asyncio.run 驱动 async handle_call，避免依赖 pytest-asyncio。
"""
import asyncio

from trader import dispatcher


class FakeBackend:
    """按方法名返回预置 result dict 的假后端。"""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def _run(self, name, *args):
        self.calls.append((name, args))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def balance(self):
        return await self._run("balance")

    async def position(self):
        return await self._run("position")

    async def orders_active(self):
        return await self._run("orders_active")

    async def orders_filled(self):
        return await self._run("orders_filled")

    async def settlement(self, date_range="近一年"):
        return await self._run("settlement", date_range)

    async def buy(self, stock_no, amount, price, client_order_id):
        return await self._run("buy", stock_no, amount, price)

    async def sell(self, stock_no, amount, price, client_order_id):
        return await self._run("sell", stock_no, amount, price)

    async def cancel(self, entrust_no):
        return await self._run("cancel", entrust_no)


def _call(frame, result):
    backend = FakeBackend(result)
    reply = asyncio.run(dispatcher.handle_call(frame, backend))
    return reply, backend


def test_success_is_single_layer_with_id_echoed():
    """code:0 → ok:true，result 就是后端原始 dict（不再多嵌一层 reply 帧）。"""
    frame = {"type": "call", "id": "abc-123", "method": "balance", "params": {}}
    reply, _ = _call(frame, {"code": 0, "status": "succeed", "data": {"可用": "295.38"}})

    assert reply["type"] == "reply"
    assert reply["id"] == "abc-123"          # id 必须回显（旧实现内层 id=null）
    assert reply["ok"] is True
    # result 直接是后端 dict，而不是 {"type":"reply", ...} 这样的再包一层。
    assert reply["result"]["code"] == 0
    assert reply["result"]["data"]["可用"] == "295.38"
    assert reply["result"].get("type") != "reply"


def test_submitted_but_unconfirmed_code2_is_not_unknown_error():
    """code:2（已提交未确认）必须给出明确文案 + 透传 result，绝不能塌成'未知错误'。"""
    frame = {"type": "call", "id": "id2", "method": "sell",
             "params": {"stock_no": "300459", "amount": 100}}
    result = {"code": 2, "status": "unknown",
              "msg": "已提交但未能在 orders/active 表中匹配到对应订单，请自行确认状态"}
    reply, _ = _call(frame, result)

    assert reply["ok"] is False
    assert reply["error"] == result["msg"]      # 用 msg，不是 "未知错误"
    assert "未知错误" not in reply["error"]
    assert reply["result"]["code"] == 2          # 透传，供 agent 区分"已提交"vs"被拒"


def test_failed_query_propagates_msg_not_unknown_error():
    """读列表失败（code:1 带 msg）应透传 msg，不再是裸的'未知错误'。"""
    frame = {"type": "call", "id": "id3", "method": "orders_active", "params": {}}
    result = {"code": 1, "status": "failed", "msg": "读取数据失败（可能验证码弹窗或刷新超时），请稍后重试"}
    reply, _ = _call(frame, result)

    assert reply["ok"] is False
    assert reply["error"] == result["msg"]
    assert reply["result"]["status"] == "failed"


def test_failed_without_detail_falls_back_to_unknown():
    """既无 error 又无 msg 时才允许回退到'未知错误'。"""
    frame = {"type": "call", "id": "id4", "method": "position", "params": {}}
    reply, _ = _call(frame, {"code": 1})
    assert reply["ok"] is False
    assert reply["error"] == "未知错误"


def test_explicit_error_key_is_preferred():
    frame = {"type": "call", "id": "id5", "method": "buy",
             "params": {"stock_no": "600000", "amount": 100}}
    reply, _ = _call(frame, {"code": 1, "error": "可用资金不足"})
    assert reply["ok"] is False
    assert reply["error"] == "可用资金不足"


def test_method_not_whitelisted():
    frame = {"type": "call", "id": "id6", "method": "evil", "params": {}}
    reply, _ = _call(frame, {"code": 0})
    assert reply["ok"] is False
    assert "不支持" in reply["error"]
    assert reply["id"] == "id6"


def test_backend_exception_is_caught():
    frame = {"type": "call", "id": "id7", "method": "sell",
             "params": {"stock_no": "300459", "amount": 100}}
    reply, _ = _call(frame, RuntimeError("窗口未找到"))
    assert reply["ok"] is False
    assert "窗口未找到" in reply["error"]
    assert reply["id"] == "id7"


def test_settlement_routes_and_forwards_date_range():
    """交割单：dispatcher 路由到 backend.settlement 并透传 date_range。"""
    frame = {"type": "call", "id": "s1", "method": "settlement",
             "params": {"date_range": "近一年"}}
    reply, backend = _call(frame, {"code": 0, "status": "succeed", "data": [], "count": 0})
    assert reply["ok"] is True
    name, args = backend.calls[-1]
    assert name == "settlement"
    assert args == ("近一年",)


def test_settlement_default_date_range():
    """不传 date_range 时默认近一年。"""
    frame = {"type": "call", "id": "s2", "method": "settlement", "params": {}}
    _, backend = _call(frame, {"code": 0, "data": []})
    assert backend.calls[-1] == ("settlement", ("近一年",))


def test_buy_params_forwarded_to_backend():
    """确认 price 透传——市价单(price 缺省→None)不会被 dispatcher 篡改。"""
    frame = {"type": "call", "id": "id8", "method": "sell",
             "params": {"stock_no": "300459", "amount": 100}}
    _, backend = _call(frame, {"code": 0})
    name, args = backend.calls[-1]
    assert name == "sell"
    assert args == ("300459", 100, None)   # price 缺省 → None（市价语义）
