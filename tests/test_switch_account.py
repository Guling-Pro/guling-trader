"""switch_account 的 slot 参数闸：非法值必须在碰任何 Win32 之前被拒绝。

多账户盲切是真钱路径的入口——slot 打错（0、负数、字符串、None）绝不能
落到 hot_key 发键，必须在 async 包装层就地拦下并给出明确文案。
绑定/发键层用打桩隔离，全部用例可在非 Windows 平台运行。
"""
import asyncio

from trader.ths.win import WinThsBackend


def _switch(slot):
    return asyncio.run(WinThsBackend().switch_account(slot))


def test_rejects_non_integer_slot():
    for bad in (None, "abc", [1], {}):
        result = _switch(bad)
        assert result["code"] == 1
        assert "slot 参数无效" in result["msg"]


def test_rejects_out_of_range_slot():
    for bad in (0, -1, 10, 99):
        result = _switch(bad)
        assert result["code"] == 1
        assert "slot 超出范围" in result["msg"]


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
        lambda slot: (seen.append(slot) or {"code": 0, "data": {"slot": slot}}),
    )
    result = asyncio.run(backend.switch_account("2"))
    assert result["code"] == 0
    assert seen == [2]
