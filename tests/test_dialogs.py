"""DialogSentry 纯决策逻辑回归（不触碰 Win32，任意平台可跑）。

覆盖按钮标签归一化、弹窗分类、未知弹窗记录和合同编号提取。
Win32 枚举与真实客户端行为留给 Windows 真机联调。
"""
from trader.ths.dialogs import (
    DialogFingerprint,
    DialogSentry,
    PumpResult,
    classify_dialog,
    choose_button,
    extract_entrust_no,
    is_known_captcha,
    is_known_cancel_confirmation,
    is_known_confirmation,
    normalize_button_label,
)


# ---- 按钮标签归一化 --------------------------------------------------------

def test_normalize_strips_accelerator_suffix():
    assert normalize_button_label("是(Y)") == "是"
    assert normalize_button_label("否(N)") == "否"
    assert normalize_button_label("确定(&O)") == "确定"
    assert normalize_button_label("是(&Y)") == "是"
    assert normalize_button_label("确定（Y）") == "确定"  # 全角括号


def test_normalize_strips_spaces_and_amp():
    assert normalize_button_label("确 定") == "确定"
    assert normalize_button_label("&确定") == "确定"
    assert normalize_button_label("  是  ") == "是"
    assert normalize_button_label("") == ""
    assert normalize_button_label(None) == ""


# ---- 肯定按钮选择 ----------------------------------------------------------

def test_choose_prefers_yes_over_ok():
    # 委托确认框：是(Y)/否(N) → 点「是」
    assert choose_button(["是", "否"]) == "是"
    assert choose_button(["否", "是"]) == "是"


def test_choose_ok_dialog():
    # 结果/提示框：单「确定」
    assert choose_button(["确定"]) == "确定"


def test_choose_single_button_keeps_legacy_handling():
    # 原逻辑：信息框唯一按钮无论叫什么都等价于关闭。
    assert choose_button(["知道了"]) == "知道了"


def test_choose_never_picks_negative_among_many():
    # 多按钮且无肯定项 → None，绝不主动点「取消」
    assert choose_button(["取消", "重试"]) is None
    assert choose_button([]) is None


# ---- 弹窗安全分类 ----------------------------------------------------------

def _dialog(*, title="", text="", buttons=None, has_edit=False):
    return DialogFingerprint(
        hwnd=100,
        title=title,
        text=text,
        buttons=buttons or {},
        has_edit=has_edit,
    )


def test_known_copy_captcha_is_the_only_edit_dialog_allowed_to_use_ocr():
    dlg = _dialog(text="检测到您正在拷贝数据，请输入验证码", has_edit=True)
    assert is_known_captcha(dlg)
    assert classify_dialog(dlg) == "known_captcha"


def test_unknown_edit_dialog_is_recorded_as_unknown():
    # 原逻辑仍会 OCR；分类只用于反馈未知类型。
    dlg = _dialog(title="安全验证", text="请输入交易密码", has_edit=True)
    assert not is_known_captcha(dlg)
    assert classify_dialog(dlg) == "unknown_edit"


def test_unique_button_is_unknown_but_still_handled():
    dlg = _dialog(title="提示", text="风险提示", buttons={"确定": 101})
    assert not is_known_confirmation(dlg)
    assert classify_dialog(dlg) == "unknown"


def test_known_confirmation_requires_affirmative_and_negative_buttons():
    assert is_known_confirmation(_dialog(buttons={"是": 101, "否": 102}))
    assert is_known_confirmation(_dialog(buttons={"确定": 101, "取消": 102}))
    assert not is_known_confirmation(_dialog(buttons={"确定": 101}))


def test_cancel_confirmation_with_reprice_edit_is_not_sent_to_ocr(monkeypatch):
    import trader.ths.dialogs as dialogs_module

    calls = []

    class Api:
        def PostMessage(self, *args):
            calls.append(args)

    class Con:
        BM_CLICK = 1

    class Backend:
        ocr_called = False

        def input_ocr(self):
            self.ocr_called = True

    monkeypatch.setattr(dialogs_module, "win32api", Api(), raising=False)
    monkeypatch.setattr(dialogs_module, "win32con", Con(), raising=False)
    dlg = _dialog(
        text="您是否确定以上撤销买入委托？\n撤单确认\n(撤单并以新的价格委托)",
        buttons={"否": 102, "是": 101},
        has_edit=True,
    )
    backend = Backend()

    assert is_known_cancel_confirmation(dlg)
    assert classify_dialog(dlg) == "known_cancel_confirmation"
    assert DialogSentry(backend).dismiss(dlg) == "click:是"
    assert backend.ocr_called is False
    assert calls == [(101, 1, 0, 0)]


def test_cancel_confirmation_without_affirmative_button_stays_pending():
    class Backend:
        ocr_called = False

        def input_ocr(self):
            self.ocr_called = True

    dlg = _dialog(
        text="您是否确定以上撤销买入委托？\n撤单确认",
        buttons={"取消": 102},
        has_edit=True,
    )
    backend = Backend()

    assert DialogSentry(backend).dismiss(dlg) == "pending:cancel_confirmation_no_affirmative"
    assert backend.ocr_called is False


def test_dismiss_unknown_edit_keeps_legacy_ocr():
    class Backend:
        def input_ocr(self):
            self.called = True

    backend = Backend()
    action = DialogSentry(backend).dismiss(
        _dialog(title="身份验证", text="请输入动态口令", has_edit=True)
    )
    assert action == "input_ocr"
    assert backend.called is True


def test_dismiss_unknown_dialog_keeps_legacy_enter_fallback(monkeypatch):
    import trader.ths.dialogs as dialogs_module

    calls = []

    class Api:
        def PostMessage(self, *args):
            calls.append(args)

    class Con:
        BM_CLICK = 1
        WM_KEYDOWN = 2
        WM_KEYUP = 3
        WM_CLOSE = 4
        VK_RETURN = 13

    monkeypatch.setattr(dialogs_module, "win32api", Api(), raising=False)
    monkeypatch.setattr(dialogs_module, "win32con", Con(), raising=False)
    class Gui:
        def IsWindow(self, hwnd):
            return False
        def IsWindowVisible(self, hwnd):
            return False

    monkeypatch.setattr(dialogs_module, "win32gui", Gui(), raising=False)
    action = DialogSentry(object()).dismiss(
        _dialog(title="未知提示", text="请人工判断", buttons={"继续": 101})
    )
    assert action == "click:继续"
    assert calls == [(101, 1, 0, 0)]


def test_pump_records_unknown_dialog_and_continues(monkeypatch):
    import trader.ths.dialogs as dialogs_module

    dlg = _dialog(title="未知提示", text="请人工判断", buttons={"继续": 101})
    sentry = DialogSentry(object())
    monkeypatch.setattr(
        dialogs_module,
        "win32api",
        type("Api", (), {"PostMessage": lambda self, *args: None})(),
        raising=False,
    )
    monkeypatch.setattr(
        dialogs_module,
        "win32con",
        type("Con", (), {"BM_CLICK": 1})(),
        raising=False,
    )
    monkeypatch.setattr(sentry, "scan", lambda: [dlg])

    result = sentry.pump(budget=1.0, settle=0.0)

    assert result.dialogs
    assert all(item["unknown"] is True for item in result.dialogs)
    assert all(item["reason"] == "unknown" for item in result.dialogs)
    assert result.unknown_dialogs == result.dialogs


def test_known_confirmation_still_clicks_affirmative_button(monkeypatch):
    import trader.ths.dialogs as dialogs_module

    calls = []

    class Api:
        def PostMessage(self, *args):
            calls.append(args)

    class Con:
        BM_CLICK = 1

    monkeypatch.setattr(dialogs_module, "win32api", Api(), raising=False)
    monkeypatch.setattr(dialogs_module, "win32con", Con(), raising=False)
    action = DialogSentry(object()).dismiss(
        _dialog(title="委托确认", buttons={"否": 102, "是": 101})
    )
    assert action == "click:是"
    assert calls == [(101, 1, 0, 0)]


# ---- 合同编号提取 ----------------------------------------------------------

def test_extract_entrust_no_variants():
    assert extract_entrust_no("您的买入委托已成功提交，合同编号：12345。") == "12345"
    assert extract_entrust_no("合同编号: 67890") == "67890"
    assert extract_entrust_no("合同编号889900") == "889900"
    assert extract_entrust_no("可用资金不足") is None
    assert extract_entrust_no("") is None
    assert extract_entrust_no(None) is None


# ---- PumpResult 回执附加 ---------------------------------------------------

def test_attach_to_adds_dialogs_only_when_present():
    r = PumpResult()
    receipt = r.attach_to({"code": 0})
    assert "dialogs" not in receipt  # 无弹窗不加字段，回执保持干净

    r2 = PumpResult(dialogs=[{"title": "提示", "text": "请选择意向申报委托", "action": "click:确定"}])
    receipt2 = r2.attach_to({"code": 0})
    assert receipt2["dialogs"][0]["action"] == "click:确定"

    pending = PumpResult(
        dialogs=[{"title": "身份验证", "text": "请输入动态口令",
                  "action": "input_ocr", "unknown": True,
                  "reason": "unknown_edit"}],
        unknown_dialogs=[{"title": "身份验证", "text": "请输入动态口令",
                          "action": "input_ocr", "unknown": True,
                          "reason": "unknown_edit"}],
    )
    pending_receipt = pending.attach_to({"code": 2})
    assert pending_receipt["unknown_dialog"] is True
    assert pending_receipt["unknown_dialogs"][0]["reason"] == "unknown_edit"


def test_texts_falls_back_to_title():
    r = PumpResult(dialogs=[
        {"title": "提示", "text": "废单：可用资金不足", "action": "click:确定"},
        {"title": "委托确认", "text": "", "action": "click:是"},
    ])
    assert r.texts == ["废单：可用资金不足", "委托确认"]
