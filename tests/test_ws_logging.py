"""WebSocket 本地审计日志：保留诊断信息，但任何凭证都不能落盘。"""
import asyncio
import logging

from trader import ws_client


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)


def test_audit_json_redacts_secret_values_and_keeps_trading_fields():
    rendered = ws_client._audit_json({
        "type": "call",
        "params": {
            "stock_no": "600000",
            "amount": 100,
            "agent_token": "agent-secret",
            "nested": {"confirmation_token": "confirm-secret"},
        },
    })

    assert '"stock_no": "600000"' in rendered
    assert '"amount": 100' in rendered
    assert "agent-secret" not in rendered
    assert "confirm-secret" not in rendered
    assert rendered.count("<redacted>") == 2


def test_pair_pending_code_is_redacted():
    rendered = ws_client._audit_json({
        "type": "pair_pending", "code": "123456", "expires_at": "soon",
    })

    assert "123456" not in rendered
    assert '"code": "<redacted>"' in rendered


def test_rpc_audit_logs_full_sanitized_request_and_reply(monkeypatch, caplog):
    async def fake_handle_call(frame, _backend):
        return {
            "type": "reply",
            "id": frame["id"],
            "ok": True,
            "result": {
                "status": "succeed",
                "data": {
                    "entrust_no": "E-001",
                    "confirmation_token": "reply-secret",
                },
            },
        }

    monkeypatch.setattr(ws_client.dispatcher, "handle_call", fake_handle_call)
    client = ws_client.WsClient(backend=object())
    client.ws = FakeWs()
    frame = {
        "type": "call",
        "id": "rpc-audit-1",
        "method": "buy",
        "params": {
            "stock_no": "600000",
            "amount": 100,
            "order_type": "LIMIT",
            "price": 10.5,
            "agent_token": "request-secret",
        },
    }

    caplog.set_level(logging.INFO, logger="trader.ws_client")

    async def drive():
        await client._handle_frame(frame)
        await asyncio.gather(*list(client._call_tasks))

    asyncio.run(drive())

    rendered = caplog.text
    assert "[WS<-] received=" in rendered
    assert "[WS->] reply_ready id=rpc-audit-1 method=buy" in rendered
    assert "[WS->] reply_written id=rpc-audit-1 method=buy" in rendered
    assert '"stock_no": "600000"' in rendered
    assert '"entrust_no": "E-001"' in rendered
    assert "request-secret" not in rendered
    assert "reply-secret" not in rendered
