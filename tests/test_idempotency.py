"""C5a 幂等 + C5b 查单：台账语义与 dispatcher 闸门。

核心承诺（也是唯一承诺）：**同 client_order_id 重发绝不产生第二次提交**。
返回的可能是首次成功回执，也可能是「首次结果未知」——后者是合法态，不是 bug：
最危险那一刻（点了提交、回执没回来）台账自己也不知道结果。
"""
import asyncio

import pytest

from trader import contract, dispatcher
from trader.config import EXTERNAL_CANCEL_CONFIRMATION_DIRECT, TraderConfig
from trader.order_ledger import LedgerUnavailable, OrderLedger


@pytest.fixture()
def ledger(tmp_path):
    return OrderLedger(tmp_path / "orders.db")


BUY_PARAMS = {"stock_no": "600000", "amount": 100, "price": 8.1}
EXTERNAL_ORDER = {
    "entrust_no": "777",
    "证券代码": "600000",
    "方向": "买入",
    "委托价": 8.1,
    "委托数量": 100,
    "已成数量": 0,
    "状态": "已报",
}


def coid(sequence: int) -> str:
    """符合新协议的测试 ID；仅 dispatcher 对外入口强制该格式。"""
    return f"gl-0198f6a1-{sequence:04x}-7000-8000-{sequence:012x}"


def _register_agent_order(ledger, sequence=90, entrust_no="777"):
    """登记原买卖单，模拟本系统此前成功下出的委托。"""
    order_id = coid(sequence)
    ledger.reserve(order_id, "buy", BUY_PARAMS)
    ledger.complete(order_id, contract.ok({"entrust_no": entrust_no}), entrust_no)
    return order_id


# --- 台账本身 ---------------------------------------------------------------

def test_reserve_then_duplicate(ledger):
    assert ledger.reserve(coid(1), "buy", BUY_PARAMS) == ("new", None)
    verdict, record = ledger.reserve(coid(1), "buy", BUY_PARAMS)
    assert verdict == "duplicate"
    assert record["state"] == "submitting"


def test_same_id_different_params_is_conflict(ledger):
    ledger.reserve(coid(1), "buy", BUY_PARAMS)
    verdict, _ = ledger.reserve(coid(1), "buy", {**BUY_PARAMS, "amount": 200})
    assert verdict == "conflict", "同 id 换参数必须拒绝，不能静默返回首次回执"


def test_complete_and_entrust_join(ledger):
    ledger.reserve(coid(1), "buy", BUY_PARAMS)
    ledger.complete(coid(1), contract.ok({"entrust_no": "777"}), "777")
    assert ledger.get(coid(1))["state"] == "done"
    assert ledger.coid_by_entrust() == {"777": coid(1)}


def test_survives_reopen(tmp_path):
    """落盘：受控端重启后幂等仍然成立（否则重发=重复下单）。"""
    path = tmp_path / "orders.db"
    OrderLedger(path).reserve(coid(1), "buy", BUY_PARAMS)
    verdict, _ = OrderLedger(path).reserve(coid(1), "buy", BUY_PARAMS)
    assert verdict == "duplicate"


def test_corrupt_ledger_raises_not_silently_degrades(tmp_path):
    bad = tmp_path / "orders.db"
    bad.write_bytes(b"this is not a sqlite file, not even close" * 10)
    with pytest.raises(LedgerUnavailable):
        OrderLedger(bad).reserve(coid(1), "buy", BUY_PARAMS)


# --- dispatcher 闸门 ---------------------------------------------------------

class OrderBackend:
    def __init__(self, ledger, result=None):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.degraded = False
        self.ledger = ledger
        self.submits = 0
        self._result = result or contract.ok({"entrust_no": "777"})
        self.active_queries = 0
        self.filled_queries = 0
        self.all_order_queries = 0
        self.calls: list[str] = []

    async def buy(self, stock_no, amount, price, client_order_id):
        self.submits += 1
        self.calls.append("buy")
        return self._result

    async def sell(self, stock_no, amount, price, client_order_id):
        self.submits += 1
        self.calls.append("sell")
        return self._result

    async def cancel(self, entrust_no):
        self.submits += 1
        self.calls.append("cancel")
        return self._result

    async def orders_active(self):
        self.active_queries += 1
        return contract.ok([])

    async def orders_filled(self):
        self.filled_queries += 1
        return contract.ok([])

    async def orders_active_all(self):
        self.all_order_queries += 1
        return contract.ok([])


def _buy(backend, coid, amount=100):
    frame = {"type": "call", "id": "x", "method": "buy",
             "params": {"stock_no": "600000", "amount": amount, "price": 8.1,
                        "client_order_id": coid}}
    return asyncio.run(dispatcher.handle_call(frame, backend))


def _cancel(backend, coid, entrust_no="777"):
    frame = {"type": "call", "id": "cancel", "method": "cancel",
             "params": {"entrust_no": entrust_no, "client_order_id": coid}}
    return asyncio.run(dispatcher.handle_call(frame, backend))


def _confirm_external_cancel(backend, coid, confirmation_token):
    frame = {
        "type": "call",
        "id": "confirm-cancel",
        "method": "confirm_external_cancel",
        "params": {
            "confirmation_token": confirmation_token,
            "client_order_id": coid,
        },
    }
    return asyncio.run(dispatcher.handle_call(frame, backend))


def test_resend_same_coid_never_submits_twice(ledger):
    backend = OrderBackend(ledger)
    first = _buy(backend, coid(1))
    second = _buy(backend, coid(1))

    assert backend.submits == 1, "同 coid 重发绝不能产生第二次提交"
    assert first["result"]["data"]["entrust_no"] == "777"
    assert second["result"]["data"]["entrust_no"] == "777"      # 返回首次回执
    assert second["result"]["data"]["idempotent_replay"] is True


def test_resend_after_unknown_outcome_returns_unknown_not_new_order(ledger):
    """首次结果不可知时，重发拿到的仍是「不可知」——契约不撒谎，但也绝不重下。"""
    backend = OrderBackend(ledger, contract.submitted_unconfirmed(
        "已提交但未能确认", data={"submitted": True}))
    first = _buy(backend, coid(2))
    second = _buy(backend, coid(2))
    assert backend.submits == 1
    assert backend.active_queries == 2
    assert backend.filled_queries == 2
    assert first["result"]["data"]["auto_query"]["code"] == "ok"
    assert first["result"]["data"]["auto_query"]["data"]["state"] == "未知"
    assert second["result"]["code"] == "submitted_unconfirmed"
    assert second["result"]["error"]["class"] == "unknown_outcome"
    assert second["result"]["data"]["idempotent_replay"] is True
    assert second["result"]["data"]["auto_query"]["code"] == "ok"


def test_same_coid_different_params_rejected(ledger):
    backend = OrderBackend(ledger)
    _buy(backend, coid(3), amount=100)
    other = _buy(backend, coid(3), amount=200)
    assert backend.submits == 1
    assert other["result"]["code"] == "invalid_params"
    assert other["result"]["data"]["submitted"] is False


def test_ledger_unavailable_rejects_order(tmp_path):
    """台账不可用一律拒单，禁静默降级为无幂等下单。"""
    class NoLedgerBackend(OrderBackend):
        ledger = None

    backend = NoLedgerBackend.__new__(NoLedgerBackend)
    OrderBackend.__init__(backend, None)
    reply = _buy(backend, coid(4))
    assert backend.submits == 0
    assert reply["result"]["code"] == "ledger_unavailable"
    assert reply["result"]["error"]["class"] == "ledger_unavailable"


@pytest.mark.parametrize("method, params", [
    ("buy", {"stock_no": "600000", "amount": 100, "price": 8.1}),
    ("sell", {"stock_no": "600000", "amount": 100, "price": 8.1}),
    ("cancel", {"entrust_no": "777"}),
])
@pytest.mark.parametrize("value", [
    None,
    "",
    "   ",
    "gl-1",
    "GL-0198f6a1-0001-7000-8000-000000000001",
    "gl-0198f6a1-0001-6000-8000-000000000001",
    "gl-0198f6a1-0001-7000-7000-000000000001",
    "gl-0198f6a1-0001-7000-8000-000000000001 ",
])
def test_order_requires_canonical_uuid_v7(ledger, method, params, value):
    """非法幂等键绝不允许接触交易端，包括看似 UUID 但版本或 variant 错误的值。"""
    backend = OrderBackend(ledger)
    frame = {"type": "call", "id": "x", "method": method,
             "params": {**params,
                        **({} if value is None else {"client_order_id": value})}}
    reply = asyncio.run(dispatcher.handle_call(frame, backend))
    assert reply["ok"] is False
    assert reply["result"]["code"] == "invalid_params"
    assert reply["result"]["data"]["submitted"] is False
    assert backend.submits == 0


@pytest.mark.parametrize("method, params", [
    ("buy", {"stock_no": "600000", "amount": 100, "price": 8.1}),
    ("sell", {"stock_no": "600000", "amount": 100, "price": 8.1}),
    ("cancel", {"entrust_no": "777"}),
])
def test_order_accepts_canonical_uuid_v7(ledger, method, params):
    backend = OrderBackend(ledger)
    if method == "cancel":
        _register_agent_order(ledger)
    frame = {"type": "call", "id": "x", "method": method,
             "params": {**params, "client_order_id": coid(10)}}
    reply = asyncio.run(dispatcher.handle_call(frame, backend))
    assert reply["ok"] is True
    assert backend.calls == [method]


def test_unconfirmed_cancel_auto_verifies_target_without_resubmitting(ledger):
    """撤单未知时读全量委托表核验目标单，绝不再次点击撤单。"""
    class B(OrderBackend):
        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok([{
                **EXTERNAL_ORDER,
                "状态": "已撤",
            }])

    _register_agent_order(ledger)
    backend = B(ledger, contract.submitted_unconfirmed(
        "撤单已提交但尚未确认", data={"submitted": True}))
    first = _cancel(backend, coid(11))
    second = _cancel(backend, coid(11))
    assert first["result"]["code"] == "submitted_unconfirmed"
    assert second["result"]["code"] == "submitted_unconfirmed"
    assert first["result"]["data"]["auto_query"]["data"]["cancel_state"] == "已撤"
    assert second["result"]["data"]["auto_query"]["data"]["cancel_state"] == "已撤"
    assert backend.submits == 1
    assert backend.active_queries == 0
    assert backend.filled_queries == 0
    assert backend.all_order_queries == 2


def test_external_cancel_refreshes_prompt_without_persisting_or_reusing_token(ledger):
    """提示重放只换令牌，既不点击 GUI，也不把授权令牌写进台账。"""
    class B(OrderBackend):
        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok([EXTERNAL_ORDER])

    backend = B(ledger)
    first = _cancel(backend, coid(18))
    second = _cancel(backend, coid(18))

    assert first["ok"] is False
    assert first["result"]["code"] == "confirmation_required"
    assert first["result"]["error"]["class"] == "confirmation_required"
    assert first["result"]["data"]["submitted"] is False
    assert first["result"]["data"]["order"] == EXTERNAL_ORDER
    first_token = first["result"]["data"]["confirmation_token"]
    second_token = second["result"]["data"]["confirmation_token"]
    assert second_token != first_token
    stored_receipt = ledger.get(coid(18))["receipt"]
    assert "confirmation_token" not in stored_receipt["data"]

    stale = _confirm_external_cancel(backend, coid(33), first_token)
    assert stale["result"]["data"]["confirmation_state"] == "missing"
    assert backend.calls == []

    confirmed = _confirm_external_cancel(backend, coid(34), second_token)
    assert confirmed["ok"] is True
    assert backend.calls == ["cancel"]
    assert backend.all_order_queries == 3


def test_external_cancel_confirmation_rereads_and_records_target_for_query(ledger):
    """确认必须二次读表后才撤，并把目标编号写入确认动作台账。"""
    class B(OrderBackend):
        def __init__(self, ledger):
            super().__init__(ledger)
            self.rows = [dict(EXTERNAL_ORDER)]

        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok(self.rows)

    backend = B(ledger)
    prompt = _cancel(backend, coid(19))
    token = prompt["result"]["data"]["confirmation_token"]
    confirmed = _confirm_external_cancel(backend, coid(20), token)

    assert confirmed["ok"] is True
    assert backend.calls == ["cancel"]
    assert backend.all_order_queries == 2
    assert ledger.get(coid(20))["entrust_no"] == "777"

    backend.rows = [{**EXTERNAL_ORDER, "状态": "已撤"}]
    queried = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "query", "method": "query_order",
         "params": {"client_order_id": coid(20)}}, backend,
    ))
    assert queried["result"]["data"]["resolution"] == "by_entrust_no"
    assert queried["result"]["data"]["cancel_state"] == "已撤"


def test_external_cancel_rejects_reused_token_without_second_click(ledger):
    class B(OrderBackend):
        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok([EXTERNAL_ORDER])

    backend = B(ledger)
    token = _cancel(backend, coid(21))["result"]["data"]["confirmation_token"]
    assert _confirm_external_cancel(backend, coid(22), token)["ok"] is True
    repeated = _confirm_external_cancel(backend, coid(23), token)

    assert repeated["result"]["code"] == "confirmation_required"
    assert repeated["result"]["data"]["confirmation_state"] == "used"
    assert backend.calls == ["cancel"]


def test_external_cancel_stops_when_order_changes_between_prompt_and_confirmation(ledger):
    class B(OrderBackend):
        def __init__(self, ledger):
            super().__init__(ledger)
            self.rows = [dict(EXTERNAL_ORDER)]

        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok(self.rows)

    backend = B(ledger)
    token = _cancel(backend, coid(24))["result"]["data"]["confirmation_token"]
    backend.rows = [{**EXTERNAL_ORDER, "已成数量": 10, "状态": "部成"}]
    changed = _confirm_external_cancel(backend, coid(25), token)

    assert changed["result"]["code"] == "confirmation_required"
    assert changed["result"]["data"]["submitted"] is False
    assert changed["result"]["data"]["current_order"]["已成数量"] == 10
    assert backend.calls == []


def test_external_cancel_rejects_expired_token_without_clicking(ledger, monkeypatch):
    class B(OrderBackend):
        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok([EXTERNAL_ORDER])

    monkeypatch.setattr(dispatcher, "EXTERNAL_CANCEL_CONFIRMATION_TTL_SECS", -1.0)
    backend = B(ledger)
    token = _cancel(backend, coid(26))["result"]["data"]["confirmation_token"]
    expired = _confirm_external_cancel(backend, coid(27), token)

    assert expired["result"]["code"] == "confirmation_required"
    assert expired["result"]["data"]["confirmation_state"] == "expired"
    assert backend.calls == []


def test_external_cancel_connection_reset_invalidates_pending_token(ledger):
    class B(OrderBackend):
        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok([EXTERNAL_ORDER])

    backend = B(ledger)
    token = _cancel(backend, coid(30))["result"]["data"]["confirmation_token"]
    dispatcher.clear_external_cancel_confirmations()
    invalidated = _confirm_external_cancel(backend, coid(31), token)

    assert invalidated["result"]["code"] == "confirmation_required"
    assert invalidated["result"]["data"]["confirmation_state"] == "missing"
    assert backend.calls == []

    refreshed = _cancel(backend, coid(30))
    refreshed_token = refreshed["result"]["data"]["confirmation_token"]
    assert refreshed_token != token
    assert backend.calls == []
    assert backend.all_order_queries == 2


def test_external_cancel_read_timeout_is_never_reported_as_submitted(ledger, monkeypatch):
    """确认前读表超时没有调用 backend.cancel，不能伪装为未知的真实撤单。"""
    class B(OrderBackend):
        async def orders_active_all(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(dispatcher, "CALL_TIMEOUT_SECS", 0.01)
    backend = B(ledger)
    timed_out = _cancel(backend, coid(32))

    assert timed_out["result"]["code"] == "call_timeout"
    assert timed_out["result"]["data"]["submitted"] is False
    assert backend.calls == []


def test_registered_cancel_skips_confirmation_and_does_not_read_full_table(ledger):
    class B(OrderBackend):
        async def orders_active_all(self):
            pytest.fail("已登记订单不应进入人工订单全量表确认路径")

    _register_agent_order(ledger)
    backend = B(ledger)
    reply = _cancel(backend, coid(28))

    assert reply["ok"] is True
    assert backend.calls == ["cancel"]


def test_external_cancel_direct_mode_skips_confirmation(ledger, monkeypatch):
    class B(OrderBackend):
        async def orders_active_all(self):
            pytest.fail("direct 模式不应读取人工订单确认表")

    monkeypatch.setattr(
        dispatcher._config,
        "load",
        lambda: TraderConfig(
            device_id="",
            external_cancel_confirmation=EXTERNAL_CANCEL_CONFIRMATION_DIRECT,
        ),
    )
    backend = B(ledger)
    reply = _cancel(backend, coid(29))

    assert reply["ok"] is True
    assert backend.calls == ["cancel"]


def test_auto_query_timeout_preserves_unknown_without_resubmitting(ledger, monkeypatch):
    """自动核单失败只能作为附加证据，绝不能覆盖原回执或补发下单。"""
    monkeypatch.setattr(dispatcher, "AUTO_QUERY_TIMEOUT_SECS", 0.01)

    class SlowQueryBackend(OrderBackend):
        async def orders_active(self):
            self.active_queries += 1
            await asyncio.sleep(3600)

    backend = SlowQueryBackend(ledger, contract.submitted_unconfirmed(
        "已提交但未能确认", data={"submitted": True}))
    reply = _buy(backend, coid(12))
    result = reply["result"]
    assert result["code"] == "submitted_unconfirmed"
    assert result["error"]["class"] == "unknown_outcome"
    assert result["data"]["auto_query"]["code"] == "call_timeout"
    assert backend.calls == ["buy"]
    assert backend.submits == 1
    assert backend.degraded is True


# --- C5b query_order ---------------------------------------------------------

def test_query_order_resolves_by_entrust_no(ledger):
    ledger.reserve(coid(5), "buy", BUY_PARAMS)
    ledger.complete(coid(5), contract.ok({"entrust_no": "777"}), "777")

    class B(OrderBackend):
        async def orders_active(self):
            return contract.ok([{"entrust_no": "777", "证券代码": "600000",
                                 "委托数量": 100, "状态": "已报"}])

    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": coid(5)}}, B(ledger)))
    data = reply["result"]["data"]
    assert data["state"] == "已报"
    assert data["resolution"] == "by_entrust_no"


def test_query_order_unresolved_when_ambiguous(ledger):
    """entrust_no 未知 + 实表有两笔同参单 → 不猜，报未知（需人工）。"""
    ledger.reserve(coid(6), "buy", BUY_PARAMS)

    class B(OrderBackend):
        async def orders_active(self):
            return contract.ok([
                {"entrust_no": "1", "证券代码": "600000", "方向": "买入",
                 "委托数量": 100, "委托价": 8.1, "状态": "已报"},
                {"entrust_no": "2", "证券代码": "600000", "方向": "买入",
                 "委托数量": 100, "委托价": 8.1, "状态": "已报"},
            ])

    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": coid(6)}}, B(ledger)))
    data = reply["result"]["data"]
    assert data["state"] == "未知"
    assert data["resolution"] == "unresolved"


def test_query_order_unknown_coid_is_not_found(ledger):
    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": "never-seen"}}, OrderBackend(ledger)))
    assert reply["result"]["code"] == "not_found"


def test_query_order_keeps_legacy_id_readable(ledger):
    """格式升级不能让历史台账里的未知订单无法核查。"""
    legacy_id = "legacy-coid"
    ledger.reserve(legacy_id, "buy", BUY_PARAMS)
    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": legacy_id}}, OrderBackend(ledger)))
    assert reply["result"]["code"] == "ok"
    assert reply["result"]["data"]["client_order_id"] == legacy_id


@pytest.mark.parametrize(("row", "expected"), [
    ({"entrust_no": "777", "委托数量": 100, "已成数量": 0, "状态": "已撤"}, "已撤"),
    ({"entrust_no": "777", "委托数量": 100, "已成数量": 20, "状态": "已撤"}, "部成后已撤"),
    ({"entrust_no": "777", "委托数量": 100, "已成数量": 100, "状态": "已成"}, "已成"),
    ({"entrust_no": "777", "委托数量": 100, "已成数量": 20, "状态": "部成"}, "仍在飞"),
    ({"entrust_no": "777", "委托数量": 100, "已成数量": 0, "状态": "废单"}, "废单"),
])
def test_query_cancel_resolves_terminal_or_in_flight_state(ledger, row, expected):
    """撤单 ID 必须按目标 entrust_no 查全量表，不能从成交表推断已撤。"""
    query_id = coid(13)
    ledger.reserve(query_id, "cancel", {"entrust_no": "777"})

    class B(OrderBackend):
        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.ok([row])

    backend = B(ledger)
    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": query_id}}, backend))
    data = reply["result"]["data"]
    assert data["resolution"] == "by_entrust_no"
    assert data["cancel_state"] == expected
    assert data["entrust_no"] == "777"
    assert backend.all_order_queries == 1
    assert backend.active_queries == 0
    assert backend.filled_queries == 0


def test_query_cancel_is_unknown_when_full_order_table_lacks_target(ledger):
    query_id = coid(14)
    ledger.reserve(query_id, "cancel", {"entrust_no": "777"})
    backend = OrderBackend(ledger)

    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": query_id}}, backend))
    data = reply["result"]["data"]
    assert data["resolution"] == "unresolved"
    assert data["cancel_state"] == "未知"
    assert data["tables_readable"] is True


def test_query_cancel_is_unknown_when_full_order_table_is_unreadable(ledger):
    query_id = coid(17)
    ledger.reserve(query_id, "cancel", {"entrust_no": "777"})

    class B(OrderBackend):
        async def orders_active_all(self):
            self.all_order_queries += 1
            return contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED, "验证码弹窗")

    backend = B(ledger)
    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": query_id}}, backend))
    data = reply["result"]["data"]
    assert data["resolution"] == "unresolved"
    assert data["cancel_state"] == "未知"
    assert data["tables_readable"] is False


def test_cancel_target_does_not_overwrite_original_order_id_join(ledger):
    """历史撤单台账也不得让撤单 ID 覆盖原买卖单的表格回显关联。"""
    buy_id, cancel_id = coid(15), coid(16)
    ledger.reserve(buy_id, "buy", BUY_PARAMS)
    ledger.complete(buy_id, contract.ok({"entrust_no": "777"}), "777")
    ledger.reserve(cancel_id, "cancel", {"entrust_no": "777"})
    ledger.complete(cancel_id, contract.ok({"entrust_no": "777"}), "777")

    assert ledger.coid_by_entrust() == {"777": buy_id}


@pytest.mark.parametrize("row", [
    {"证券代码": "600000", "方向": "卖出", "委托数量": 100, "委托价": 8.1, "状态": "已报"},
    {"证券代码": "600000", "方向": "买入", "委托数量": 100, "委托价": 8.2, "状态": "已报"},
])
def test_query_order_heuristic_rejects_wrong_direction_or_limit_price(ledger, row):
    """外部同代码单不得因方向相反或限价不同而被归因为本单。"""
    query_id = coid(7)
    ledger.reserve(query_id, "buy", BUY_PARAMS)

    class B(OrderBackend):
        async def orders_active(self):
            return contract.ok([row])

    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": query_id}}, B(ledger)))
    data = reply["result"]["data"]
    assert data["state"] == "未知"
    assert data["resolution"] == "unresolved"


def test_query_order_heuristic_matches_unique_full_limit_fingerprint(ledger):
    query_id = coid(8)
    ledger.reserve(query_id, "buy", BUY_PARAMS)

    class B(OrderBackend):
        async def orders_active(self):
            return contract.ok([{
                "entrust_no": "888", "证券代码": "600000", "方向": "买入",
                "委托数量": 100, "委托价": 8.1, "状态": "已报",
            }])

    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": query_id}}, B(ledger)))
    data = reply["result"]["data"]
    assert data["state"] == "已报"
    assert data["resolution"] == "heuristic"
