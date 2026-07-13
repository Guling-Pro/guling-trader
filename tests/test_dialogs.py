"""DialogSentry 纯决策逻辑回归（不触碰 Win32，任意平台可跑）。

结构化处置的决策面只有三块：按钮标签归一化、肯定按钮选择、合同编号提取。
这三块错了，真机上点错按钮/丢回执；win32 枚举与点击留给真机联调。
"""
from trader.ths.dialogs import (
    PumpResult,
    choose_button,
    extract_entrust_no,
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


def test_choose_single_button_whatever_label():
    # 信息框的唯一按钮无论叫什么都等价于关闭
    assert choose_button(["知道了"]) == "知道了"


def test_choose_never_picks_negative_among_many():
    # 多按钮且无肯定项 → None（走 Enter/WM_CLOSE 兜底），绝不主动点「取消」
    assert choose_button(["取消", "重试"]) is None
    assert choose_button([]) is None


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


def test_texts_falls_back_to_title():
    r = PumpResult(dialogs=[
        {"title": "提示", "text": "废单：可用资金不足", "action": "click:确定"},
        {"title": "委托确认", "text": "", "action": "click:是"},
    ])
    assert r.texts == ["废单：可用资金不足", "委托确认"]
