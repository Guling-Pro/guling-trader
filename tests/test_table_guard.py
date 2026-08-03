"""表头归属校验（table_guard）——2026-08-03 查询串线事故的判据回归。

事故形态：请求 A 收到表 B，且 status=succeed。这里锁定判据本身：真表放行、
他表拒收、空表头拒收。表头逐字取自事故当天 alerts.log 记录的实得键集。
"""
from trader.ths.table_guard import check_table

POSITION_COLS = ["操作", "证券代码", "证券名称", "股票余额", "可用余额",
                 "冻结数量", "参考成本价", "市价"]
ACTIVE_COLS = ["证券代码", "操作", "委托数量", "委托价格", "成交数量",
               "成交均价", "合同编号", "备注"]
FILLED_COLS = ["成交时间", "证券代码", "证券名称", "操作", "成交数量",
               "成交均价", "成交金额", "合同编号"]
SETTLEMENT_COLS = ["成交日期", "证券代码", "证券名称", "操作", "成交数量",
                   "成交均价", "成交金额", "发生金额", "手续费", "印花税"]


def test_each_table_accepts_itself():
    assert check_table("position", POSITION_COLS) is None
    assert check_table("active_orders", ACTIVE_COLS) is None
    assert check_table("filled_orders", FILLED_COLS) is None
    assert check_table("settlement", SETTLEMENT_COLS) is None


def test_rejects_the_two_tables_seen_in_the_incident():
    """08-03 实得：请求方要资金/持仓，拿到成交明细表或持仓表。"""
    assert check_table("position", FILLED_COLS)        # 要持仓拿到成交表
    assert check_table("active_orders", FILLED_COLS)   # 最险：错表=「无挂单」
    assert check_table("filled_orders", POSITION_COLS)
    assert check_table("active_orders", POSITION_COLS)


def test_reject_reason_names_the_foreign_columns():
    reason = check_table("active_orders", FILLED_COLS)
    assert "他表特征列" in reason
    assert "成交时间" in reason


def test_settlement_not_accepted_as_filled_orders():
    """交割单与成交表共享「成交编号」——靠交割单独有列区分，不能混过。"""
    assert check_table("filled_orders", SETTLEMENT_COLS)


def test_empty_header_rejected():
    assert check_table("position", []) == "表头为空"


def test_unknown_kind_passes_through():
    """本函数只管它登记过的表，不当通用闸门（如自选股 OCR 结果）。"""
    assert check_table("watchlist", ["随便什么"]) is None
