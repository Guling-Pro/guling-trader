"""dispatcher 超时/busy/degraded 语义回归（2026-07-13 弹窗卡死事故）。

三条铁律：
1. 下单类调用无论卡在哪，超时后必回 code=2 status=unknown +「先核单勿补单」，
   绝不表现为裸报错或永不回复；
2. 拿不到 win_lock 回 busy，不无限饿死；
3. 超时后置 degraded，下一次调用先跑 dialog_cleanup 自愈。
"""
import asyncio

from trader import dispatcher


COID = "gl-0198f6a1-0003-7000-8000-000000000003"


class FakeLedger:
    def reserve(self, client_order_id, method, params):
        return "new", None

    def complete(self, client_order_id, receipt, entrust_no=None):
        pass

    def release(self, client_order_id):
        pass


class HangingBackend:
    """sell 永远不返回（模拟弹窗卡死）；查询正常。"""

    def __init__(self):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.degraded = False
        self.cleanup_calls = 0
        self.ledger = FakeLedger()

    async def sell(self, *a, **k):
        await asyncio.sleep(3600)

    async def orders_active(self):
        from trader import contract
        return contract.ok([])

    def dialog_cleanup(self):  # dispatcher 经 asyncio.to_thread 调用（同步）
        self.cleanup_calls += 1


def _call(backend, method, params=None, **frame_extra):
    frame = {"type": "call", "id": "t1", "method": method, "params": params or {}}
    frame.update(frame_extra)
    return asyncio.run(dispatcher.handle_call(frame, backend))


def test_order_timeout_returns_unknown_not_bare_error(monkeypatch):
    monkeypatch.setattr(dispatcher, "CALL_TIMEOUT_SECS", 0.05)
    backend = HangingBackend()
    reply = _call(backend, "sell", {"stock_no": "300458", "amount": 500,
                                      "order_type": "FIVE_LEVEL_IOC",
                                      "client_order_id": COID})
    assert reply["ok"] is False
    assert reply["result"]["code"] == "submitted_unconfirmed"
    assert reply["result"]["error"]["class"] == "unknown_outcome"
    # 核单指引必须在错误文本里，防调用方凭报错补单
    assert "query_order" in reply["error"] or "orders_active" in reply["error"]
    assert "勿改单重下" in reply["error"]
    assert backend.degraded is True


def test_query_timeout_is_failed_not_unknown(monkeypatch):
    monkeypatch.setattr(dispatcher, "CALL_TIMEOUT_SECS", 0.05)

    class SlowQueryBackend(HangingBackend):
        async def orders_active(self):
            await asyncio.sleep(3600)

    reply = _call(SlowQueryBackend(), "orders_active")
    assert reply["ok"] is False
    assert reply["result"]["code"] == "call_timeout"  # 查询超时是普通失败，不是「可能已提交」


def test_lock_busy_instead_of_starvation(monkeypatch):
    monkeypatch.setattr(dispatcher, "LOCK_TIMEOUT_SECS", 0.05)
    backend = HangingBackend()

    async def drive():
        await backend.win_lock.acquire()  # 模拟持锁方被拖住
        frame = {"type": "call", "id": "t2", "method": "buy",
                 "params": {"stock_no": "600000", "amount": 100,
                            "order_type": "FIVE_LEVEL_IOC",
                            "client_order_id": COID}}
        return await dispatcher.handle_call(frame, backend)

    reply = asyncio.run(drive())
    assert reply["ok"] is False
    assert reply["result"]["status"] == "busy"
    assert reply["result"]["code"] == "busy"
    assert "orders_active" in reply["error"]  # 下单类 busy 也要带核单提醒


def test_degraded_triggers_cleanup_on_next_call():
    backend = HangingBackend()
    backend.degraded = True
    reply = _call(backend, "orders_active")
    assert reply["ok"] is True
    assert backend.cleanup_calls == 1
    assert backend.degraded is False  # 自愈后复位


def test_normal_call_unaffected():
    backend = HangingBackend()
    reply = _call(backend, "orders_active")
    assert reply["ok"] is True
    assert backend.cleanup_calls == 0
