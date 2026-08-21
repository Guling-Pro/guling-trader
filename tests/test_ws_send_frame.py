"""WsClient.send_frame 发送结果的回归测试。"""
import asyncio

from websockets.protocol import State

from trader import ws_client


class ClosedWs:
    state = State.CLOSED

    def __init__(self):
        self.send_called = False

    async def send(self, _raw):
        self.send_called = True


class FailingWs:
    async def send(self, _raw):
        raise RuntimeError("simulated write failure")


class SendingWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)


class BlockingWs:
    def __init__(self):
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send(self, _raw):
        self.send_started.set()
        await self.release_send.wait()


def _client():
    return ws_client.WsClient(backend=object())


def test_send_frame_returns_false_when_unconnected_or_closed():
    client = _client()
    assert asyncio.run(client.send_frame({"type": "order_event"})) is False

    closed = ClosedWs()
    client.ws = closed
    assert asyncio.run(client.send_frame({"type": "order_event"})) is False
    assert closed.send_called is False


def test_send_frame_returns_false_when_write_raises():
    client = _client()
    client.ws = FailingWs()

    assert asyncio.run(client.send_frame({"type": "order_event"})) is False


def test_send_frame_returns_true_after_successful_write():
    client = _client()
    ws = SendingWs()
    client.ws = ws

    assert asyncio.run(client.send_frame({"type": "order_event"})) is True
    assert len(ws.sent) == 1


def test_send_frame_returns_false_when_connection_changes_during_write():
    async def drive():
        client = _client()
        old_ws = BlockingWs()
        client.ws = old_ws

        sending = asyncio.create_task(client.send_frame({"type": "order_event"}))
        await old_ws.send_started.wait()

        # run() may have torn down the old socket and established a new one while
        # send_frame was awaiting the old write. The frame must remain retryable.
        client.ws = SendingWs()
        client._connection_generation += 1
        old_ws.release_send.set()

        assert await sending is False
        assert client.ws.sent == []

    asyncio.run(drive())
