"""回执归属：执行期间断线重连的话，旧回执必须丢弃而不是发到新连接上。

一笔 RPC 最长跑 25s，其间完全可能重连。旧回执的 id 属于旧会话，发到新连接上
归属无从保证（能否被网关配到别的请求头上取决于对端 id 策略，不能靠对端兜底）。
"""
import asyncio
import json

from trader import ws_client


class FakeWs:
    def __init__(self, name):
        self.name = name
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class Backend:
    def __init__(self):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.degraded = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def orders_active(self):
        self.started.set()
        await self.release.wait()
        return {"code": 0, "status": "succeed", "data": []}


async def _drive(reconnect: bool):
    backend = Backend()
    client = ws_client.WsClient(backend=backend)
    old = FakeWs("old")
    client.ws = old

    frame = {"type": "call", "id": "rpc-1", "method": "orders_active", "params": {}}
    await client._handle_frame(frame, old)
    await asyncio.wait_for(backend.started.wait(), timeout=1.0)

    new = FakeWs("new")
    if reconnect:
        client.ws = new       # 执行期间断线重连
    backend.release.set()
    for _ in range(100):      # 等后台 task 收尾
        if old.sent or new.sent:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    return old, new


def test_reply_dropped_when_connection_changed():
    old, new = asyncio.run(_drive(reconnect=True))
    assert new.sent == [], "旧会话的回执绝不能发到新连接上"
    assert old.sent == []


def test_reply_sent_normally_on_same_connection():
    old, new = asyncio.run(_drive(reconnect=False))
    assert len(old.sent) == 1
    assert old.sent[0]["id"] == "rpc-1"
    assert old.sent[0]["ok"] is True
