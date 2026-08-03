"""调用代次：超时后脱缰的工作线程必须在下一个检查点停手。

2026-08-03 串线事故的第二条根因：dispatcher 的 25s 总超时用 asyncio.wait_for 包
asyncio.to_thread——**线程取消不掉**，它还在发全局按键，而 finally 已放 win_lock
让下一笔进场，两个线程同击一个 xiadan 窗口（页面被切走=抓错表，弹窗被抢=错点）。
检查点覆盖翻页/抓表/弹窗/提交四类动作。
"""
import asyncio

import pytest

from trader import dispatcher
from trader.ths import win as w
from trader.ths.win import StaleCallAborted, WinThsBackend


def _backend(monkeypatch):
    b = WinThsBackend()
    b.hwnd_main = 1
    monkeypatch.setattr(w, "hot_key", lambda keys: None)
    monkeypatch.setattr(w, "_activate_window", lambda hwnd: None)
    monkeypatch.setattr(w, "sleep_time", 0)
    return b


def test_guarded_call_aborts_after_invalidation(monkeypatch):
    b = _backend(monkeypatch)

    def work():
        b._abort_if_stale("before")        # 本笔仍有效 → 放行
        b.invalidate_inflight("模拟 dispatcher 超时")
        b._abort_if_stale("after")         # 已被作废 → 必须抛
        return {"code": 0, "status": "succeed"}

    result = b._run_guarded(work)
    assert result["status"] == "failed"
    assert result["code"] == "aborted"
    assert "作废" in result["error"]["message"]


@pytest.mark.parametrize("action", [
    lambda b: b.switch_to_normal(),   # 翻页链路第一个动作（发全局按键之前）
    lambda b: b.refresh(),            # F5
    lambda b: b.read_table_text(1),   # Ctrl+C 抓表
    lambda b: b.dialog_cleanup(),     # degraded 自愈：与下一笔抢同一个弹窗
    lambda b: b._pump_dialogs(),      # 提交后的弹窗处置
])
def test_every_checkpoint_stops_a_stale_thread(monkeypatch, action):
    b = _backend(monkeypatch)
    raised = {}

    def work():
        b.invalidate_inflight("模拟超时")
        try:
            action(b)
        except StaleCallAborted as e:
            raised["where"] = e.where
            raise
        return {"code": 0}

    b._run_guarded(work)
    assert raised, "检查点没拦住脱缰线程"


def test_stale_query_stops_before_regrabbing(monkeypatch):
    """抓表期间被判超时 → 不再重抓第二轮，也不再敲一轮翻页键给别人的页面。

    （switch_to_normal 依赖真 Win32，这里打桩；真正拦住第二轮的是 refresh 里的
    检查点——每个检查点自身的拦截能力由上面的参数化用例逐个覆盖。）
    """
    b = _backend(monkeypatch)
    reads = []
    monkeypatch.setattr(b, "switch_to_normal", lambda: None)
    monkeypatch.setattr(b, "get_right_hwnd", lambda: 999)
    monkeypatch.setattr(b, "_find_grid", lambda hwnd: 888)

    def read(ctrl):
        reads.append(ctrl)
        b.invalidate_inflight("抓表期间超时")     # 模拟 dispatcher 此刻判超时放锁
        return "成交时间\t证券代码\t成交金额\t\r\n"  # 且抓到的还是别人的表 → 本会触发重抓

    monkeypatch.setattr(b, "read_table_text", read)

    r = b.get_active_orders()
    assert r["status"] == "failed"
    assert len(reads) == 1, "本笔已作废，绝不能再抓第二轮"


def test_unmanaged_thread_is_not_blocked(monkeypatch):
    """UI 直调 / 单测直调不带代次，作废动作不能把它们一起打死。"""
    b = _backend(monkeypatch)
    b.invalidate_inflight("与本线程无关")
    b._abort_if_stale("ui")  # 不抛即通过


class RecordingBackend:
    """dispatcher 侧替身：只关心超时时有没有作废在飞线程。"""

    def __init__(self):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.degraded = False
        self.invalidated: list[str] = []

    async def orders_active(self):
        await asyncio.sleep(3600)

    def dialog_cleanup(self):
        pass

    def invalidate_inflight(self, reason=""):
        self.invalidated.append(reason)
        return len(self.invalidated)


def test_dispatcher_invalidates_inflight_on_timeout(monkeypatch):
    monkeypatch.setattr(dispatcher, "CALL_TIMEOUT_SECS", 0.05)
    backend = RecordingBackend()
    frame = {"type": "call", "id": "t1", "method": "orders_active", "params": {}}
    reply = asyncio.run(dispatcher.handle_call(frame, backend))
    assert reply["ok"] is False
    assert backend.invalidated, "超时后必须作废在飞线程，否则它继续操作窗口"
    assert "orders_active" in backend.invalidated[0]
    assert backend.degraded is True
