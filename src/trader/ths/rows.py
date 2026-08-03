"""THS 原始表 → 契约 v2 载荷（C3 行结构 / C6 类型与单位 / B2 时间）。

纯函数，不碰 Win32，可跨平台单测。规范化只做三件事：**键名钉死、类型转换、
空占位符映射 null**。认不出来的值一律保留原文或 null，绝不猜测、绝不用 0 兜底。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from ..contract import direction, money, pct, price, qty, text

# --- 委托状态（C3 值域）-----------------------------------------------------
ST_PENDING = "未报"
ST_PLACED = "已报"
ST_PARTIAL = "部成"
ST_FILLED = "已成"
ST_CANCELED = "已撤"
ST_REJECTED = "废单"
ST_UNKNOWN = "未知"

ORDER_STATES = (ST_PENDING, ST_PLACED, ST_PARTIAL, ST_FILLED,
                ST_CANCELED, ST_REJECTED, ST_UNKNOWN)

# 终态：不再可能继续成交，orders_active 不返回这些行。
TERMINAL_STATES = frozenset({ST_FILLED, ST_CANCELED, ST_REJECTED})

# 柜台备注原文 → 状态。**认不出即 ST_UNKNOWN，且 unknown 按「在飞」保守返回**：
# 宁可多给消费侧一行让它看见，也不能把一张活着的挂单藏起来——那正是孤儿挂单
# 架空止损哨兵的失效路径（2026-08-03 事故分析结论）。
_STATE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("已撤", ST_CANCELED), ("部撤", ST_CANCELED), ("撤单", ST_CANCELED),
    ("废单", ST_REJECTED), ("无效", ST_REJECTED), ("拒绝", ST_REJECTED),
    ("部成", ST_PARTIAL), ("部分成交", ST_PARTIAL),
    ("已成", ST_FILLED), ("全部成交", ST_FILLED), ("成交", ST_FILLED),
    ("已报", ST_PLACED), ("已申报", ST_PLACED),
    ("未报", ST_PENDING), ("待报", ST_PENDING),
)


def classify_order_state(note: Any) -> str:
    s = text(note)
    if not s:
        return ST_UNKNOWN
    for kw, state in _STATE_PATTERNS:
        if kw in s:
            return state
    return ST_UNKNOWN


def is_in_flight(state: str, order_qty: Optional[int], filled_qty: Optional[int]) -> bool:
    """是否仍在飞。未知态一律算在飞（保守）。"""
    if state in TERMINAL_STATES:
        return False
    if order_qty and filled_qty is not None and filled_qty >= order_qty:
        return False
    return True


# --- B2 成交时间 -------------------------------------------------------------
# THS 成交表只给 "HH:MM:SS"（无日期）。补齐的日期与时区**来自受控端本机时钟**，
# 不是柜台时间——契约里写明，消费侧对账时按此理解。

_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_DATE_TIME = re.compile(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})[ T]+(\d{1,2}:\d{2}(:\d{2})?)$")


def to_iso_time(value: Any, now: Optional[datetime] = None) -> Optional[str]:
    """成交时间 → 带时区偏移的 ISO 8601。认不出的格式原样返回。"""
    s = text(value)
    if not s:
        return None
    now = now or datetime.now().astimezone()
    tz = now.tzinfo
    m = _DATE_TIME.match(s)
    if m:
        y, mo, d, hms = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        parts = [int(x) for x in hms.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return datetime(y, mo, d, *parts, tzinfo=tz).isoformat()
    if _TIME_ONLY.match(s):
        parts = [int(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return datetime(now.year, now.month, now.day, *parts, tzinfo=tz).isoformat()
    return s


# --- 各表行规范化 ------------------------------------------------------------

def normalize_balance(raw: dict[str, Any]) -> dict[str, Any]:
    """资金面板：全部转 number（元），带 % 的键更名为 _pct。"""
    return {
        "资金余额": money(raw.get("资金余额")),
        "冻结金额": money(raw.get("冻结金额")),
        "可用金额": money(raw.get("可用金额")),
        "可取金额": money(raw.get("可取金额")),
        "股票市值": money(raw.get("股票市值")),
        "总资产": money(raw.get("总资产")),
        "持仓盈亏": money(raw.get("持仓盈亏")),
        "当日盈亏": money(raw.get("当日盈亏")),
        "当日盈亏比_pct": pct(raw.get("当日盈亏比")),
    }


def normalize_position_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "证券代码": text(row.get("证券代码")),
        "证券名称": text(row.get("证券名称")),
        "股票余额": qty(row.get("股票余额")),
        "可用余额": qty(row.get("可用余额")),
        "冻结数量": qty(row.get("冻结数量")),
        "参考成本价": price(row.get("参考成本价")),
        "市价": price(row.get("市价")),
        "market_value": money(row.get("最新市值") or row.get("市值")),
        "浮动盈亏": money(row.get("浮动盈亏") or row.get("盈亏")),
        "盈亏比例_pct": pct(row.get("盈亏比例") or row.get("盈亏比(%)")),
    }


def normalize_active_row(row: dict[str, Any],
                         coid_by_entrust: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """委托行 → C3 钉死结构。client_order_id 由台账 join，join 不上即 null。"""
    entrust_no = text(row.get("合同编号") or row.get("委托编号"))
    order_qty = qty(row.get("委托数量"))
    filled_qty = qty(row.get("成交数量"))
    state = classify_order_state(row.get("备注") or row.get("状态") or row.get("委托状态"))
    return {
        "client_order_id": (coid_by_entrust or {}).get(entrust_no or ""),
        "entrust_no": entrust_no,
        "证券代码": text(row.get("证券代码")),
        "证券名称": text(row.get("证券名称")),
        "方向": direction(row.get("操作") or row.get("买卖标志")),
        "委托价": price(row.get("委托价格") or row.get("委托价")),
        "委托数量": order_qty,
        "已成数量": filled_qty,
        "成交均价": price(row.get("成交均价")),
        "状态": state,
        "柜台备注": text(row.get("备注")),
    }


def normalize_filled_row(row: dict[str, Any],
                         coid_by_entrust: Optional[dict[str, str]] = None,
                         now: Optional[datetime] = None) -> dict[str, Any]:
    entrust_no = text(row.get("合同编号") or row.get("委托编号"))
    return {
        "client_order_id": (coid_by_entrust or {}).get(entrust_no or ""),
        "entrust_no": entrust_no,
        "成交编号": text(row.get("成交编号")),
        "成交时间": to_iso_time(row.get("成交时间"), now),
        "证券代码": text(row.get("证券代码")),
        "证券名称": text(row.get("证券名称")),
        "方向": direction(row.get("操作")),
        "成交数量": qty(row.get("成交数量")),
        "成交均价": price(row.get("成交均价")),
        "成交金额": money(row.get("成交金额")),
    }


def normalize_settlement_row(row: dict[str, Any],
                             now: Optional[datetime] = None) -> dict[str, Any]:
    """交割单：列因券商而异，钉死已知列，未知列原样保留（低频复盘工具，宁可多带）。"""
    known = {
        "成交日期": to_iso_time(row.get("成交日期") or row.get("日期"), now),
        "证券代码": text(row.get("证券代码")),
        "证券名称": text(row.get("证券名称")),
        "方向": direction(row.get("操作")),
        "成交数量": qty(row.get("成交数量") or row.get("数量")),
        "成交均价": price(row.get("成交均价") or row.get("均价")),
        "成交金额": money(row.get("成交金额") or row.get("金额")),
        "发生金额": money(row.get("发生金额")),
        "手续费": money(row.get("手续费")),
        "印花税": money(row.get("印花税")),
    }
    extras = {k: text(v) for k, v in row.items() if k not in _SETTLEMENT_MAPPED}
    if extras:
        known["其它列"] = extras
    return known


_SETTLEMENT_MAPPED = frozenset({
    "成交日期", "日期", "证券代码", "证券名称", "操作", "成交数量", "数量",
    "成交均价", "均价", "成交金额", "金额", "发生金额", "手续费", "印花税",
})
