"""三张 grid 表的请求-响应配对校验（禁 succeed 携错表出门）。

2026-08-03 串线事故的正面修复：翻页快捷键是全局按键，没落到 xiadan 时 grid 里
还是上一次查询的表，Ctrl+C 原样抓走；过去非空即 code=0。这里锁定四条：
① 正表照常返回；② 空表（今天无挂单/无成交）仍是成功、data=[]；
③ 错表拒收且带 got_columns；④ 首次抓错、重抓抓对 → 返回对的那张。
Win32 层全部打桩，可跨平台跑。
"""
import pytest

from trader.ths import win as w
from trader.ths.win import WinThsBackend, _VERIFIED_EMPTY_GRID

POSITION_TABLE = (
    "操作\t证券代码\t证券名称\t股票余额\t可用余额\t冻结数量\t参考成本价\t市价\t\r\n"
    "卖出\t600000\t浦发银行\t1000\t1000\t0\t8.100\t8.230\t\r\n"
)
ACTIVE_TABLE = (
    "证券代码\t操作\t委托数量\t委托价格\t成交数量\t成交均价\t合同编号\t备注\t\r\n"
    "300458\t买入\t500\t35.100\t0\t0.000\t123456\t已报\t\r\n"
)
FILLED_TABLE = (
    "成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额\t合同编号\t\r\n"
    "10:43:02\t300458\t全志科技\t买入\t500\t35.100\t17550.00\t123456\t\r\n"
)
EMPTY_ACTIVE_TABLE = (  # 无挂单时 THS 照样拷出表头 + 空占位行
    "证券代码\t操作\t委托数量\t委托价格\t成交数量\t成交均价\t合同编号\t备注\t\r\n"
    "\t\t\t\t\t\t\t\t\r\n"
)
EMPTY_ACTIVE_TABLE_WITH_DISPLAY_ROW = (
    # 实机皮肤可能在空表中残留委托时间、成交价格等展示值；核心订单字段全空/零。
    "委托时间\t证券代码\t证券名称\t买卖\t委托状态\t委托数量\t成交数量\t委托价格\t"
    "成交价格\t已撤数量\t合同编号\t交易市场\t\r\n"
    "11:26:48\t\t\t\t\t0\t0\t0\t0\t0\t\t\t\r\n"
)
ACTIVE_TABLE_MISSING_CONTRACT = (
    "委托时间\t证券代码\t证券名称\t买卖\t委托状态\t委托数量\t成交数量\t委托价格\t"
    "成交价格\t已撤数量\t合同编号\t交易市场\t\r\n"
    "11:26:48\t518800\t示例基金\t买入\t已报\t100\t0\t5.000\t0\t0\t\t沪A\t\r\n"
)


def _backend(monkeypatch, texts):
    """texts=每次 read_table_text 依次返回的剪贴板文本。"""
    b = WinThsBackend()
    b.hwnd_main = 1
    seq = list(texts)
    monkeypatch.setattr(b, "switch_to_normal", lambda: None)
    monkeypatch.setattr(b, "refresh", lambda: None)
    monkeypatch.setattr(b, "get_right_hwnd", lambda: 999)
    monkeypatch.setattr(b, "_find_grid", lambda hwnd: 888)
    monkeypatch.setattr(b, "read_table_text", lambda ctrl: seq.pop(0) if seq else None)
    monkeypatch.setattr(w, "hot_key", lambda keys: None)
    monkeypatch.setattr(w, "_activate_window", lambda hwnd: None)
    monkeypatch.setattr(w, "sleep_time", 0)
    return b


@pytest.mark.parametrize("method,table", [
    ("get_position", POSITION_TABLE),
    ("get_active_orders", ACTIVE_TABLE),
    ("get_filled_orders", FILLED_TABLE),
])
def test_correct_table_returns_succeed(monkeypatch, method, table):
    b = _backend(monkeypatch, [table])
    r = getattr(b, method)()
    assert r["status"] == "succeed"
    assert r["contract_version"] == "2"
    assert len(r["data"]) == 1


def test_empty_table_is_still_success(monkeypatch):
    """空委托表=「今天真的没挂单」，必须 code=0 data=[]——不能被校验误杀，
    否则消费侧拿不到数据，与错表一样致盲。"""
    b = _backend(monkeypatch, [EMPTY_ACTIVE_TABLE])
    r = b.get_active_orders()
    assert r["status"] == "succeed"
    assert r["data"] == []


def test_empty_order_display_row_is_not_mistaken_for_missing_contract(monkeypatch):
    """空委托表即使保留时间展示行，限价单基线也应安全视为无历史订单。"""
    b = _backend(monkeypatch, [EMPTY_ACTIVE_TABLE_WITH_DISPLAY_ROW])

    baseline, error = b._read_limit_order_baseline()

    assert error is None
    assert baseline == set()
    assert b._last_grid_columns["active_orders"] == (
        "委托时间", "证券代码", "证券名称", "买卖", "委托状态", "委托数量",
        "成交数量", "委托价格", "成交价格", "已撤数量", "合同编号", "交易市场",
    )


def test_limit_baseline_error_logs_clipboard_and_normalized_rows(monkeypatch, caplog):
    """合同号异常时，trader.log 必须能还原本次复制原文与最终订单行。"""
    b = _backend(monkeypatch, [ACTIVE_TABLE_MISSING_CONTRACT])

    baseline, error = b._read_limit_order_baseline()

    assert baseline is None
    assert error["code"] == "table_mismatch"
    rendered = caplog.text
    assert "[LIMIT_BASELINE_DEBUG]" in rendered
    assert "clipboard_text" in rendered
    assert "normalized_rows" in rendered
    assert "missing_contract_row_indexes" in rendered
    assert "518800" in rendered


def test_no_header_empty_table_is_success_only_with_verified_marker(monkeypatch):
    """验证码已处理、空表却没输出表头时，才能明确返回无挂单。"""
    b = _backend(monkeypatch, [_VERIFIED_EMPTY_GRID])
    r = b.get_active_orders()
    assert r["status"] == "succeed"
    assert r["data"] == []
    assert "active_orders" in b._last_grid_verified_empty
    assert b._last_grid_columns.get("active_orders") is None


def test_plain_clipboard_failure_is_not_empty_table(monkeypatch):
    """普通 None 没有经过验证码闭环，必须保持读取失败。"""
    b = _backend(monkeypatch, [None] * WinThsBackend._GRID_ATTEMPTS)
    r = b.get_active_orders()
    assert r["status"] == "failed"
    assert r["code"] == "read_failed"


@pytest.mark.parametrize("method,wrong", [
    ("get_position", FILLED_TABLE),
    ("get_active_orders", FILLED_TABLE),   # 最险：错表会被读成「无挂单」
    ("get_filled_orders", POSITION_TABLE),
])
def test_wrong_table_never_returns_succeed(monkeypatch, method, wrong):
    b = _backend(monkeypatch, [wrong] * WinThsBackend._GRID_ATTEMPTS)
    r = getattr(b, method)()
    assert r["status"] == "failed"
    assert r["code"] == "table_mismatch"
    assert r["error"]["class"] == "table_mismatch"
    assert "不是本次请求的表" in r["error"]["message"]
    assert r["data"]["got_columns"]      # 诊断用：实得表头进回执
    assert "rows" not in r["data"]       # 错表的行绝不出门


def test_retry_recovers_when_page_finally_switches(monkeypatch):
    """首次翻页键没落到 xiadan（抓到上一张表），重抓一次就对了。"""
    b = _backend(monkeypatch, [FILLED_TABLE, ACTIVE_TABLE])
    r = b.get_active_orders()
    assert r["status"] == "succeed"
    assert r["data"][0]["entrust_no"] == "123456"


def test_active_orders_uses_f1_f8_hotkeys(monkeypatch):
    b = _backend(monkeypatch, [ACTIVE_TABLE])
    keys = []
    monkeypatch.setattr(w, "hot_key", lambda value: keys.append(value))

    r = b.get_active_orders()

    assert r["status"] == "succeed"
    assert keys == [["F1"], ["F8"]]


def test_clipboard_failure_keeps_old_message(monkeypatch):
    """抓不到文本（验证码/拷贝没落定）仍是原来的读取失败语义，不误报错表。"""
    b = _backend(monkeypatch, [])
    r = b.get_position()
    assert r["status"] == "failed"
    assert r["code"] == "read_failed"
    assert "读取数据失败" in r["error"]["message"]


def test_wrong_table_is_not_written_into_state(monkeypatch):
    """last-known 内存态也不许被错表污染。"""
    b = _backend(monkeypatch, [FILLED_TABLE] * WinThsBackend._GRID_ATTEMPTS)
    b.get_active_orders()
    assert b.state.get("active_orders") is None
