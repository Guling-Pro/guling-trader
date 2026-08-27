"""行规范化契约（C3 行结构 / B2 时间 / 在飞判据）。"""
from datetime import datetime, timedelta, timezone

import pytest

from trader.ths import rows

TZ8 = timezone(timedelta(hours=8))

ACTIVE_RAW = {
    "证券代码": "300458", "证券名称": "全志科技", "操作": "买入",
    "委托数量": "500", "委托价格": "35.100", "成交数量": "0",
    "成交均价": "--", "合同编号": "123456", "备注": "已报",
}


def test_active_row_has_pinned_c3_keys():
    row = rows.normalize_active_row(ACTIVE_RAW)
    for key in ("client_order_id", "entrust_no", "证券代码", "证券名称", "方向",
                "委托价", "委托数量", "已成数量", "状态"):
        assert key in row, key
    assert row["委托数量"] == 500          # number，不是字符串
    assert row["委托价"] == 35.1
    assert row["成交均价"] is None         # "--" → null
    assert row["方向"] == "买入"
    assert row["状态"] == "已报"


def test_client_order_id_joined_from_ledger_else_null():
    """C4：coid 由台账 join，join 不上就是 null（外部单/回查失败的单）。"""
    assert rows.normalize_active_row(ACTIVE_RAW, {"123456": "gl-1-7"})["client_order_id"] == "gl-1-7"
    assert rows.normalize_active_row(ACTIVE_RAW, {})["client_order_id"] is None


def test_active_row_accepts_broker_buy_sell_column_alias():
    """实机委托表的“买卖”列也必须进入完整下单回执指纹。"""
    raw = {
        "合同编号": "A-1", "证券代码": "518800", "买卖": "买入",
        "委托数量": "100", "委托价格": "1.000", "成交数量": "0",
        "委托状态": "已报",
    }

    row = rows.normalize_active_row(raw)

    assert row["entrust_no"] == "A-1"
    assert row["方向"] == "买入"
    assert row["委托数量"] == 100
    assert row["委托价"] == 1.0
    assert row["状态"] == "已报"


@pytest.mark.parametrize("note,expected", [
    ("已报", rows.ST_PLACED), ("未报", rows.ST_PENDING), ("部成", rows.ST_PARTIAL),
    ("已成", rows.ST_FILLED), ("已撤", rows.ST_CANCELED), ("废单", rows.ST_REJECTED),
    ("场外撤单中", rows.ST_CANCELED), ("", rows.ST_UNKNOWN), ("XJ状态", rows.ST_UNKNOWN),
])
def test_order_state_classification(note, expected):
    assert rows.classify_order_state(note) == expected


def test_unknown_state_counts_as_in_flight():
    """未识别态按在飞返回——宁可多给一行，也不能把活单藏起来。"""
    assert rows.is_in_flight(rows.ST_UNKNOWN, 500, 0) is True
    assert rows.is_in_flight(rows.ST_PLACED, 500, 0) is True
    assert rows.is_in_flight(rows.ST_PARTIAL, 500, 100) is True
    assert rows.is_in_flight(rows.ST_FILLED, 500, 500) is False
    assert rows.is_in_flight(rows.ST_CANCELED, 500, 0) is False
    # 数量已满也算终态（柜台备注滞后时的兜底）
    assert rows.is_in_flight(rows.ST_PLACED, 500, 500) is False


def test_unknown_state_with_null_qty_still_in_flight():
    """数量读不到（null）时绝不能推断成「已完成」。"""
    assert rows.is_in_flight(rows.ST_UNKNOWN, None, None) is True


def test_filled_time_is_iso_with_local_clock_date():
    now = datetime(2026, 8, 4, 15, 30, tzinfo=TZ8)
    row = rows.normalize_filled_row({"成交时间": "10:43:02", "成交数量": "500",
                                     "成交金额": "17,550.00", "成交均价": "35.100",
                                     "操作": "买入", "合同编号": "1"}, now=now)
    # B2：日期与时区来自本机时钟（成交表只给 HH:MM:SS）
    assert row["成交时间"] == "2026-08-04T10:43:02+08:00"
    assert row["成交数量"] == 500
    assert row["成交金额"] == 17550.0


def test_filled_time_unparsable_is_kept_verbatim():
    assert rows.to_iso_time("盘后固定价") == "盘后固定价"
    assert rows.to_iso_time("--") is None


def test_position_row_types():
    row = rows.normalize_position_row({"证券代码": "600000", "股票余额": "1000",
                                       "可用余额": "1000", "冻结数量": "0",
                                       "参考成本价": "8.100", "市价": "8.230"})
    assert row["股票余额"] == 1000 and isinstance(row["股票余额"], int)
    assert row["参考成本价"] == 8.1
    assert row["冻结数量"] == 0          # 真实的 0 仍是 0，只有占位符才是 null
