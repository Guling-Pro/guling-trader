"""get_balance 可见性过滤回归：多账户下必须读当前账户的可见控件。

2026-07-14 双账户切换演练事故：xiadan 多账户登录时每个账户各挂一套同 ID
资金控件（0x3F4..），只有当前账户的可见。get_balance 原先不过滤可见性，
Alt+2 已切到账户二后仍返回账户一的全套数字——真钱 sizing 的输入被污染。
这里锁定「可见优先、放宽兜底」的查找顺序。Win32 层全部打桩，可跨平台跑。
"""
from trader.ths import win as w
from trader.ths.win import WinThsBackend

HIDDEN, VISIBLE = 111, 222


def _stubbed_backend(monkeypatch, find_ctrl):
    b = WinThsBackend()
    monkeypatch.setattr(b, "switch_to_normal", lambda: None)
    monkeypatch.setattr(b, "refresh", lambda: None)
    monkeypatch.setattr(b, "get_right_hwnd", lambda: 999)
    monkeypatch.setattr(b, "_find_ctrl_by_id", find_ctrl)
    monkeypatch.setattr(w, "hot_key", lambda keys: None)
    monkeypatch.setattr(
        w, "get_text", lambda h: "1.23" if h == VISIBLE else "34915.47"
    )
    return b


def test_prefers_visible_ctrl_over_hidden_copy(monkeypatch):
    """可见副本存在时必须用它——绝不能读到其他账户隐藏面板的数字。"""

    def find_ctrl(root, cid, cls=None, visible=False):
        return VISIBLE if visible else HIDDEN

    result = _stubbed_backend(monkeypatch, find_ctrl).get_balance()
    assert result["code"] == 0
    assert set(result["data"].values()) == {"1.23"}


def test_falls_back_to_unfiltered_when_no_visible(monkeypatch):
    """找不到可见控件时放宽兜底（单账户/旧皮肤兼容），不至于整块读空。"""

    def find_ctrl(root, cid, cls=None, visible=False):
        return 0 if visible else HIDDEN

    result = _stubbed_backend(monkeypatch, find_ctrl).get_balance()
    assert result["code"] == 0
    assert set(result["data"].values()) == {"34915.47"}
