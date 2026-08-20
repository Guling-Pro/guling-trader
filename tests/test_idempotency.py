"""C5a 幂等 + C5b 查单：台账语义与 dispatcher 闸门。

核心承诺（也是唯一承诺）：**同 client_order_id 重发绝不产生第二次提交**。
返回的可能是首次成功回执，也可能是「首次结果未知」——后者是合法态，不是 bug：
最危险那一刻（点了提交、回执没回来）台账自己也不知道结果。
"""
import asyncio

import pytest

from trader import contract, dispatcher
from trader.order_ledger import LedgerUnavailable, OrderLedger


@pytest.fixture()
def ledger(tmp_path):
    return OrderLedger(tmp_path / "orders.db")


BUY_PARAMS = {"stock_no": "600000", "amount": 100, "price": 8.1}


# --- 台账本身 ---------------------------------------------------------------

def test_reserve_then_duplicate(ledger):
    assert ledger.reserve("gl-1", "buy", BUY_PARAMS) == ("new", None)
    verdict, record = ledger.reserve("gl-1", "buy", BUY_PARAMS)
    assert verdict == "duplicate"
    assert record["state"] == "submitting"


def test_same_id_different_params_is_conflict(ledger):
    ledger.reserve("gl-1", "buy", BUY_PARAMS)
    verdict, _ = ledger.reserve("gl-1", "buy", {**BUY_PARAMS, "amount": 200})
    assert verdict == "conflict", "同 id 换参数必须拒绝，不能静默返回首次回执"


def test_complete_and_entrust_join(ledger):
    ledger.reserve("gl-1", "buy", BUY_PARAMS)
    ledger.complete("gl-1", contract.ok({"entrust_no": "777"}), "777")
    assert ledger.get("gl-1")["state"] == "done"
    assert ledger.coid_by_entrust() == {"777": "gl-1"}


def test_survives_reopen(tmp_path):
    """落盘：受控端重启后幂等仍然成立（否则重发=重复下单）。"""
    path = tmp_path / "orders.db"
    OrderLedger(path).reserve("gl-1", "buy", BUY_PARAMS)
    verdict, _ = OrderLedger(path).reserve("gl-1", "buy", BUY_PARAMS)
    assert verdict == "duplicate"


def test_corrupt_ledger_raises_not_silently_degrades(tmp_path):
    bad = tmp_path / "orders.db"
    bad.write_bytes(b"this is not a sqlite file, not even close" * 10)
    with pytest.raises(LedgerUnavailable):
        OrderLedger(bad).reserve("gl-1", "buy", BUY_PARAMS)


# --- dispatcher 闸门 ---------------------------------------------------------

class OrderBackend:
    def __init__(self, ledger, result=None):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.degraded = False
        self.ledger = ledger
        self.submits = 0
        self.client_order_ids: list[str] = []
        self._result = result or contract.ok({"entrust_no": "777"})

    async def buy(self, stock_no, amount, price, client_order_id):
        self.submits += 1
        self.client_order_ids.append(client_order_id)
        return self._result

    async def orders_active(self):
        return contract.ok([])

    async def orders_filled(self):
        return contract.ok([])


def _buy(backend, coid=None, amount=100, rpc_id="x"):
    params = {"stock_no": "600000", "amount": amount, "price": 8.1}
    if coid is not None:
        params["client_order_id"] = coid
    frame = {"type": "call", "id": "x", "method": "buy",
             "params": params}
    frame["id"] = rpc_id
    return asyncio.run(dispatcher.handle_call(frame, backend))


def test_resend_same_coid_never_submits_twice(ledger):
    backend = OrderBackend(ledger)
    first = _buy(backend, "gl-1")
    second = _buy(backend, "gl-1")

    assert backend.submits == 1, "同 coid 重发绝不能产生第二次提交"
    assert first["result"]["data"]["entrust_no"] == "777"
    assert second["result"]["data"]["entrust_no"] == "777"      # 返回首次回执
    assert second["result"]["data"]["idempotent_replay"] is True


def test_resend_after_unknown_outcome_returns_unknown_not_new_order(ledger):
    """首次结果不可知时，重发拿到的仍是「不可知」——契约不撒谎，但也绝不重下。"""
    backend = OrderBackend(ledger, contract.submitted_unconfirmed(
        "已提交但未能确认", data={"submitted": True}))
    _buy(backend, "gl-2")
    second = _buy(backend, "gl-2")
    assert backend.submits == 1
    assert second["result"]["code"] == "submitted_unconfirmed"
    assert second["result"]["error"]["class"] == "unknown_outcome"


def test_same_coid_different_params_rejected(ledger):
    backend = OrderBackend(ledger)
    _buy(backend, "gl-3", amount=100)
    other = _buy(backend, "gl-3", amount=200)
    assert backend.submits == 1
    assert other["result"]["code"] == "invalid_params"
    assert other["result"]["data"]["submitted"] is False


def test_ledger_unavailable_rejects_order(tmp_path):
    """台账不可用一律拒单，禁静默降级为无幂等下单。"""
    class NoLedgerBackend(OrderBackend):
        ledger = None

    backend = NoLedgerBackend.__new__(NoLedgerBackend)
    OrderBackend.__init__(backend, None)
    reply = _buy(backend, "gl-4")
    assert backend.submits == 0
    assert reply["result"]["code"] == "ledger_unavailable"
    assert reply["result"]["error"]["class"] == "ledger_unavailable"


def test_order_without_coid_uses_rpc_id_and_is_idempotent(ledger):
    """调用方无需填写 coid；同 RPC id 重发仍只能提交一次。"""
    backend = OrderBackend(ledger)
    first = _buy(backend, rpc_id="auto-42")
    second = _buy(backend, rpc_id="auto-42")

    assert first["ok"] is True
    assert backend.submits == 1
    assert backend.client_order_ids == ["rpc:buy:auto-42"]
    assert first["result"]["data"]["client_order_id"] == "rpc:buy:auto-42"
    assert second["result"]["data"]["idempotent_replay"] is True


def test_order_without_rpc_id_is_rejected_before_submit(ledger):
    backend = OrderBackend(ledger)
    reply = _buy(backend, rpc_id=None)

    assert reply["ok"] is False
    assert reply["result"]["code"] == "invalid_params"
    assert reply["result"]["data"]["submitted"] is False
    assert backend.submits == 0


# --- C5b query_order ---------------------------------------------------------

def test_query_order_resolves_by_entrust_no(ledger):
    ledger.reserve("gl-5", "buy", BUY_PARAMS)
    ledger.complete("gl-5", contract.ok({"entrust_no": "777"}), "777")

    class B(OrderBackend):
        async def orders_active(self):
            return contract.ok([{"entrust_no": "777", "证券代码": "600000",
                                 "委托数量": 100, "状态": "已报"}])

    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": "gl-5"}}, B(ledger)))
    data = reply["result"]["data"]
    assert data["state"] == "已报"
    assert data["resolution"] == "by_entrust_no"


def test_query_order_unresolved_when_ambiguous(ledger):
    """entrust_no 未知 + 实表有两笔同参单 → 不猜，报未知（需人工）。"""
    ledger.reserve("gl-6", "buy", BUY_PARAMS)

    class B(OrderBackend):
        async def orders_active(self):
            return contract.ok([
                {"entrust_no": "1", "证券代码": "600000", "委托数量": 100, "状态": "已报"},
                {"entrust_no": "2", "证券代码": "600000", "委托数量": 100, "状态": "已报"},
            ])

    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": "gl-6"}}, B(ledger)))
    data = reply["result"]["data"]
    assert data["state"] == "未知"
    assert data["resolution"] == "unresolved"


def test_query_order_unknown_coid_is_not_found(ledger):
    reply = asyncio.run(dispatcher.handle_call(
        {"type": "call", "id": "q", "method": "query_order",
         "params": {"client_order_id": "never-seen"}}, OrderBackend(ledger)))
    assert reply["result"]["code"] == "not_found"
