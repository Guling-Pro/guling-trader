"""连接后账户列表提示的回归测试。"""
import asyncio
import json

from trader import contract
from trader.ws_client import ConnectionState, WsClient


class _Socket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Backend:
    def __init__(self, result):
        self.win_lock = asyncio.Lock()
        self.result = result
        self.calls = 0

    async def list_accounts(self):
        self.calls += 1
        return self.result


def _connected_client(result):
    backend = _Backend(result)
    client = WsClient(backend=backend)
    client.state = ConnectionState.CONNECTED
    client.ws = _Socket()
    client._connection_generation = 1
    return client, backend


def test_connected_client_pushes_available_accounts_once():
    client, backend = _connected_client(contract.ok({
        "accounts": [{"slot": 1, "shortcut": "Alt+1", "text": "示例券商-甲*乙"}],
        "current_account_text": "示例券商 甲*乙",
        "partial": False,
    }))

    asyncio.run(client._publish_accounts_after_connected())
    asyncio.run(client._publish_accounts_after_connected())

    assert backend.calls == 1
    assert client.ws.sent[0]["type"] == "account_event"
    assert client.ws.sent[0]["event"] == "available"
    assert client.ws.sent[0]["accounts"][0]["slot"] == 1
    assert client.ws.sent[0]["current_account_text"] == "示例券商 甲*乙"


def test_connected_client_reports_unavailable_accounts_without_trading():
    client, backend = _connected_client(contract.fail(
        "read_failed", "read_failed", "账户下拉框不可读",
        data={"accounts": [], "current_account_text": None, "partial": True},
    ))

    asyncio.run(client._publish_accounts_after_connected())

    assert backend.calls == 1
    assert client.ws.sent[0]["event"] == "unavailable"
    assert client.ws.sent[0]["accounts"] == []
    assert client.ws.sent[0]["message"] == "账户下拉框不可读"
