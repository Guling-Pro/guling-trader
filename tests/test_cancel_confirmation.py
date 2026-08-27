"""撤单点击后的 F3 回执判定，不触碰 Win32。"""

import pytest

from trader import contract
from trader.ths import win as w
from trader.ths.win import WinThsBackend, _VERIFIED_EMPTY_GRID, _cancel_f3_outcome


HEADER = "委托编号\t证券代码\t委托状态\t委托数量\t\r\n"


def _table(*rows):
    return HEADER + "".join("\t".join(row) + "\t\r\n" for row in rows)


@pytest.mark.parametrize("status", ["已撤", "部撤", "撤单成功"])
def test_f3_explicit_cancel_is_confirmed(status):
    assert _cancel_f3_outcome(_table(("A-1", "600000", status, "100")), "A-1") == (
        "canceled", "已撤"
    )


@pytest.mark.parametrize("status,expected", [
    ("已报", "已报"), ("部成", "部成"), ("已成", "已成"),
    ("废单", "废单"), ("柜台处理中", "未知"), ("撤单中", "未知"),
])
def test_f3_non_cancel_state_is_never_confirmed(status, expected):
    assert _cancel_f3_outcome(_table(("A-1", "600000", status, "100")), "A-1") == (
        "pending", expected
    )


def test_f3_missing_target_is_unresolved_not_cancelled():
    assert _cancel_f3_outcome(_table(("B-2", "600000", "已报", "100")), "A-1") == (
        "unresolved", None
    )


def test_f3_empty_or_verified_empty_grid_is_unresolved_not_cancelled():
    empty = HEADER + "\t\t\t\t\r\n"
    assert _cancel_f3_outcome(empty, "A-1") == ("unresolved", None)
    assert _cancel_f3_outcome(_VERIFIED_EMPTY_GRID, "A-1") == ("unresolved", None)


@pytest.mark.parametrize("data", [
    None,
    "证券代码\t委托状态\t\r\n600000\t已撤\t\r\n",
    "委托编号\t证券代码\t\r\nA-1\t600000\t\r\n",
])
def test_f3_unreadable_or_wrong_table_is_not_cancelled(data):
    assert _cancel_f3_outcome(data, "A-1") == ("unreadable", None)


def test_f3_accepts_contract_number_and_remark_aliases():
    data = "合同编号\t证券代码\t备注\t\r\nA-1\t600000\t已撤\t\r\n"
    assert _cancel_f3_outcome(data, "A-1") == ("canceled", "已撤")


def test_f3_matches_either_id_alias_when_both_columns_exist():
    data = "委托编号\t合同编号\t委托状态\t\r\nE-1\tC-1\t已撤\t\r\n"
    assert _cancel_f3_outcome(data, "C-1") == ("canceled", "已撤")


def test_post_submit_f3_confirmation_returns_success_without_second_cancel(monkeypatch):
    backend = WinThsBackend()
    calls = []
    monkeypatch.setattr(backend, "refresh", lambda **kwargs: calls.append("refresh"))
    monkeypatch.setattr(backend, "get_right_hwnd", lambda: 10)
    monkeypatch.setattr(backend, "_find_grid", lambda hwnd: 20)
    monkeypatch.setattr(
        backend, "read_table_text", lambda ctrl: _table(("A-1", "600000", "已撤", "100")),
    )

    result = backend._verify_cancel_after_submit("A-1")

    assert contract.is_succeed(result)
    assert result["data"] == {
        "entrust_no": "A-1",
        "submitted": True,
        "cancel_state": "已撤",
        "cancel_verified": True,
    }
    assert calls == ["refresh"]


def test_post_submit_missing_target_returns_unconfirmed(monkeypatch):
    backend = WinThsBackend()
    monkeypatch.setattr(backend, "refresh", lambda **kwargs: None)
    monkeypatch.setattr(backend, "get_right_hwnd", lambda: 10)
    monkeypatch.setattr(backend, "_find_grid", lambda hwnd: 20)
    monkeypatch.setattr(backend, "read_table_text", lambda ctrl: HEADER)
    monkeypatch.setattr(w, "CANCEL_VERIFY_TIMEOUT_SECS", 0.001)
    monkeypatch.setattr(w.time, "sleep", lambda _: None)

    result = backend._verify_cancel_after_submit("A-1")

    assert result["code"] == "submitted_unconfirmed"
    assert result["data"]["submitted"] is True
    assert result["data"]["cancel_verified"] is False
    assert result["data"]["f3_verification"] == "unresolved"
