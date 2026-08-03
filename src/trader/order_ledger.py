"""下单台账：client_order_id 幂等键 + entrust_no 关联（契约 v2 C4/C5a/C5b）。

为什么必须落盘：幂等要跨受控端重启才有意义——超时后消费侧的安全动作是「原 id 重发」，
若重启就失忆，重发会变成真的重复下单。

**台账不可用一律拒单，禁静默降级**（需求方拍板）：读不到/写不进台账时无法保证幂等，
此时下单等于把重复下单的风险悄悄还给调用方，宁可失败。

三条语义（PROTOCOL.md 同步）：

* `reserve()` 在**点提交之前**写入。所以「已登记但结果未知」是正常态，不是异常态——
  最危险那一刻（点了提交、回执没回来）台账自己也不知道结果，契约不撒谎。
* 同 id 重发：返回首次记录的回执；首次仍在飞则回 submitted_unconfirmed。任一情况下
  **绝不产生第二次点击**。
* 同 id 但参数不同：拒绝并大声报错（invalid_params）。这是调用方的 id 复用 bug，
  静默返回首次回执会让它以为新单下出去了。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 保留窗口：需求方要求 ≥5 交易日；14 自然日在任何长假下都能覆盖。
RETENTION_DAYS = 14

STATE_SUBMITTING = "submitting"   # 已登记、已（或即将）点提交，结果未知
STATE_DONE = "done"               # 首次回执已落定（成功/失败都算落定）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    method          TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    state           TEXT NOT NULL,
    entrust_no      TEXT,
    receipt         TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_entrust ON orders(entrust_no);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
"""


class LedgerUnavailable(RuntimeError):
    """台账不可用（打不开/损坏/写失败）——调用方必须拒单，不得降级为无幂等下单。"""


def fingerprint(method: str, params: dict[str, Any]) -> str:
    """请求指纹：同 id 不同参数要能认出来。"""
    keys = ("stock_no", "amount", "price", "entrust_no")
    payload = {k: params.get(k) for k in keys if params.get(k) is not None}
    return json.dumps({"method": method, **payload}, sort_keys=True, ensure_ascii=False)


class OrderLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._init_db()

    # --- 底层 ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            raise LedgerUnavailable(f"台账打不开（{self.path}）：{e}") from e

    def _init_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
        except sqlite3.Error as e:
            raise LedgerUnavailable(f"台账初始化失败：{e}") from e
        self.purge()

    def purge(self, retention_days: int = RETENTION_DAYS) -> int:
        """清理过期条目。失败只记日志——清不掉不影响幂等正确性。"""
        cutoff = time.time() - retention_days * 86400
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute("DELETE FROM orders WHERE created_at < ?", (cutoff,))
                return cur.rowcount or 0
        except sqlite3.Error:
            logger.warning("台账清理失败（不影响下单）", exc_info=True)
            return 0

    # --- 幂等主路径 ---------------------------------------------------------

    def reserve(self, client_order_id: str, method: str,
                params: dict[str, Any]) -> tuple[str, Optional[dict]]:
        """在点提交之前登记。

        返回 ``(verdict, record)``：

        * ``("new", None)`` —— 首次，可以下单；
        * ``("duplicate", record)`` —— 同 id 同参数重发，**不得再点提交**；
        * ``("conflict", record)`` —— 同 id 不同参数，调用方 id 复用 bug。
        """
        fp = fingerprint(method, params)
        now = time.time()
        try:
            with self._lock, self._connect() as conn:
                try:
                    conn.execute(
                        "INSERT INTO orders (client_order_id, method, fingerprint, state,"
                        " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                        (client_order_id, method, fp, STATE_SUBMITTING, now, now))
                    return "new", None
                except sqlite3.IntegrityError:
                    row = conn.execute(
                        "SELECT * FROM orders WHERE client_order_id = ?",
                        (client_order_id,)).fetchone()
                    if row is None:  # 并发删除，极罕见；当作不可用而非放行
                        raise LedgerUnavailable("台账条目在登记过程中消失")
                    record = _row_to_dict(row)
                    verdict = "duplicate" if row["fingerprint"] == fp else "conflict"
                    return verdict, record
        except sqlite3.Error as e:
            raise LedgerUnavailable(f"台账登记失败：{e}") from e

    def complete(self, client_order_id: str, receipt: dict,
                 entrust_no: Optional[str] = None) -> None:
        """落定首次回执。写失败抛 LedgerUnavailable——单已经下出去了，绝不能静默。"""
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE orders SET state=?, receipt=?, entrust_no=?, updated_at=?"
                    " WHERE client_order_id=?",
                    (STATE_DONE, json.dumps(receipt, ensure_ascii=False),
                     entrust_no, time.time(), client_order_id))
        except sqlite3.Error as e:
            raise LedgerUnavailable(f"台账回写失败：{e}") from e

    def release(self, client_order_id: str) -> None:
        """撤销登记（仅用于「确认没点提交」的前置失败，如参数校验不过）。"""
        try:
            with self._lock, self._connect() as conn:
                conn.execute("DELETE FROM orders WHERE client_order_id=?", (client_order_id,))
        except sqlite3.Error:
            logger.warning("台账撤销登记失败 coid=%s", client_order_id, exc_info=True)

    # --- 读路径 -------------------------------------------------------------

    def get(self, client_order_id: str) -> Optional[dict]:
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM orders WHERE client_order_id=?",
                                   (client_order_id,)).fetchone()
                return _row_to_dict(row) if row else None
        except sqlite3.Error as e:
            raise LedgerUnavailable(f"台账读取失败：{e}") from e

    def coid_by_entrust(self) -> dict[str, str]:
        """entrust_no → client_order_id，供 orders_active/orders_filled 回显 join。

        读失败返回空表：**回显是尽力而为的增强字段**（对账主键是 entrust_no），
        不能因为 join 不上就让查询整体失败。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT entrust_no, client_order_id FROM orders"
                    " WHERE entrust_no IS NOT NULL AND entrust_no != ''").fetchall()
                return {str(r["entrust_no"]): str(r["client_order_id"]) for r in rows}
        except sqlite3.Error:
            logger.warning("台账 entrust_no 映射读取失败，本次不回显 client_order_id",
                           exc_info=True)
            return {}


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("receipt"):
        try:
            d["receipt"] = json.loads(d["receipt"])
        except (TypeError, ValueError):
            d["receipt"] = None
    return d
