"""dispatcher 锁 + agent 下单登记回归。沿用 asyncio.run 同步驱动约定。"""
import asyncio

from trader import contract, dispatcher


COID = "gl-0198f6a1-0002-7000-8000-000000000002"


class FakeLedger:
    def reserve(self, client_order_id, method, params):
        return "new", None

    def complete(self, client_order_id, receipt, entrust_no=None):
        pass

    def release(self, client_order_id):
        pass


class LockFakeBackend:
    def __init__(self):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.concurrent = 0
        self.max_concurrent = 0
        self.ledger = FakeLedger()

    async def _hold(self, result):
        # 记录临界区并发度，验证锁真的串行化。
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(0.02)
        self.concurrent -= 1
        return result

    async def orders_active(self):
        return await self._hold(contract.ok([]))

    async def verify_account_for_trade(self):
        return contract.ok({"account_verified": True, "account_text": "测试账户"})

    async def buy(self, stock_no, amount, price, client_order_id):
        return await self._hold(contract.ok({"entrust_no": "777"}))

    async def sell(self, stock_no, amount, price, client_order_id):
        return await self._hold(contract.ok({"entrust_no": "888"}))


def test_window_methods_serialized_by_lock():
    backend = LockFakeBackend()

    async def drive():
        frame = {"type": "call", "id": "x", "method": "orders_active", "params": {}}
        await asyncio.gather(*[dispatcher.handle_call(dict(frame), backend) for _ in range(5)])

    asyncio.run(drive())
    assert backend.max_concurrent == 1  # 串行化：临界区任意时刻至多 1 个


def test_buy_registers_entrust_no():
    backend = LockFakeBackend()
    frame = {"type": "call", "id": "b", "method": "buy",
             "params": {"stock_no": "600519", "amount": 100, "price": 1700.0,
                        "order_type": "LIMIT",
                        "client_order_id": COID}}
    reply = asyncio.run(dispatcher.handle_call(frame, backend))
    assert reply["ok"] is True
    assert "777" in backend.agent_entrust_nos
