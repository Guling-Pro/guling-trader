# 委托/成交事件 WS 主动推送 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** trader 在本机周期性快照券商委托表,diff 出"下单/部分成交/全成/撤单"事件(含手机端、Windows 手动、agent 三种来源),经现有 WS 隧道主动推 `order_event` 帧给上游,把上游从"轮询查成交"变为"事件驱动"。

**Architecture:** 新增独立模块 `order_watch.py`,把"解析快照 / diff 状态机 / 交易时段判断"做成纯函数(可离线单测),异步任务 `order_watch_task` 只是薄壳:轮询 `backend.orders_active()` → `build_snapshot` → `diff_snapshots` → `client.send_frame`。所有对 THS 单窗口的访问(轮询 + RPC 下单/查询)共用 backend 上的一把 `asyncio.Lock` 串行化,避免两个 `copy_table` 并发抢前台/剪贴板导致串读。

**Tech Stack:** Python 3.11,asyncio,websockets(已有);纯标准库,**不引入新依赖**。

## Global Constraints

- **Python 版本**:3.11(CI 用 `actions/setup-python@v5` python 3.11);不得用 3.12+ 专属语法。
- **不引入新依赖**:只用标准库 + 仓库已有的 `websockets`/`asyncio`。
- **不改 WS 协议握手、不改现有 RPC 路径**:`ws_client._main_loop` / `handshake` / `dispatcher.handle_call` 的 reply 信封语义保持不变;本任务只在 `handle_call` 外围加锁 + 登记 entrust_no,在 `_async_main` 增加一个 task。
- **不做交易决策**:trader 侧不做追价/撤单/改价判断,不加任何安全/风控门。只探测并推送事件。
- **字段名用真实 THS 表头**,逐字使用,禁止臆造 key。委托表(今日委托,F1/F8)确认含列:`证券代码`、`操作`、`委托数量`、`委托价格`、`成交数量`、`成交均价`、`合同编号`、`备注`(其中 `证券代码/操作/委托数量/委托价格/合同编号/备注` 已在 `src/trader/ths/win.py:854 _lookup_entrust_no` 中作为真实列名使用并确认)。
- **自适应降频(分钟级,RPA 友好)**:不固定周期。空闲(无未完成委托)时 `idle=300s`(5 分钟);有未完成委托挂着时提速到 `active=60s`(1 分钟,为及时抓成交)。两值可经 `TraderConfig.order_watch_idle_secs` / `order_watch_active_secs` 配置(重启生效)。**核心理由(验证码)**:每次 `orders_active` 都做 `F5 重查 + 切表`,THS 会间歇弹验证码需 OCR 识别填写,轮询越密 → 验证码触发越多 → OCR 负担/失败率越高,还可能在用户或 agent RPC 要用窗口时卡在验证码上。故必须分钟级:空闲 5min(验证码很少),仅在确有未完成委托、需要抓成交时提速到 1min。每轮只读一张表(不叠加 orders_filled),进一步减少验证码触发。
- **重启只建基线**:进程重启后第一轮成功轮询只记录基线快照、不补发历史事件,`seq` 用进程内单调计数从 0 起。
- **前置确认(进入 Task 7 前必须完成)**:① 今日委托表(F1/F8)确认**保留**当日已成/已撤的委托行(`备注` 随状态变为 `已成`/`已撤`),而非全成后从表消失——本方案的状态机依赖此性质;② `order_event` 帧的 `type` 与字段集需与上游 yu-agent 达成一致。两点都在 Task 7 live smoke 中验证;若 ① 不成立(全成后行消失),需回到 Task 3 增加"消失=查成交表确认全成"的兜底分支。

---

### Task 1: order_watch 模块骨架 + 交易时段判断

**Files:**
- Create: `src/trader/order_watch.py`
- Test: `tests/test_order_watch.py`

**Interfaces:**
- Produces:
  - `IDLE_INTERVAL_DEFAULT: int`(= 300,即 5 分钟)、`ACTIVE_INTERVAL_DEFAULT: int`(= 60,即 1 分钟)
  - `FRAME_TYPE: str`(= `"order_event"`)
  - 列名常量 `COL_CODE/COL_OP/COL_ORDER_QTY/COL_ORDER_PRICE/COL_FILLED_QTY/COL_AVG_PRICE/COL_ENTRUST_NO/COL_NOTE`
  - `in_trading_session(now: datetime) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_order_watch.py
"""order_watch 纯函数单测：交易时段 / 快照解析 / diff 状态机 / 轮询薄壳。

沿用本仓库测试约定：同步测试，async 用 asyncio.run 驱动，不依赖 pytest-asyncio。
"""
from datetime import datetime

from trader import order_watch


def test_in_trading_session_morning_and_afternoon():
    # 周三 10:00 / 14:00 在盘中；12:00 午休不在；08:00 盘前不在。
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 10, 0)) is True
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 14, 0)) is True
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 12, 0)) is False
    assert order_watch.in_trading_session(datetime(2026, 6, 24, 8, 0)) is False


def test_in_trading_session_weekend_is_false():
    # 2026-06-27 是周六。
    assert order_watch.in_trading_session(datetime(2026, 6, 27, 10, 0)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_order_watch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.order_watch'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/trader/order_watch.py
"""本机周期性快照委托表 → diff → 经 WS 主动推 order_event 事件。

设计要点见 docs/superpowers/plans/2026-06-28-order-event-push.md。
纯函数（build_snapshot / diff_snapshots / in_trading_session）与异步薄壳分离，
便于离线单测。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from typing import Any, Optional

logger = logging.getLogger(__name__)

IDLE_INTERVAL_DEFAULT = 300   # 空闲（无未完成委托）轮询周期：5 分钟。验证码顾虑→分钟级
ACTIVE_INTERVAL_DEFAULT = 60  # 有未完成委托挂着时提速：1 分钟（为及时抓成交）
FRAME_TYPE = "order_event"

# THS 真实表头（逐字）
COL_CODE = "证券代码"
COL_OP = "操作"
COL_ORDER_QTY = "委托数量"
COL_ORDER_PRICE = "委托价格"
COL_FILLED_QTY = "成交数量"
COL_AVG_PRICE = "成交均价"
COL_ENTRUST_NO = "合同编号"
COL_NOTE = "备注"

_MORNING = (dtime(9, 30), dtime(11, 30))
_AFTERNOON = (dtime(13, 0), dtime(15, 0))


def in_trading_session(now: datetime) -> bool:
    """A 股交易时段判断（不含节假日；节假日由非交易日无委托变化天然兜住）。"""
    if now.weekday() >= 5:          # 周六/周日
        return False
    t = now.time()
    return (_MORNING[0] <= t <= _MORNING[1]) or (_AFTERNOON[0] <= t <= _AFTERNOON[1])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_order_watch.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/trader/order_watch.py tests/test_order_watch.py
git commit -m "feat(order_watch): 模块骨架 + 交易时段判断纯函数"
```

---

### Task 2: build_snapshot — 委托表解析成 per-order 状态

**Files:**
- Modify: `src/trader/order_watch.py`
- Test: `tests/test_order_watch.py`

**Interfaces:**
- Consumes: 列名常量(Task 1);`backend.orders_active()` 返回结构 `{"code":0,"status":"succeed","data":[行dict,...]}`,行 dict 的 key 即真实表头(见 `src/trader/ths/win.py:244 parse_table`)。
- Produces:
  - `build_snapshot(active_result: dict) -> dict[str, dict]`,返回 `{合同编号: order_state}`,其中 `order_state = {"entrust_no","stock_no","op","order_qty":int,"order_price":str,"filled_qty":int,"avg_price":str,"note":str}`。`code != 0` 或空时返回 `{}`。

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_order_watch.py
def _active(rows):
    return {"code": 0, "status": "succeed", "data": rows}


def test_build_snapshot_parses_real_headers():
    snap = order_watch.build_snapshot(_active([
        {
            "证券代码": "600519", "操作": "买入", "委托数量": "100",
            "委托价格": "1700.000", "成交数量": "0", "成交均价": "",
            "合同编号": "12345", "备注": "已报",
        },
    ]))
    assert set(snap) == {"12345"}
    o = snap["12345"]
    assert o["stock_no"] == "600519"
    assert o["op"] == "买入"
    assert o["order_qty"] == 100
    assert o["order_price"] == "1700.000"
    assert o["filled_qty"] == 0
    assert o["note"] == "已报"


def test_build_snapshot_skips_rows_without_entrust_no():
    snap = order_watch.build_snapshot(_active([{"证券代码": "600519", "合同编号": ""}]))
    assert snap == {}


def test_build_snapshot_empty_on_error_code():
    assert order_watch.build_snapshot({"code": 1, "msg": "读取失败"}) == {}
    assert order_watch.build_snapshot(None) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_order_watch.py::test_build_snapshot_parses_real_headers -v`
Expected: FAIL with `AttributeError: module 'trader.order_watch' has no attribute 'build_snapshot'`

- [ ] **Step 3: Write minimal implementation**

```python
# 追加到 src/trader/order_watch.py
def _to_int(value: Any) -> int:
    try:
        return int(str(value if value is not None else "0").strip().replace(",", "") or "0")
    except (ValueError, TypeError):
        return 0


def build_snapshot(active_result: Optional[dict]) -> dict[str, dict]:
    """把 orders_active 返回解析为 {合同编号: order_state}。code!=0/空 → {}。"""
    snap: dict[str, dict] = {}
    if not active_result or active_result.get("code") != 0:
        return snap
    for row in active_result.get("data", []) or []:
        eno = (row.get(COL_ENTRUST_NO) or "").strip()
        if not eno:
            continue
        snap[eno] = {
            "entrust_no": eno,
            "stock_no": (row.get(COL_CODE) or "").strip(),
            "op": (row.get(COL_OP) or "").strip(),
            "order_qty": _to_int(row.get(COL_ORDER_QTY)),
            "order_price": (row.get(COL_ORDER_PRICE) or "").strip(),
            "filled_qty": _to_int(row.get(COL_FILLED_QTY)),
            "avg_price": (row.get(COL_AVG_PRICE) or "").strip(),
            "note": (row.get(COL_NOTE) or "").strip(),
        }
    return snap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_order_watch.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/trader/order_watch.py tests/test_order_watch.py
git commit -m "feat(order_watch): build_snapshot 解析委托表为 per-order 状态"
```

---

### Task 3: diff_snapshots — 事件状态机 + 帧组装

**Files:**
- Modify: `src/trader/order_watch.py`
- Test: `tests/test_order_watch.py`

**Interfaces:**
- Consumes: `order_state` 结构(Task 2)。
- Produces:
  - `diff_snapshots(prev: dict[str, dict], cur: dict[str, dict], agent_entrust_nos: set[str]) -> list[dict]`,返回事件列表(不含 `seq`/`ts`,由轮询薄壳注入)。事件帧:
    ```
    {
      "type": "order_event",
      "event": "placed" | "partially_filled" | "filled" | "canceled",
      "source": "agent" | "external",
      "entrust_no": str, "stock_no": str, "op": str,
      "order_qty": int, "order_price": str,
      "filled_qty": int, "avg_price": str, "note": str,
    }
    ```
  - 规则:新出现的 `合同编号` → 按当前状态分类(已撤→canceled / 全成→filled / 部成→partially_filled / 否则 placed);已存在且 `备注` 新转 `已撤` → canceled;已存在且 `成交数量↑` → 全成 filled 否则 partially_filled;无变化 → 不发(幂等)。`合同编号 ∈ agent_entrust_nos` → `source="agent"`,否则 `"external"`。消失的委托**不发事件**(由薄壳记 warning,保守避免误报;依赖"今日委托保留全状态行"的前置确认)。

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_order_watch.py
def _order(eno, qty, filled, note, code="600519", op="买入", price="1700.000", avg=""):
    return {
        "entrust_no": eno, "stock_no": code, "op": op,
        "order_qty": qty, "order_price": price,
        "filled_qty": filled, "avg_price": avg, "note": note,
    }


def test_new_order_emits_placed():
    cur = {"1": _order("1", 100, 0, "已报")}
    evs = order_watch.diff_snapshots({}, cur, set())
    assert len(evs) == 1
    e = evs[0]
    assert e["type"] == "order_event"
    assert e["event"] == "placed"
    assert e["source"] == "external"
    assert e["entrust_no"] == "1"
    assert e["order_qty"] == 100
    assert e["filled_qty"] == 0


def test_placed_then_partial_then_full():
    s0 = {"1": _order("1", 100, 0, "已报")}
    s1 = {"1": _order("1", 100, 60, "部成", avg="1699.500")}
    s2 = {"1": _order("1", 100, 100, "已成", avg="1699.800")}

    e1 = order_watch.diff_snapshots(s0, s1, set())
    assert [e["event"] for e in e1] == ["partially_filled"]
    assert e1[0]["filled_qty"] == 60
    assert e1[0]["avg_price"] == "1699.500"

    e2 = order_watch.diff_snapshots(s1, s2, set())
    assert [e["event"] for e in e2] == ["filled"]
    assert e2[0]["filled_qty"] == 100
    assert e2[0]["note"] == "已成"


def test_placed_then_canceled():
    s0 = {"1": _order("1", 100, 0, "已报")}
    s1 = {"1": _order("1", 100, 0, "已撤")}
    evs = order_watch.diff_snapshots(s0, s1, set())
    assert [e["event"] for e in evs] == ["canceled"]


def test_no_change_is_idempotent():
    s0 = {"1": _order("1", 100, 60, "部成")}
    assert order_watch.diff_snapshots(s0, dict(s0), set()) == []


def test_source_tagged_agent_when_entrust_known():
    cur = {"9": _order("9", 100, 0, "已报")}
    evs = order_watch.diff_snapshots({}, cur, {"9"})
    assert evs[0]["source"] == "agent"


def test_disappeared_order_emits_nothing():
    s0 = {"1": _order("1", 100, 0, "已报")}
    assert order_watch.diff_snapshots(s0, {}, set()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_order_watch.py -k diff or source or placed or canceled or idempotent or disappeared -v`
Expected: FAIL with `AttributeError: module 'trader.order_watch' has no attribute 'diff_snapshots'`

- [ ] **Step 3: Write minimal implementation**

```python
# 追加到 src/trader/order_watch.py
def _is_full(o: dict) -> bool:
    return o["order_qty"] > 0 and o["filled_qty"] >= o["order_qty"]


def _classify_new(o: dict) -> str:
    if "已撤" in o["note"]:
        return "canceled"
    if _is_full(o):
        return "filled"
    if o["filled_qty"] > 0:
        return "partially_filled"
    return "placed"


def _make_event(event_name: str, o: dict, agent_entrust_nos: set[str]) -> dict:
    return {
        "type": FRAME_TYPE,
        "event": event_name,
        "source": "agent" if o["entrust_no"] in agent_entrust_nos else "external",
        "entrust_no": o["entrust_no"],
        "stock_no": o["stock_no"],
        "op": o["op"],
        "order_qty": o["order_qty"],
        "order_price": o["order_price"],
        "filled_qty": o["filled_qty"],
        "avg_price": o["avg_price"],
        "note": o["note"],
    }


def diff_snapshots(prev: dict[str, dict], cur: dict[str, dict],
                   agent_entrust_nos: set[str]) -> list[dict]:
    """对比两轮快照，返回 order_event 列表（不含 seq/ts）。"""
    events: list[dict] = []
    for eno, o in cur.items():
        before = prev.get(eno)
        if before is None:
            events.append(_make_event(_classify_new(o), o, agent_entrust_nos))
            continue
        if "已撤" in o["note"] and "已撤" not in before["note"]:
            events.append(_make_event("canceled", o, agent_entrust_nos))
            continue
        if o["filled_qty"] > before["filled_qty"]:
            name = "filled" if _is_full(o) else "partially_filled"
            events.append(_make_event(name, o, agent_entrust_nos))
    return events
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_order_watch.py -v`
Expected: PASS (all order_watch tests green)

- [ ] **Step 5: Commit**

```bash
git add src/trader/order_watch.py tests/test_order_watch.py
git commit -m "feat(order_watch): diff 状态机 + order_event 帧组装"
```

---

### Task 4: THS 单窗口串行化锁 + agent 下单登记

**Files:**
- Modify: `src/trader/ths/win.py`(WinThsBackend.`__init__`)
- Modify: `src/trader/dispatcher.py`(`handle_call`)
- Test: `tests/test_dispatcher_lock.py`

**Interfaces:**
- Produces:
  - `WinThsBackend.win_lock: asyncio.Lock`(轮询与 RPC 共用,串行化窗口访问)
  - `WinThsBackend.agent_entrust_nos: set[str]`(agent 经 RPC 下单成功后登记的合同编号,供 order_watch 标记 source)
  - `handle_call` 行为不变,但:碰窗口的方法(`trading_methods`)在 `backend.win_lock` 下串行;`buy`/`sell` 成功(结果含 `entrust_no`)后把 `entrust_no` 加入 `backend.agent_entrust_nos`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dispatcher_lock.py
"""dispatcher 锁 + agent 下单登记回归。沿用 asyncio.run 同步驱动约定。"""
import asyncio

from trader import dispatcher


class LockFakeBackend:
    def __init__(self):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self.concurrent = 0
        self.max_concurrent = 0

    async def _hold(self, result):
        # 记录临界区并发度，验证锁真的串行化。
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(0.02)
        self.concurrent -= 1
        return result

    async def orders_active(self):
        return await self._hold({"code": 0, "status": "succeed", "data": []})

    async def buy(self, stock_no, amount, price, client_order_id):
        return await self._hold({"code": 0, "entrust_no": "777"})

    async def sell(self, stock_no, amount, price, client_order_id):
        return await self._hold({"code": 0, "entrust_no": "888"})


def test_window_methods_serialized_by_lock():
    backend = LockFakeBackend()

    async def drive():
        frame = {"type": "call", "id": "x", "method": "orders_active", "params": {}}
        await asyncio.gather(*[dispatcher.handle_call(dict(frame), backend) for _ in range(5)])

    asyncio.run(drive())
    assert backend.max_concurrent == 1  # 串行化：临界区任意时刻至多 1 个


def test_buy_registers_entrust_no():
    backend = LockFakeBackend()
    frame = {"type": "call", "id": "b", "method": "buy",
             "params": {"stock_no": "600519", "amount": 100, "price": 1700.0}}
    reply = asyncio.run(dispatcher.handle_call(frame, backend))
    assert reply["ok"] is True
    assert "777" in backend.agent_entrust_nos
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dispatcher_lock.py -v`
Expected: FAIL — `test_window_methods_serialized_by_lock` 报 `assert backend.max_concurrent == 1`(当前无锁,并发度 >1);`test_buy_registers_entrust_no` 报 `assert "777" in set()`(当前不登记)。

- [ ] **Step 3a: Write minimal implementation — backend 字段**

在 `src/trader/ths/win.py` 的 `WinThsBackend.__init__` 末尾追加(`asyncio` 已在该文件导入):

```python
        # order_watch 与 RPC 共用：串行化对 THS 单窗口的访问，避免并发 copy_table。
        self.win_lock = asyncio.Lock()
        # agent 经 RPC 下单成功后登记的合同编号，供 order_watch 标记事件来源。
        self.agent_entrust_nos: set[str] = set()
```

- [ ] **Step 3b: Write minimal implementation — dispatcher 加锁 + 登记**

在 `src/trader/dispatcher.py` 的 `handle_call` 中,`trading_methods` 集合定义之后、`try:` 之前,插入锁获取;并把 `try` 改为 `try/finally` 释放锁。即把原有:

```python
    if method in trading_methods and not cfg.enable_ths_plugin:
        reply["ok"] = False
        reply["error"] = "同花顺实盘交易插件已被禁用，请在客户端界面中开启该插件模块！"
        return reply

    try:
```

改为:

```python
    if method in trading_methods and not cfg.enable_ths_plugin:
        reply["ok"] = False
        reply["error"] = "同花顺实盘交易插件已被禁用，请在客户端界面中开启该插件模块！"
        return reply

    # 串行化 THS 单窗口访问：order_watch 轮询与下单/查询共用 backend.win_lock。
    needs_window = method in trading_methods
    if needs_window:
        await backend.win_lock.acquire()
    try:
```

并在 `handle_call` 现有 `except`/收尾之后补 `finally`(与该 `try` 对齐),释放锁:

```python
    finally:
        if needs_window:
            backend.win_lock.release()
```

在 `buy`/`sell` 分支拿到 `result` 后,登记 entrust_no。把:

```python
            result = await backend.buy(stock_no, amount, price, client_order_id)
```

改为:

```python
            result = await backend.buy(stock_no, amount, price, client_order_id)
            _eno = (result or {}).get("entrust_no")
            if _eno:
                backend.agent_entrust_nos.add(str(_eno))
```

`sell` 分支同样处理(把 `result = await backend.sell(...)` 后追加同样三行,变量名复用 `_eno`)。

> 注意:`finally` 必须覆盖现有 `try` 的全部 `except` 分支之后。实现时先 `cx definition --name handle_call --from src/trader/dispatcher.py` 看清 `try` 的完整结构(含末尾 `except Exception` 和 return),把 `finally` 接在最后一个 `except` 块之后、与 `try` 同缩进。RPA 分支(`xueqiu_publish_review`)不在 `trading_methods` 内 → `needs_window=False` → 不占 THS 锁。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dispatcher_lock.py tests/test_dispatcher_envelope.py -v`
Expected: PASS(新锁测试 + 既有 envelope 回归全绿)

- [ ] **Step 5: Commit**

```bash
git add src/trader/ths/win.py src/trader/dispatcher.py tests/test_dispatcher_lock.py
git commit -m "feat(dispatcher): THS 单窗口串行化锁 + agent 下单 entrust_no 登记"
```

---

### Task 5: 轮询薄壳 _poll_once + next_interval(自适应) + order_watch_task

**Files:**
- Modify: `src/trader/order_watch.py`
- Modify: `src/trader/config.py`(新增两个节奏配置项)
- Test: `tests/test_order_watch.py`

**Interfaces:**
- Consumes: `build_snapshot`/`diff_snapshots`(Task 2/3);`backend.win_lock`/`backend.agent_entrust_nos`/`backend.orders_active()`(Task 4);`client.send_frame(frame: dict)`(`src/trader/ws_client.py:346`,已存在);`client.backend`(`ws_client.py:164`,已存在);`state.snapshot()` 含 `connection_state`/`enable_ths_plugin`(`src/trader/main_window.py` SharedState);`config.load()`(`src/trader/config.py:59`)。
- Produces:
  - `TraderConfig.order_watch_idle_secs: int`(默认 300)、`TraderConfig.order_watch_active_secs: int`(默认 60)。
  - `next_interval(snapshot: dict, idle_secs: int, active_secs: int) -> int`:快照里存在"未完成委托"(`备注` 不含 `已成`/`已撤` 且未全成)→ 返回 `active_secs`,否则 `idle_secs`。纯函数。
  - `async def _poll_once(backend, client, prev, seq) -> tuple[dict, int, bool]`:加锁取 `orders_active` → 解析 → 若 `prev is None` 建基线返回 `(cur, seq, True)` 不发事件 → 否则 diff 并对每个事件注入 `seq`(自增)+ `ts`(`time.time()`)后 `await client.send_frame(ev)`,返回 `(cur, seq, True)`;读失败(code!=0)返回 `(prev, seq, False)`。
  - `async def order_watch_task(state, client) -> None`:**自适应**循环,gate(`connection_state=="CONNECTED"` 且 `enable_ths_plugin` 且 `in_trading_session`),调用 `_poll_once`,按 `next_interval` 决定下次 sleep(空闲 idle、有挂单 active);gate 不通过/读失败/异常一律退回 idle;`CancelledError` 退出,其余异常记 warning 续跑。

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_order_watch.py
import asyncio


class WatchFakeBackend:
    def __init__(self, scripted):
        self.win_lock = asyncio.Lock()
        self.agent_entrust_nos: set[str] = set()
        self._scripted = list(scripted)   # 每次 orders_active 返回下一项
        self._i = 0

    async def orders_active(self):
        item = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return item


class WatchFakeClient:
    def __init__(self, backend):
        self.backend = backend
        self.sent: list[dict] = []

    async def send_frame(self, frame):
        self.sent.append(frame)


def test_first_round_builds_baseline_no_emit():
    backend = WatchFakeBackend([_active([
        {"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
         "成交数量": "0", "成交均价": "", "合同编号": "1", "备注": "已报"},
    ])])
    client = WatchFakeClient(backend)

    async def drive():
        prev, seq, ok = await order_watch._poll_once(backend, client, None, 0)
        return prev, seq, ok

    prev, seq, ok = asyncio.run(drive())
    assert ok is True
    assert set(prev) == {"1"}
    assert client.sent == []          # 重启只建基线，不补发历史


def test_second_round_emits_fill_with_seq_and_ts():
    r0 = _active([{"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
                   "成交数量": "0", "成交均价": "", "合同编号": "1", "备注": "已报"}])
    r1 = _active([{"证券代码": "600519", "操作": "买入", "委托数量": "100", "委托价格": "1700.000",
                   "成交数量": "100", "成交均价": "1699.800", "合同编号": "1", "备注": "已成"}])
    backend = WatchFakeBackend([r0, r1])
    client = WatchFakeClient(backend)

    async def drive():
        prev, seq, _ = await order_watch._poll_once(backend, client, None, 0)
        prev, seq, _ = await order_watch._poll_once(backend, client, prev, seq)
        return seq

    seq = asyncio.run(drive())
    assert len(client.sent) == 1
    ev = client.sent[0]
    assert ev["event"] == "filled"
    assert ev["seq"] == 1
    assert isinstance(ev["ts"], float)
    assert seq == 1


def test_read_failure_skips_round():
    backend = WatchFakeBackend([{"code": 1, "msg": "验证码弹窗"}])
    client = WatchFakeClient(backend)

    async def drive():
        return await order_watch._poll_once(backend, client, None, 0)

    prev, seq, ok = asyncio.run(drive())
    assert ok is False
    assert prev is None              # 读失败不污染基线
    assert client.sent == []


def test_next_interval_active_when_open_order():
    snap = {"1": _order("1", 100, 0, "已报")}            # 未完成 → 提速
    assert order_watch.next_interval(snap, 300, 60) == 60


def test_next_interval_active_when_partial():
    snap = {"1": _order("1", 100, 60, "部成")}           # 部成仍未完成 → 提速
    assert order_watch.next_interval(snap, 300, 60) == 60


def test_next_interval_idle_when_all_done():
    snap = {
        "1": _order("1", 100, 100, "已成"),
        "2": _order("2", 100, 0, "已撤"),
    }
    assert order_watch.next_interval(snap, 300, 60) == 300


def test_next_interval_idle_when_empty():
    assert order_watch.next_interval({}, 300, 60) == 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_order_watch.py -k "poll or baseline or second_round or read_failure or next_interval" -v`
Expected: FAIL with `AttributeError: module 'trader.order_watch' has no attribute '_poll_once'`(及 `next_interval` 缺失)

- [ ] **Step 3a: Write minimal implementation — config 新增节奏字段**

在 `src/trader/config.py` 的 `TraderConfig` dataclass 末尾(`chrome_cdp_port` 之后)追加:

```python
    order_watch_idle_secs: int = 300   # order_watch 空闲轮询周期（秒）：默认 5 分钟
    order_watch_active_secs: int = 60  # 有未完成委托时的提速周期（秒）：默认 1 分钟
```

在 `load()` 的 `TraderConfig(...)` 构造里(`chrome_cdp_port=...` 之后)追加:

```python
            order_watch_idle_secs=data.get("order_watch_idle_secs", 300),
            order_watch_active_secs=data.get("order_watch_active_secs", 60),
```

在 `save()` 的 `data = {...}` 里(`"chrome_cdp_port": ...` 之后)追加:

```python
        "order_watch_idle_secs": config.order_watch_idle_secs,
        "order_watch_active_secs": config.order_watch_active_secs,
```

- [ ] **Step 3b: Write minimal implementation — order_watch 自适应循环**

在 `src/trader/order_watch.py` 顶部 import 区追加(与既有 `from datetime import ...` 同组):

```python
from . import config
```

追加纯函数与异步薄壳:

```python
def _is_open(o: dict) -> bool:
    """该委托是否仍未完成（可能继续成交）。"""
    if "已撤" in o["note"] or "已成" in o["note"]:
        return False
    if o["order_qty"] > 0 and o["filled_qty"] >= o["order_qty"]:
        return False
    return True


def next_interval(snapshot: dict, idle_secs: int, active_secs: int) -> int:
    """有未完成委托挂着 → active（提速）；否则 idle（降频）。"""
    return active_secs if any(_is_open(o) for o in snapshot.values()) else idle_secs


async def _poll_once(backend, client, prev: Optional[dict], seq: int) -> tuple[Optional[dict], int, bool]:
    """单轮：取委托快照 → diff → 发帧。返回 (new_prev, new_seq, ok)。"""
    async with backend.win_lock:
        active = await backend.orders_active()
    if not active or active.get("code") != 0:
        return prev, seq, False                      # 未绑定/验证码/读失败 → 跳过本轮
    cur = build_snapshot(active)
    if prev is None:
        logger.info("order_watch 基线建立：%d 笔委托", len(cur))
        return cur, seq, True                         # 重启只建基线，不补发历史
    events = diff_snapshots(prev, cur, backend.agent_entrust_nos)
    for eno in prev:
        if eno not in cur:
            logger.warning("order_watch 委托 %s 已从委托表消失，保守起见未发事件", eno)
    for ev in events:
        seq += 1
        ev["seq"] = seq
        ev["ts"] = time.time()
        await client.send_frame(ev)
        logger.info("order_watch 推送 %s entrust=%s source=%s filled=%s",
                    ev["event"], ev["entrust_no"], ev["source"], ev["filled_qty"])
    return cur, seq, True


async def order_watch_task(state, client) -> None:
    """自适应盯委托表 → 事件驱动推送。与 _ths_polling_task 同样 exception-safe。

    验证码顾虑 → 分钟级:空闲 idle（默认 5min）,有未完成委托时提速 active（默认 1min）。
    """
    backend = client.backend
    cfg = config.load()
    idle_secs = cfg.order_watch_idle_secs or IDLE_INTERVAL_DEFAULT
    active_secs = cfg.order_watch_active_secs or ACTIVE_INTERVAL_DEFAULT
    prev: Optional[dict] = None
    seq = 0
    interval = idle_secs
    logger.info("order_watch_task 启动（空闲 %ds / 活跃 %ds）", idle_secs, active_secs)
    while True:
        try:
            await asyncio.sleep(interval)
            snap = state.snapshot()
            if snap.get("connection_state") != "CONNECTED":
                interval = idle_secs
                continue
            if not snap.get("enable_ths_plugin", True):
                interval = idle_secs
                continue
            if not in_trading_session(datetime.now()):
                interval = idle_secs
                continue
            prev, seq, ok = await _poll_once(backend, client, prev, seq)
            interval = next_interval(prev, idle_secs, active_secs) if (ok and prev) else idle_secs
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("order_watch_task 异常：%s", e)
            interval = idle_secs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_order_watch.py -v`
Expected: PASS(全部 order_watch 测试绿)

- [ ] **Step 5: Commit**

```bash
git add src/trader/order_watch.py src/trader/config.py tests/test_order_watch.py
git commit -m "feat(order_watch): _poll_once + 自适应 next_interval（分钟级，可配置）"
```

---

### Task 6: 接入 _async_main(拉起 + finally cancel)

**Files:**
- Modify: `src/trader/main.py`(import 区 + `_async_main` 的 create_task / finally)
- Test: `tests/test_order_watch_wiring.py`

**Interfaces:**
- Consumes: `order_watch.order_watch_task`(Task 5)。
- Produces: `_async_main` 中与 `_ths_polling_task`/`_pairing_refresh_watcher` 同样 `create_task` 拉起 `order_watch.order_watch_task(state, client)`,并在 `finally` 中 `cancel` + 纳入 `gather`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_order_watch_wiring.py
"""接线回归：main 引用 order_watch 且任务符号存在。"""
import inspect

import trader.main as main
from trader import order_watch


def test_main_imports_order_watch_and_wires_task():
    assert hasattr(order_watch, "order_watch_task")
    src = inspect.getsource(main._async_main)
    assert "order_watch.order_watch_task" in src      # 已拉起
    assert src.count("order_watch") >= 2              # create_task + finally cancel
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_order_watch_wiring.py -v`
Expected: FAIL — `assert "order_watch.order_watch_task" in src`(尚未接线)。
(若 `import trader.main` 在非 Windows 上因 win32 依赖报错,本任务实现时确认 main 顶层 import 是否惰性;现有测试套件已能 import dispatcher,main 顶层若硬依赖 win32 则把该断言改为 `inspect.getsource(... )` 前先 `importlib`,或参考既有测试对 main 的处理方式。)

- [ ] **Step 3a: Write minimal implementation — import**

在 `src/trader/main.py` 顶部已有的 `from . import ...` 分组里加入 `order_watch`(与 `ws_client`/`bootstrap`/`tray` 同组):

```python
from . import order_watch
```

- [ ] **Step 3b: Write minimal implementation — 拉起 + 收尾**

在 `_async_main` 中,把:

```python
    polling_task = asyncio.create_task(_ths_polling_task(state))
    refresh_task = asyncio.create_task(_pairing_refresh_watcher(state, client))
    try:
        await client.run()
```

改为:

```python
    polling_task = asyncio.create_task(_ths_polling_task(state))
    refresh_task = asyncio.create_task(_pairing_refresh_watcher(state, client))
    order_event_task = asyncio.create_task(order_watch.order_watch_task(state, client))
    try:
        await client.run()
```

并把 `finally` 块:

```python
    finally:
        polling_task.cancel()
        refresh_task.cancel()
        try:
            await asyncio.gather(polling_task, refresh_task, return_exceptions=True)
        except Exception:
            pass
```

改为:

```python
    finally:
        polling_task.cancel()
        refresh_task.cancel()
        order_event_task.cancel()
        try:
            await asyncio.gather(polling_task, refresh_task, order_event_task, return_exceptions=True)
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_order_watch_wiring.py -v && python -m pytest -q`
Expected: 接线测试 PASS;全量套件 PASS(无回归)。

- [ ] **Step 5: Commit**

```bash
git add src/trader/main.py tests/test_order_watch_wiring.py
git commit -m "feat(main): _async_main 拉起 order_watch_task + finally 取消"
```

---

### Task 7: 交易日 live smoke 验证(手动)

**Files:**
- Modify: `docs/superpowers/plans/2026-06-28-order-event-push.md`(在本节勾选并记录实测结果)

**Interfaces:** 无代码产出;验证 Global Constraints 的两条前置确认 + 端到端行为。

- [ ] **Step 1: 前置确认 ①(委托表保留全状态行)**

交易日盘中,Windows 端 xiadan 登录后,小额限价单挂单 → 全成后,手动 F1/F8 看"今日委托"是否仍含该行且 `备注=已成`、`成交数量==委托数量`。同理撤一单看是否 `备注=已撤` 仍在表内。
Expected: 已成/已撤行**保留**在委托表(状态机成立)。
若不成立(行消失):停止,回 Task 3 增加"prev 中存在、cur 中消失 → 查 orders_filled 确认全成则发 filled,否则 canceled"的兜底分支,补单测后再继续。

- [ ] **Step 2: 前置确认 ②(帧契约对齐 yu-agent)**

与上游确认 `order_event` 的 `type` 与字段集(本方案:`type/event/source/entrust_no/stock_no/op/order_qty/order_price/filled_qty/avg_price/note/seq/ts`)。
Expected: 上游按 `entrust_no` 去重(agent 自己下的单上游已知,`source` 为 best-effort 辅助)。

- [ ] **Step 3: 手机端下单 → Windows 探活推送**

Windows trader 正常运行并 `CONNECTED`、盘中。用**手机 App** 对同一账户挂一笔小额限价单。
Expected: ≤ ~`POLL_INTERVAL+读取耗时`(约 15s 内)上游收到恰好一条 `event="placed"`、`source="external"` 的帧,字段与手机/THS 界面一致。成交后再收到一条 `partially_filled`/`filled`。

- [ ] **Step 4: agent 经 RPC 下单 → source 标记**

经上游 agent 正常下一笔单。
Expected: order_watch 推出的对应事件 `source="agent"`(因 dispatcher 已登记其 entrust_no);不与 RPC 抢窗口报错(锁生效)。

- [ ] **Step 5: 重启不补发**

盘中重启 trader。
Expected: 重启后第一轮只建基线、无历史事件补发;此后新变化照常推送。

- [ ] **Step 6: 记录结果并提交**

把每步实测结论(含一帧真实 `order_event` 样例)回填本节。

```bash
git add docs/superpowers/plans/2026-06-28-order-event-push.md
git commit -m "docs(order_watch): live smoke 验证结果回填"
```

---

## Self-Review

**1. Spec coverage**
- 成交事件推送 → Task 2/3/5(diff `成交数量↑` → partial/filled)✓
- 手动(手机端)下单/挂单事件 → Task 3(新 `合同编号` → placed)+ Task 7 Step3 验证;机制依据"Windows xiadan F5 重查券商服务器订单簿,含手机下单"✓
- 与 `_ths_polling_task` 同模式 create_task/finally cancel → Task 6 ✓
- ~3s→自适应分钟级降频(空闲 300s/活跃 60s,可配置)、connection_state/bound/交易时段 gate → Task 5(gate + next_interval)+ Global Constraints ✓
- 验证码顾虑(轮询触发 OCR)→ 分钟级 + 每轮只读一张表 + 读失败跳过本轮 → Global Constraints + Task 5 `_poll_once` + `test_read_failure_skips_round` ✓
- 合同编号 diff、内存快照、重启只建基线、进程内 seq → Task 5 ✓
- 范围外(不做决策/不碰 gateway/yu-agent/不加风控门)→ 全程未触碰 ✓
- 验收1 离线单测(两轮快照、字段值、幂等、真实表头)→ Task 2/3/5 ✓
- 验收2 重启只建基线 → Task 5 `test_first_round_builds_baseline_no_emit` + Task 7 Step5 ✓
- 验收3 交易日 live smoke → Task 7 ✓
- 新增风险点 R1 单窗口锁 → Task 4 ✓

**2. Placeholder scan:** 无 TBD/TODO;每个代码步骤含完整可运行代码与命令。Task 7 为人工验证,步骤为可执行的操作清单(非代码占位)。

**3. Type consistency:** `order_state` 字段(entrust_no/stock_no/op/order_qty/order_price/filled_qty/avg_price/note)在 build_snapshot(产出)、diff_snapshots/_make_event(消费)、测试 `_order` helper 间一致;`_poll_once` 返回 `(prev, seq, ok)` 在实现与三个测试间一致;`backend.win_lock`/`backend.agent_entrust_nos` 在 Task 4 定义、Task 5 消费、各 Fake backend 同名。
