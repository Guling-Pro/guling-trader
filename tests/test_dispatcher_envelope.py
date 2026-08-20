"""dispatcher.handle_call 信封 + 状态透传回归测试。

覆盖 2026-05-21 汤姆猫卖单事故的两个根因：
- Bug 4：reply 帧曾被 ws_client 双层包裹 → 外层永远 ok:true，掩盖真实失败。
  这里锁定 dispatcher 只产出"单层"reply 帧（含 id/ok/result|error），ws_client
  直接转发即可。
- Bug 1：失败结果曾一律塌缩成"未知错误"。这里锁定契约 v2 信封被原样透传，
  且 submitted_unconfirmed（已提交未确认）给出明确不要重复下单的语义。

均为同步测试，用 asyncio.run 驱动 async handle_call，避免依赖 pytest-asyncio。
"""
import asyncio

from trader import contract, dispatcher


class FakeBackend:
    """按方法名返回预置 result dict 的假后端。"""

    def __init__(self, result):
        self._result = result
        self.calls = []
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()

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

    async def switch_account(self, slot):
        return await self._run("switch_account", slot)


def _call(frame, result):
    backend = FakeBackend(result)
    reply = asyncio.run(dispatcher.handle_call(frame, backend))
    return reply, backend


def test_success_is_single_layer_with_id_echoed():
    """code:0 → ok:true，result 就是后端原始 dict（不再多嵌一层 reply 帧）。"""
    frame = {"type": "call", "id": "abc-123", "method": "balance", "params": {}}
    reply, _ = _call(frame, contract.ok({"可用金额": 295.38}))

    assert reply["type"] == "reply"
    assert reply["id"] == "abc-123"          # id 必须回显（旧实现内层 id=null）
    assert reply["ok"] is True
    # result 直接是后端 dict，而不是 {"type":"reply", ...} 这样的再包一层。
    assert reply["result"]["status"] == "succeed"
    assert reply["result"]["code"] == "ok"
    assert reply["result"]["contract_version"] == "2"
    assert reply["result"]["data"]["可用金额"] == 295.38
    assert reply["result"].get("type") != "reply"


def test_submitted_unconfirmed_is_not_unknown_error():
    """已提交未确认必须给出明确文案 + 透传信封，绝不能塌成'未知错误'。"""
    frame = {"type": "call", "id": "id2", "method": "sell",
             "params": {"stock_no": "300459", "amount": 100}}
    result = contract.submitted_unconfirmed("已提交但未能在委托表中匹配到对应订单",
                                            data={"submitted": True})
    reply, _ = _call(frame, result)

    assert reply["ok"] is False
    assert reply["error"] == result["error"]["message"]
    assert "未知错误" not in reply["error"]
    # 供调用方区分「已提交」vs「被拒」：code + class 都是机器枚举
    assert reply["result"]["code"] == "submitted_unconfirmed"
    assert reply["result"]["error"]["class"] == "unknown_outcome"


def test_failed_query_propagates_msg_not_unknown_error():
    """读列表失败（code:1 带 msg）应透传 msg，不再是裸的'未知错误'。"""
    frame = {"type": "call", "id": "id3", "method": "orders_active", "params": {}}
    result = contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED,
                           "读取数据失败（可能验证码弹窗或刷新超时），请稍后重试")
    reply, _ = _call(frame, result)

    assert reply["ok"] is False
    assert reply["error"] == result["error"]["message"]
    assert reply["result"]["status"] == "failed"
    assert reply["result"]["code"] == "read_failed"


def test_non_contract_shape_is_rejected_loudly():
    """后端若返回非契约形态（老信封/裸 dict），dispatcher 必须转成 internal_error 信封，
    绝不放行——冻结后消费侧按契约解析，放行等于把解析崩溃推给对端。"""
    frame = {"type": "call", "id": "id4", "method": "position", "params": {}}
    reply, _ = _call(frame, {"code": 1})
    assert reply["ok"] is False
    assert reply["result"]["code"] == "internal_error"
    assert reply["result"]["contract_version"] == "2"


def test_broker_rejection_carries_class_and_raw_text():
    """柜台拒单：class 可机器分流，broker_msg 保留原文（C2 两层分类）。"""
    frame = {"type": "call", "id": "id5", "method": "buy",
             "params": {"stock_no": "600000", "amount": 100}}
    reply, _ = _call(frame, contract.broker_rejected("可用资金不足，无法委托"))
    assert reply["ok"] is False
    assert reply["result"]["error"]["class"] == "insufficient_funds"
    assert reply["result"]["error"]["broker_msg"] == "可用资金不足，无法委托"


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


def test_tools_list_returns_correct_schema():
    """验证 tools/list 返回完整的工具 Schema。"""
    frame = {"type": "call", "id": "t1", "method": "tools/list", "params": {}}
    reply, backend = _call(frame, {"code": 0})
    
    assert reply["ok"] is True
    assert reply["id"] == "t1"
    assert "tools" in reply["result"]
    
    tools = reply["result"]["tools"]
    assert len(tools) > 0
    # 验证 whitelisted 方法在 tools 中都有对应项
    tool_names = {t["name"] for t in tools}
    assert "balance" in tool_names
    assert "position" in tool_names
    assert "orders_active" in tool_names
    assert "orders_filled" in tool_names
    assert "settlement" in tool_names
    assert "buy" in tool_names
    assert "sell" in tool_names
    assert "cancel" in tool_names
    assert "switch_account" in tool_names

    # 确保没有多余的 code 字段嵌套在 result 中
    assert "code" not in reply["result"]


def test_switch_account_forwards_slot_and_single_layer_reply():
    """switch_account 路由到后端并透传 slot，回执保持单层信封。"""
    frame = {"type": "call", "id": "sw-1", "method": "switch_account",
             "params": {"slot": 2}}
    reply, backend = _call(frame, {"code": 0, "status": "succeed",
                                   "data": {"slot": 2}})

    assert reply["ok"] is True
    assert reply["id"] == "sw-1"
    assert reply["result"]["data"]["slot"] == 2
    assert backend.calls == [("switch_account", (2,))]


def test_fallback_schema_matches_file_schema():
    """验证内置的 FALLBACK_TOOLS_SCHEMA 与 docs/tools_schema.json 完全一致，防止三源漂移"""
    import json
    from pathlib import Path
    
    root = Path(__file__).resolve().parent.parent
    schema_path = root / "docs" / "tools_schema.json"
    
    assert schema_path.exists(), "docs/tools_schema.json 文件不存在！"
    
    with open(schema_path, "r", encoding="utf-8") as f:
        file_schema = json.load(f)
        
    assert dispatcher.FALLBACK_TOOLS_SCHEMA["tools"] == file_schema["tools"], (
        "内置 FALLBACK_TOOLS_SCHEMA 的 tools 列表与 docs/tools_schema.json 的 tools 列表不一致！"
        "更新接口定义时请确保两处同步修改。"
    )
