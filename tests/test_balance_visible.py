"""get_balance 可见性过滤回归：多账户下必须读当前账户的可见控件。

2026-07-14 双账户切换演练事故：xiadan 多账户登录时每个账户各挂一套同 ID
资金控件（0x3F4..），只有当前账户的可见。get_balance 原先不过滤可见性，
Alt+2 已切到账户二后仍返回账户一的全套数字——真钱 sizing 的输入被污染。

这里锁定两条纪律：① 只认可见控件；② 读不到就明确失败，绝不回退到未过滤
匹配（兜底在面板加载间隙同样可能抓到其他账户的隐藏副本——宁可失败不可读错）。
Win32 层全部打桩，可跨平台跑。
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
    monkeypatch.setattr(w, "sleep_time", 0)
    monkeypatch.setattr(
        w, "get_text", lambda h: "1.23" if h == VISIBLE else "34915.47"
    )
    return b


def test_reads_only_visible_ctrl(monkeypatch):
    """可见副本存在时必须用它——绝不能读到其他账户隐藏面板的数字。"""

    def find_ctrl(root, cid, cls=None, visible=False):
        return VISIBLE if visible else HIDDEN

    result = _stubbed_backend(monkeypatch, find_ctrl).get_balance()
    assert result["status"] == "succeed"
    # 契约 v2：数值一律 number（元），键名钉死
    assert result["data"]["总资产"] == 1.23
    assert result["data"]["可用金额"] == 1.23


def test_fails_loudly_when_no_visible_ctrl(monkeypatch):
    """无可见控件 → 明确报失败，绝不回退未过滤匹配去读隐藏面板。"""

    def find_ctrl(root, cid, cls=None, visible=False):
        return 0 if visible else HIDDEN

    result = _stubbed_backend(monkeypatch, find_ctrl).get_balance()
    assert result["status"] == "failed"
    assert result["code"] == "read_failed"
    assert "不回退" in result["error"]["message"]
    # 隐藏副本的数字绝不能出现在任何返回里
    assert "34915.47" not in str(result)
