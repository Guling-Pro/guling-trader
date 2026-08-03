"""契约 v2 信封与类型规范（C1/C2/C6）——冻结件的直接回归哨。

冻结的前提是漂移可检测：这些断言就是「漂移探测器」，改动信封形状必然打红。
"""
import json

import pytest

from trader import contract
from trader.ths import rows

ALL_TOOLS_ENVELOPES = [
    contract.ok({"any": 1}),
    contract.ok([]),
    contract.fail(contract.CODE_READ_FAILED, contract.CLS_READ_FAILED, "读不到"),
    contract.busy("忙"),
    contract.broker_rejected("可用资金不足"),
    contract.submitted_unconfirmed("不可知"),
]


@pytest.mark.parametrize("env", ALL_TOOLS_ENVELOPES)
def test_envelope_shape_is_uniform(env):
    """C1：所有回执同形，无例外形。"""
    assert set(env) == {"status", "code", "data", "error", "contract_version"}
    assert env["status"] in ("succeed", "failed", "busy")
    assert isinstance(env["code"], str) and env["code"]
    assert env["contract_version"] == "2"
    if env["status"] == "succeed":
        assert env["error"] is None
    else:
        assert set(env["error"]) == {"class", "broker_msg", "message"}
        assert isinstance(env["error"]["class"], str)


@pytest.mark.parametrize("env", ALL_TOOLS_ENVELOPES)
def test_envelope_is_json_serializable(env):
    json.dumps(env, ensure_ascii=False)


def test_failed_does_not_mean_not_submitted():
    """最容易踩的语义：submitted_unconfirmed 是 failed，但**不代表没提交**。"""
    env = contract.submitted_unconfirmed("超时", data={"submitted": True})
    assert env["status"] == "failed"
    assert env["code"] == "submitted_unconfirmed"
    assert env["error"]["class"] == "unknown_outcome"
    assert env["data"]["submitted"] is True
    assert contract.CLS_UNKNOWN_OUTCOME in contract.NON_RETRYABLE_CLASSES


# --- C2 两层错误分类 ---------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("可用资金不足，无法委托", contract.CLS_INSUFFICIENT_FUNDS),
    ("委托价格超出涨跌幅限制", contract.CLS_PRICE_OUT_OF_LIMIT),
    ("委托数量必须是100的整数倍", contract.CLS_INVALID_QUANTITY),
    ("该证券今日停牌", contract.CLS_SUSPENDED),
    ("未开通科创板交易权限", contract.CLS_NO_PERMISSION),
    ("柜台超时，请稍后重试", contract.CLS_BROKER_TIMEOUT),
])
def test_broker_message_classification(text, expected):
    assert contract.classify_broker_message(text) == expected


@pytest.mark.parametrize("text", ["", None, "系统提示：请联系客户经理", "XJ-2049"])
def test_unrecognized_broker_message_is_unknown(text):
    """认不出就必须是 unknown——误判「可重试」会真的重复下单。"""
    assert contract.classify_broker_message(text) == contract.CLS_UNKNOWN
    assert contract.CLS_UNKNOWN in contract.NON_RETRYABLE_CLASSES


def test_broker_rejected_keeps_raw_text():
    env = contract.broker_rejected("可用资金不足，无法委托")
    assert env["error"]["broker_msg"] == "可用资金不足，无法委托"
    assert env["error"]["class"] == contract.CLS_INSUFFICIENT_FUNDS


# --- C6 类型与单位 -----------------------------------------------------------

@pytest.mark.parametrize("raw", ["--", "", "-", "N/A", None])
def test_placeholder_maps_to_null_never_zero(raw):
    """空占位符一律 null。映射成 0 会被下游当真值用（真钱 sizing 的输入）。"""
    assert contract.money(raw) is None
    assert contract.qty(raw) is None
    assert contract.price(raw) is None
    assert contract.pct(raw) is None


def test_number_parsing_units_and_rounding():
    assert contract.money("1,234.567") == 1234.57      # 金额取整到分
    assert contract.price("35.1234") == 35.123         # 价格到厘
    assert contract.qty("500") == 500
    assert contract.qty("500.0") == 500
    assert contract.pct("-3.25%") == -3.25


def test_direction_enum_falls_back_to_raw():
    assert contract.direction("证券买入") == "买入"
    assert contract.direction("卖出") == "卖出"
    assert contract.direction("融券回购") == "融券回购"   # 认不出保留原文，不猜


def test_balance_keys_are_pinned_and_pct_renamed():
    out = rows.normalize_balance({"总资产": "1,000.00", "当日盈亏比": "1.23%",
                                  "可用金额": "--"})
    assert out["总资产"] == 1000.0
    assert out["当日盈亏比_pct"] == 1.23
    assert "当日盈亏比" not in out          # 带 % 的键名已更名
    assert out["可用金额"] is None          # 不是 0
