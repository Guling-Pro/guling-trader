"""对外回执契约 v2（消费方契约冻结 C1/C2/C6）。

一处定义信封、机器枚举、错误分类与数值规范化，win/dispatcher 各处只调这里——
契约漂移只可能发生在这一个文件里，`tests/test_contract_envelope.py` 逐条钉死。

信封（所有工具无例外形，含 buy/sell/cancel）::

    {"status": "succeed"|"failed"|"busy",
     "code": <机器枚举串>,
     "data": <载荷或 null>,
     "error": {"class": <枚举>, "broker_msg": <柜台原文或 null>, "message": <我方人话>} | null,
     "contract_version": "2"}

两条容易踩的语义，PROTOCOL.md 同步写死：

* **status=failed 不等于「未提交」**。下单动作超时时真相不可知，此时
  status=failed + code=submitted_unconfirmed + error.class=unknown_outcome。
  调用方的安全动作是**用同一个 client_order_id 原样重发**（幂等，见 order_ledger），
  绝不是改单重下。
* **error.class=unknown 一律不可自动重试**。柜台原文映射是尽力而为的关键词表，
  认不出来就必须认不出来——误判「可重试」会真的重复下单。
"""
from __future__ import annotations

from typing import Any, Optional

CONTRACT_VERSION = "2"

# --- status（C1 冻结为三值）------------------------------------------------
STATUS_SUCCEED = "succeed"
STATUS_FAILED = "failed"
STATUS_BUSY = "busy"

# --- code：机器枚举串 -------------------------------------------------------
CODE_OK = "ok"
CODE_BUSY = "busy"
CODE_CALL_TIMEOUT = "call_timeout"
CODE_SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"   # 已点提交，结果不可知
CODE_REJECTED = "rejected"                             # 柜台明确拒绝
CODE_READ_FAILED = "read_failed"                       # 抓不到数据
CODE_TABLE_MISMATCH = "table_mismatch"                 # 抓到的不是本次请求的表
CODE_NOT_BOUND = "not_bound"                           # 受控端未绑定客户端窗口
CODE_PLUGIN_DISABLED = "plugin_disabled"
CODE_INVALID_PARAMS = "invalid_params"
CODE_LEDGER_UNAVAILABLE = "ledger_unavailable"         # 台账不可用 → 拒单，禁降级
CODE_NOT_FOUND = "not_found"                           # query_order 查无此单
CODE_UNSUPPORTED_METHOD = "unsupported_method"
CODE_INTERNAL_ERROR = "internal_error"
CODE_ABORTED = "aborted"                               # 本笔已被超时作废（代次机制）

# --- error.class（C2 两层分类）---------------------------------------------
# 第一层：结构性判定——由我方自己的控制流得出，可靠。
CLS_BUSY = "busy"
CLS_CALL_TIMEOUT = "call_timeout"
CLS_UNKNOWN_OUTCOME = "unknown_outcome"
CLS_NOT_BOUND = "not_bound"
CLS_PLUGIN_DISABLED = "plugin_disabled"
CLS_READ_FAILED = "read_failed"
CLS_TABLE_MISMATCH = "table_mismatch"
CLS_INVALID_PARAMS = "invalid_params"
CLS_LEDGER_UNAVAILABLE = "ledger_unavailable"
CLS_NOT_FOUND = "not_found"
CLS_INTERNAL_ERROR = "internal_error"
CLS_ABORTED = "aborted"
# 第二层：柜台原文尽力映射——认不出即 unknown，绝不猜。
CLS_INSUFFICIENT_FUNDS = "insufficient_funds"
CLS_PRICE_OUT_OF_LIMIT = "price_out_of_limit"
CLS_INVALID_QUANTITY = "invalid_quantity"
CLS_SUSPENDED = "suspended"
CLS_NO_PERMISSION = "no_permission"
CLS_BROKER_TIMEOUT = "broker_timeout"
CLS_UNKNOWN = "unknown"

# 柜台原文关键词 → class。顺序即优先级（先匹配到的赢）。
# 只登记高置信度词；拿不准的一律落到 unknown——消费侧对 unknown 的处置是
# 「不可自动重试」，误判成可重试会真的重复下单。
_BROKER_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("资金不足", "可用资金不足", "余额不足", "购买力不足"), CLS_INSUFFICIENT_FUNDS),
    (("超出涨跌幅", "价格超出", "涨跌停", "超过涨停", "低于跌停", "价格不在"), CLS_PRICE_OUT_OF_LIMIT),
    (("数量必须", "委托数量", "最小交易单位", "数量不是", "股数", "整数倍"), CLS_INVALID_QUANTITY),
    (("停牌", "暂停交易", "非交易时间", "不在交易时段"), CLS_SUSPENDED),
    (("无权限", "未开通", "未签署", "权限不足", "不具备"), CLS_NO_PERMISSION),
    (("柜台超时", "通讯超时", "网络超时", "请求超时", "服务器繁忙"), CLS_BROKER_TIMEOUT),
)

# 不可自动重试的 class：消费侧据此机械分流（PROTOCOL.md 同步）。
NON_RETRYABLE_CLASSES = frozenset({
    CLS_UNKNOWN, CLS_UNKNOWN_OUTCOME, CLS_INSUFFICIENT_FUNDS, CLS_NO_PERMISSION,
    CLS_INVALID_QUANTITY, CLS_INVALID_PARAMS, CLS_LEDGER_UNAVAILABLE,
})


def classify_broker_message(text: Optional[str]) -> str:
    """柜台原文 → error.class。认不出来就是 unknown，这是有意的。"""
    if not text:
        return CLS_UNKNOWN
    for keywords, cls in _BROKER_PATTERNS:
        for kw in keywords:
            if kw in text:
                return cls
    return CLS_UNKNOWN


# --- 信封构造 ---------------------------------------------------------------

def ok(data: Any = None) -> dict[str, Any]:
    return {"status": STATUS_SUCCEED, "code": CODE_OK, "data": data,
            "error": None, "contract_version": CONTRACT_VERSION}


def fail(code: str, error_class: str, message: str,
         broker_msg: Optional[str] = None, data: Any = None,
         status: str = STATUS_FAILED) -> dict[str, Any]:
    return {"status": status, "code": code, "data": data,
            "error": {"class": error_class, "broker_msg": broker_msg, "message": message},
            "contract_version": CONTRACT_VERSION}


def busy(message: str) -> dict[str, Any]:
    return fail(CODE_BUSY, CLS_BUSY, message, status=STATUS_BUSY)


def broker_rejected(broker_msg: str, message: Optional[str] = None,
                    data: Any = None) -> dict[str, Any]:
    """柜台明确拒绝：class 由原文尽力映射，原文一律原样带回。"""
    return fail(CODE_REJECTED, classify_broker_message(broker_msg),
                message or "柜台拒绝了本次委托", broker_msg=broker_msg, data=data)


def submitted_unconfirmed(message: str, data: Any = None,
                          broker_msg: Optional[str] = None) -> dict[str, Any]:
    """已点提交但结果不可知。调用方唯一安全动作=同 client_order_id 原样重发。"""
    return fail(CODE_SUBMITTED_UNCONFIRMED, CLS_UNKNOWN_OUTCOME, message,
                broker_msg=broker_msg, data=data)


def is_succeed(envelope: Any) -> bool:
    return isinstance(envelope, dict) and envelope.get("status") == STATUS_SUCCEED


# --- C6 数值与单位规范化 -----------------------------------------------------
# THS 一律给字符串，且用 "--" / "" / "-" 表示「没有这个值」。
# 空占位符必须映射 null 而不是 0：0 是一个真实数字，把「没有」写成 0 会被
# 下游当真值用（真钱 sizing 的输入）。

_NULLISH = frozenset({"", "-", "--", "---", "N/A", "n/a", "nan"})


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if s in _NULLISH:
        return None
    return s


def money(value: Any) -> Optional[float]:
    """金额（单位：元），取整到分。"""
    s = _clean(value)
    if s is None:
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def price(value: Any) -> Optional[float]:
    """价格（单位：元），保留到厘——同花顺价格是三位小数。"""
    s = _clean(value)
    if s is None:
        return None
    try:
        return round(float(s), 3)
    except ValueError:
        return None


def qty(value: Any) -> Optional[int]:
    """数量（单位：股）。"""
    s = _clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def pct(value: Any) -> Optional[float]:
    """百分比数值（键名一律以 _pct 结尾，不带 % 符号）。"""
    s = _clean(value)
    if s is None:
        return None
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def text(value: Any) -> Optional[str]:
    return _clean(value)


def direction(value: Any) -> Optional[str]:
    """操作列 → 方向枚举「买入」/「卖出」；认不出保留原文（不猜）。"""
    s = _clean(value)
    if s is None:
        return None
    if "买" in s:
        return "买入"
    if "卖" in s:
        return "卖出"
    return s
