"""请求-响应配对校验：抓回来的表必须是本次请求的那张表。

2026-08-03 串线事故：消费侧调 balance 收到成交明细表/持仓表（status=succeed）。
受控端侧的结构成因是——翻页快捷键是**全局按键**（`win32api.keybd_event` 发给当时的
前台窗口），没落到 xiadan 时页面根本没切，grid 里还是上一次查询的表，Ctrl+C 原样抓走；
而 `read_table_text` 的剪贴板序号校验只能保证「不是剪贴板里的陈旧残留」，保证不了
「这是本次请求的那一页」。三个 grid 查询过去只要非空就 `code=0` 出门。

这里只做一件事：**禁 succeed 携错表出门**。判据是表头特征列，两条同时成立才放行：

1. 命中自身特征列（该列在本表必有）；
2. 未命中他表独有特征列（他表有、本表没有的列）。

第 2 条是关键——只查第 1 条的话，未知第四张表混进来照样漏。列名以真机
`parse_table` 解析结果为准，broker 换皮导致列名变了就只改这张表。
"""
from __future__ import annotations

from typing import Iterable, Optional

# 查询 kind → 特征列。取「该表必有」的列，宽松匹配（子串命中即可），
# 容忍 broker 在列名上加前后缀。
TABLE_MARKERS: dict[str, tuple[str, ...]] = {
    # 持仓表：操作/证券代码/证券名称/股票余额/可用余额/冻结数量/参考成本价/市价
    "position": ("股票余额", "参考成本价", "冻结数量"),
    # 委托表：证券代码/操作/委托数量/委托价格/成交数量/成交均价/合同编号/备注
    "active_orders": ("委托数量", "委托价格", "委托状态"),
    # 成交明细表：成交时间/证券代码/证券名称/操作/成交数量/成交均价/成交金额/合同编号
    "filled_orders": ("成交时间", "成交编号"),
    # 交割单：另有 _do_settlement 的自校验，这里登记是为了给上面三张表提供「他表证据」
    "settlement": ("发生金额", "印花税", "成交日期", "成交编号"),
}


def _hit(markers: Iterable[str], columns: Iterable[str]) -> list[str]:
    cols = list(columns)
    return [m for m in markers if any(m in c for c in cols)]


def check_table(kind: str, columns: Iterable[str]) -> Optional[str]:
    """校验表头归属。返回 None=是本次请求的表；否则返回拒收原因（进日志与回执）。

    未登记的 kind 一律放行——本函数只负责它认识的表，不当通用闸门。
    """
    own = TABLE_MARKERS.get(kind)
    if not own:
        return None
    cols = [str(c) for c in columns]
    if not cols:
        return "表头为空"

    foreign_markers = {
        m
        for k, ms in TABLE_MARKERS.items()
        if k != kind
        for m in ms
        if m not in own
    }
    foreign = _hit(sorted(foreign_markers), cols)
    if foreign:
        return f"命中他表特征列 {foreign}（抓到的不是本次请求的表）"
    if not _hit(own, cols):
        return f"未命中本表特征列 {list(own)}"
    return None
