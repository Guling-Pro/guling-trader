"""switch_account 的 slot 参数闸：非法值必须在碰任何 Win32 之前被拒绝。

多账户盲切是真钱路径的入口——slot 打错（0、负数、字符串、None）绝不能
落到 hot_key 发键，必须在 async 包装层就地拦下并给出明确文案。
绑定/发键层用打桩隔离，全部用例可在非 Windows 平台运行。
"""
import asyncio
from types import SimpleNamespace

from trader import contract
from trader.ths import win as w

from trader.ths.win import WinThsBackend, _account_candidates_from_ocr


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
                        iter(["中信证券 王*洲", "银河证券 周*英"]).__next__)
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
    assert result["data"]["account_text"] == "银河证券 周*英"
    assert result["data"]["balance"]["可用金额"] == 20000.78
    assert result["data"]["msg"] == "已切换到：银河证券 周*英"
    assert sent == [(["alt", "2"], "switch_account")]
    assert backend.account_trading_blocked is False


def test_switch_text_unchanged_blocks_trading(monkeypatch):
    """账户文本没有变化时不能伪报切换成功，且保留买卖闸门。"""
    backend = WinThsBackend()
    monkeypatch.setattr(backend, "_read_account_selector_text", lambda: "中信证券 王*洲")
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


def test_account_ocr_only_accepts_rows_with_shortcut():
    """截图中的账户行带 Alt+N；编辑账户和资金数字不能进入列表。"""
    lines = [
        {"text": "中信证券-王*洲 Alt+1", "left": 340, "top": 130,
         "right": 730, "bottom": 160, "conf": 95.0},
        {"text": "银河证券-周*英 Alt+2", "left": 340, "top": 170,
         "right": 730, "bottom": 200, "conf": 91.0},
        {"text": "编辑账户", "left": 340, "top": 210,
         "right": 450, "bottom": 240, "conf": 99.0},
        {"text": "可用 20000.78", "left": 400, "top": 260,
         "right": 600, "bottom": 290, "conf": 90.0},
    ]
    accounts = _account_candidates_from_ocr(lines)

    assert [(x["slot"], x["text"]) for x in accounts] == [
        (1, "中信证券-王*洲"), (2, "银河证券-周*英"),
    ]


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
