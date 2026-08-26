"""握手日志不能泄露一次性配对码或长期凭证。"""
from trader.handshake import _redact_response_for_log


def test_pair_pending_log_redacts_pairing_code():
    result = _redact_response_for_log({
        "type": "pair_pending", "code": "123456", "expires_at": 123,
    })

    assert result == {
        "type": "pair_pending", "code": "<redacted>", "expires_at": 123,
    }


def test_handshake_log_redacts_token_fields():
    result = _redact_response_for_log({
        "type": "welcome", "agent_token": "secret", "session_id": "session-1",
    })

    assert result["agent_token"] == "<redacted>"
    assert result["session_id"] == "session-1"
