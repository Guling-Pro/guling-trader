"""ws_client：call 帧在后台 task 执行，消息循环不被单笔 RPC 阻塞。

2026-07-13 事故的放大器之一：_handle_frame 内联 await 执行 RPC，一笔卡死
让受控端不再处理任何后续帧（连核单查询都进不来）。
"""
import asyncio
import json

from trader import ws_client


class FakeWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class SlowBackend:
    def __init__(self):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.degraded = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def orders_active(self):
        self.started.set()
        await self.release.wait()  # 卡住，直到测试放行
        return {"code": 0, "status": "succeed", "data": []}


def test_call_frame_does_not_block_handle_frame():
    async def drive():
        backend = SlowBackend()
        client = ws_client.WsClient(backend=backend)
        client.ws = FakeWs()

        frame = {"type": "call", "id": "slow", "method": "orders_active", "params": {}}
        # _handle_frame 必须立刻返回（RPC 在后台 task 里执行）
        await asyncio.wait_for(client._handle_frame(frame), timeout=1.0)
        await asyncio.wait_for(backend.started.wait(), timeout=1.0)
        assert client.ws.sent == []  # RPC 还卡着，说明 _handle_frame 没有等它

        backend.release.set()  # 放行 → reply 应该被补发
        for _ in range(100):
            if client.ws.sent:
                break
            await asyncio.sleep(0.01)
        assert client.ws.sent and client.ws.sent[0]["id"] == "slow"
        assert client.ws.sent[0]["ok"] is True

    asyncio.run(drive())
