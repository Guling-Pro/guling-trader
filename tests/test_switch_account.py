"""switch_account 的 slot 参数闸：非法值必须在碰任何 Win32 之前被拒绝。

多账户盲切是真钱路径的入口——slot 打错（0、负数、字符串、None）绝不能
落到 hot_key 发键，必须在 async 包装层就地拦下并给出明确文案。
绑定/发键层用打桩隔离，全部用例可在非 Windows 平台运行。
"""
import asyncio
from types import SimpleNamespace

import pytest

from trader import contract
from trader.ths import win as w

from trader.ths.win import WinThsBackend, _account_candidates_from_listbox


def _switch(slot):
    return asyncio.run(WinThsBackend().switch_account(slot))


def test_rejects_non_integer_slot():
    for bad in (None, "abc", [1], {}):
        result = _switch(bad)
        assert result["code"] == "invalid_params"
        assert "slot 参数无效" in result["error"]["message"]


def test_rejects_out_of_range_slot():
    for bad in (0, -1, 10, 99):
        result = _switch(bad)
        assert result["code"] == "invalid_params"
        assert "slot 超出范围" in result["error"]["message"]


def test_valid_slot_passes_gate_and_reaches_bind(monkeypatch):
    """合法 slot（含 '2' 这类可转数字串）通过参数闸、走到绑定阶段。"""
    backend = WinThsBackend()
    bind_err = {"code": 1, "error": "未绑定（桩）"}
    monkeypatch.setattr(backend, "_ensure_bound", lambda: bind_err)
    for ok in (1, 2, 9, "2"):
        result = asyncio.run(backend.switch_account(ok))
        assert result is bind_err


def test_coerced_int_slot_forwarded_to_do_switch(monkeypatch):
    """slot 以 int 形态透传给 do_switch_account（'2' → 2）。"""
    backend = WinThsBackend()
    monkeypatch.setattr(backend, "_ensure_bound", lambda: None)
    seen = []
    monkeypatch.setattr(
        backend, "do_switch_account",
        lambda slot: (seen.append(slot) or contract.ok({"slot": slot})),
    )
    result = asyncio.run(backend.switch_account("2"))
    assert result["status"] == "succeed"
    assert seen == [2]


def test_switch_reads_account_text_and_balance(monkeypatch):
    """切换后按 0x094C 文本确认，并在同一回执中返回资金信息。"""
    backend = WinThsBackend()
    monkeypatch.setattr(backend, "_read_account_selector_text",
                        iter(["示例券商 甲*乙", "示例券商 丙*丁"]).__next__)
    sent = []
    monkeypatch.setattr(backend, "_send_hotkey",
                        lambda keys, where: sent.append((keys, where)))
    monkeypatch.setattr(
        backend, "get_balance",
        lambda: contract.ok({"可用金额": 20000.78, "总资产": 20000.78}),
    )
    monkeypatch.setattr(w, "sleep_time", 0)

    result = backend.do_switch_account(2)

    assert result["status"] == "succeed"
    assert result["data"]["account_verified"] is True
    assert result["data"]["account_text"] == "示例券商 丙*丁"
    assert result["data"]["balance"]["可用金额"] == 20000.78
    assert result["data"]["msg"] == "已切换到：示例券商 丙*丁"
    assert sent == [(["alt", "2"], "switch_account")]
    assert backend.account_trading_blocked is False


def test_switch_text_unchanged_blocks_trading(monkeypatch):
    """账户文本没有变化时不能伪报切换成功，且保留买卖闸门。"""
    backend = WinThsBackend()
    monkeypatch.setattr(backend, "_read_account_selector_text", lambda: "示例券商 甲*乙")
    monkeypatch.setattr(backend, "_send_hotkey", lambda keys, where: None)
    monkeypatch.setattr(w, "ACCOUNT_VERIFY_TIMEOUT_SECS", 0)
    balance_called = []
    monkeypatch.setattr(backend, "get_balance", lambda: balance_called.append(True))

    result = backend.do_switch_account(2)

    assert result["status"] == "failed"
    assert result["code"] == "read_failed"
    assert result["data"]["account_verified"] is False
    assert backend.account_trading_blocked is True
    assert balance_called == []


def test_trade_account_preflight_establishes_then_checks_baseline(monkeypatch):
    """启动后先读 0x094C 建基线，后续每笔交易都必须与其一致。"""
    backend = WinThsBackend()
    assert backend.account_trading_blocked is True

    monkeypatch.setattr(backend, "_read_account_selector_text",
                        lambda: "示例券商 甲*乙")
    first = backend._verify_account_for_trade()
    second = backend._verify_account_for_trade()

    assert first["status"] == "succeed"
    assert first["data"]["account_baseline_established"] is True
    assert second["status"] == "succeed"
    assert second["data"]["account_baseline_established"] is False
    assert backend.account_trading_blocked is False


def test_trade_account_preflight_blocks_changed_or_unreadable_text(monkeypatch):
    """手动切户或控件不可读时，后续买卖和撤单都必须保持关闭。"""
    backend = WinThsBackend()
    monkeypatch.setattr(backend, "_read_account_selector_text",
                        lambda: "示例券商 甲*乙")
    assert backend._verify_account_for_trade()["status"] == "succeed"

    monkeypatch.setattr(backend, "_read_account_selector_text",
                        lambda: "示例券商 丙*丁")
    changed = backend._verify_account_for_trade()
    assert changed["code"] == "read_failed"
    assert changed["data"]["expected_account_text"] == "示例券商 甲*乙"
    assert changed["data"]["account_text"] == "示例券商 丙*丁"
    assert backend.account_trading_blocked is True

    monkeypatch.setattr(backend, "_read_account_selector_text", lambda: None)
    unreadable = backend._verify_account_for_trade()
    assert unreadable["code"] == "read_failed"
    assert unreadable["data"]["account_text"] is None
    assert backend.account_trading_blocked is True


def test_account_listbox_filters_edit_item_and_assigns_slots_by_row_order():
    """真机 ListBox 最后一行“编辑账户”不是账户，账户行从上至下对应 Alt+1..9。"""
    accounts = _account_candidates_from_listbox([
        "示例券商-甲*乙", "示例券商-丙*丁", "编辑账户",
    ])

    assert accounts == [
        {"slot": 1, "shortcut": "Alt+1", "text": "示例券商-甲*乙"},
        {"slot": 2, "shortcut": "Alt+2", "text": "示例券商-丙*丁"},
    ]


def test_account_listbox_rejects_more_than_nine_accounts():
    with pytest.raises(ValueError, match="超过 Alt\\+1..Alt\\+9 范围"):
        _account_candidates_from_listbox([f"账户{i}" for i in range(10)])


def test_account_listbox_uses_a_private_four_argument_send_message(monkeypatch):
    """ListBox 的四参数绑定不能污染后续普通控件的两参数 SendMessageW 调用。"""
    calls = []

    class SendMessage:
        def __call__(self, hwnd, message, wparam, lparam):
            calls.append((hwnd, message, wparam, lparam))
            if message == w.LB_GETCOUNT:
                return 2
            return 0

    sender = SendMessage()
    monkeypatch.setattr(
        w.ctypes, "WinDLL", lambda *_args, **_kwargs: SimpleNamespace(SendMessageW=sender),
        raising=False,
    )

    assert WinThsBackend()._read_account_listbox_items(123) == ["", ""]
    assert calls == [
        (123, w.LB_GETCOUNT, 0, 0),
        (123, w.LB_GETTEXTLEN, 0, 0),
        (123, w.LB_GETTEXT, 0, pytest.approx(calls[2][3])),
        (123, w.LB_GETTEXTLEN, 1, 0),
        (123, w.LB_GETTEXT, 1, pytest.approx(calls[4][3])),
    ]


def test_get_account_list_reads_listbox_text_without_ocr(monkeypatch):
    backend = WinThsBackend()
    monkeypatch.setattr(backend, "_switch_to_normal_safely", lambda: None)
    monkeypatch.setattr(backend, "_read_account_selector_text", lambda: "示例券商-甲*乙")
    monkeypatch.setattr(backend, "_open_account_dropdown", lambda: True)
    monkeypatch.setattr(backend, "_find_open_account_listbox", lambda: 123)
    monkeypatch.setattr(
        backend, "_read_account_listbox_items",
        lambda hwnd: ["示例券商-甲*乙", "编辑账户"],
    )
    closed = []
    monkeypatch.setattr(backend, "_send_hotkey", lambda keys, where: closed.append((keys, where)))
    monkeypatch.setattr(w, "ACCOUNT_DROPDOWN_SETTLE_SECS", 0)

    result = backend.get_account_list()

    assert result["status"] == "succeed"
    assert result["data"] == {
        "accounts": [{"slot": 1, "shortcut": "Alt+1", "text": "示例券商-甲*乙"}],
        "current_account_text": "示例券商-甲*乙",
        "partial": False,
        "source": "listbox_text",
        "msg": "已读取账户下拉列表原始文本，未选择或切换任何账户",
    }
    assert closed == [(["esc"], "close_account_dropdown")]


def test_account_dropdown_click_uses_visible_verified_combobox(monkeypatch):
    """账户列表入口只点击可见、启用且归属于当前进程的 0x0912 ComboBox。"""
    backend = WinThsBackend()
    backend.hwnd_main = 100
    calls = []
    monkeypatch.setattr(
        backend, "_find_ctrl_by_id",
        lambda root, cid, cls=None, visible=False: (
            calls.append((root, cid, cls, visible)) or 200
        ),
    )
    monkeypatch.setattr(backend, "_window_is_owned_by_bound_process", lambda hwnd: True)
    monkeypatch.setattr(w, "win32gui", SimpleNamespace(
        GetWindowRect=lambda hwnd: (10, 20, 110, 60),
        IsWindowEnabled=lambda hwnd: True,
    ), raising=False)
    monkeypatch.setattr(
        backend, "_click_screen",
        lambda x, y, where: calls.append(((x, y), where)),
    )

    assert backend._open_account_dropdown() is True
    assert calls == [
        (100, 0x0912, "ComboBox", True),
        ((60, 40), "account_dropdown"),
    ]
