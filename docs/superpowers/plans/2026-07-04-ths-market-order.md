# 同花顺市价委托路径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `buy`/`sell` 不传 `price` 时走同花顺**真·市价委托面板 + 五档即成剩撤**（立即成交、剩余自动撤销、无残留挂单），回执带回真实成交量/均价；传 `price` 时保持原限价挂单逻辑不变。

**Architecture:** 在 `_do_buy`/`_do_sell` 内按 `price` 有无分派：`None`→新方法 `_submit_market_trade`（市价委托面板），有值→原 `_submit_trade`（F1/F2 限价）。市价回执改查 `orders_filled`（成交表）而非 `orders_active`，用纯函数 `_match_market_fill` 做前后差分匹配。面板导航/控件 ID/委托策略下拉选法只能在 Windows 真机 dump 后写死。

**Tech Stack:** Python 3 · pywin32（win32gui/win32api/ctypes 跨进程消息）· pytest · 同花顺 xiadan（32 位 MFC，Windows 真机）

## Global Constraints

- 复用 `buy`/`sell` 两个工具，不新增工具；`price is None` = 市价，`price` 有值 = 限价（方案 A）。
- 市价策略固定为**五档即成剩撤**（沪深北全市场通用、剩余自动撤、无残留）。
- 市价回执数据源是 `get_filled_orders()`（成交表），**不是** `get_active_orders()`。
- 限价路径（`price` 有值）逻辑**一行都不改**，仍走 `_submit_trade` + `_lookup_entrust_no` + 由 agent 用 `orders_active`/`cancel` 管理。
- 纯逻辑（Task 1/2/3）在 macOS 可跑测试；面板/控件（Task 4/5/6/7）**只能 Windows + xiadan 真机**联调，测试为手动实单核对。
- 表格列名以 `parse_table` 实际解析结果为准；本计划用 `src/trader/ths/const.py` 集中的列名常量引用，真机若不符只改常量。
- 提交信息以 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` 结尾。
- 分支：`feat/ths-market-order`（已建，spec 已在其上）。

---

### Task 1: `_do_buy`/`_do_sell` 按 price 分派到市价/限价路径

**Files:**
- Modify: `src/trader/ths/win.py`（`_do_buy` L1140、`_do_sell` L1137）
- Modify: `src/trader/ths/win.py`（新增 `_submit_market_trade` **桩**方法，占位，Task 6 填实现）
- Test: `tests/test_market_price.py`（补分派用例）

**Interfaces:**
- Consumes: 现有 `_submit_trade(panel_key, op_keyword, stock_no, amount, price)`。
- Produces:
  - `_do_buy(stock_no, amount, price)` / `_do_sell(stock_no, amount, price)`：`price is None` → `self._submit_market_trade(op_keyword, stock_no, amount)`；否则 → `self._submit_trade(...)`。
  - `_submit_market_trade(self, op_keyword, stock_no, amount) -> dict`（本任务仅桩，返回 `{"code": 9, "status": "not_implemented"}`）。`op_keyword` ∈ {"买入","卖出"}。

- [ ] **Step 1: Write the failing test**

在 `tests/test_market_price.py` 末尾追加。复用文件里已有的 `_drive`（打桩 `_ensure_bound` + `asyncio.to_thread`，捕获透传到 `to_thread` 的 `fn`/`args`）。但分派发生在 `_do_buy`/`_do_sell` **内部**，`_drive` 只捕获到 `fn=_do_buy`——所以这里直接调同步的 `_do_buy`/`_do_sell` 并打桩两个 submit 方法：

```python
def _stub_submits(backend):
    """打桩两条提交路径，返回记录调用的字典。"""
    calls = {}
    backend._submit_trade = lambda *a: calls.setdefault("limit", a) or {"code": 0}
    backend._submit_market_trade = lambda *a: calls.setdefault("market", a) or {"code": 0}
    return calls


def test_do_buy_none_price_routes_to_market():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_buy("600000", 100, None)
    assert "limit" not in calls
    assert calls["market"] == ("买入", "600000", 100)


def test_do_sell_none_price_routes_to_market():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_sell("300459", 200, None)
    assert calls["market"] == ("卖出", "300459", 200)


def test_do_buy_with_price_routes_to_limit():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_buy("600000", 100, 12.34)
    assert "market" not in calls
    assert calls["limit"] == ("F1", "买入", "600000", 100, 12.34)


def test_do_sell_with_price_routes_to_limit():
    backend = WinThsBackend()
    calls = _stub_submits(backend)
    backend._do_sell("300459", 200, 4.11)
    assert calls["limit"] == ("F2", "卖出", "300459", 200, 4.11)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_market_price.py -k "routes" -v`
Expected: FAIL —`AttributeError: 'WinThsBackend' object has no attribute '_submit_market_trade'`，或 `market` 分支未命中（现 `_do_buy` 无条件走 `_submit_trade`）。

- [ ] **Step 3: Write minimal implementation**

改 `_do_buy`/`_do_sell`（win.py L1137-1141 附近）并新增桩方法：

```python
    def _do_sell(self, stock_no, amount, price):
        if price is None:
            return self._submit_market_trade("卖出", stock_no, amount)
        return self._submit_trade("F2", "卖出", stock_no, amount, price)

    def _do_buy(self, stock_no, amount, price):
        if price is None:
            return self._submit_market_trade("买入", stock_no, amount)
        return self._submit_trade("F1", "买入", stock_no, amount, price)

    def _submit_market_trade(self, op_keyword, stock_no, amount):
        # 市价委托（五档即成剩撤）路径，真机实现见 Task 6。
        return {"code": 9, "status": "not_implemented",
                "msg": "market path not wired yet"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_market_price.py -v`
Expected: PASS（含既有 4 个透传用例 + 新 4 个分派用例）。既有 `test_*_market_passes_none_price` 仍绿——它们断言 `buy`/`sell` → `to_thread(_do_buy, ..., None)` 的透传，未触及内部分派。

- [ ] **Step 5: Commit**

```bash
git add src/trader/ths/win.py tests/test_market_price.py
git commit -m "feat(ths): _do_buy/_do_sell 按 price 分派市价/限价路径(桩)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `_match_market_fill` 成交回执纯函数

**Files:**
- Modify: `src/trader/ths/const.py`（新增成交表列名常量）
- Modify: `src/trader/ths/win.py`（新增模块级/静态 `_match_market_fill`）
- Test: `tests/test_market_fill.py`（新建）

**Interfaces:**
- Consumes: `get_filled_orders()` 返回的 `data`（`list[dict]`，中文列名）；列名常量来自 `const`。
- Produces: `_match_market_fill(before, after, stock_no, op_keyword, requested_amount) -> dict`：
  - 匹配 `after` 中相对 `before` **新增**、且 `证券代码==stock_no` 且 `op_keyword in 操作` 的成交行；
  - 汇总成交数量、按金额/数量算成交均价；
  - 返回 `{"code":0, "status":"filled"|"partially_filled", "stock_no", "op":op_keyword, "requested_amount", "filled_amount", "avg_price"}`；无新增匹配 → `{"code":2, "status":"unknown", "stock_no", "op":op_keyword, "requested_amount", "filled_amount":0}`。

- [ ] **Step 1: Write the failing test**

新建 `tests/test_market_fill.py`：

```python
"""市价单成交回执匹配（_match_market_fill）纯函数测试。

五档即成剩撤下完不留 orders_active，且可能部分成交 → 回执必须查成交表(orders_filled)
拿真实成交量/均价。这里只测前后差分 + 汇总逻辑，不触碰 Win32。
"""
from trader.ths.win import _match_market_fill


def _row(code, op, qty, price, amt, sn):
    return {"证券代码": code, "操作": op, "成交数量": qty,
            "成交均价": price, "成交金额": amt, "成交编号": sn}


def test_full_fill_single_row():
    before = []
    after = [_row("600000", "证券买入", "100", "12.340", "1234.00", "A1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "filled"
    assert r["filled_amount"] == 100
    assert r["avg_price"] == 12.34
    assert r["op"] == "买入"


def test_partial_fill_multi_row_weighted_avg():
    # 请求 300，两笔成交共 200 → 部分成交；均价按金额/数量加权。
    before = [_row("600000", "证券买入", "999", "9.999", "9989.00", "OLD")]
    after = [
        _row("600000", "证券买入", "999", "9.999", "9989.00", "OLD"),
        _row("600000", "证券买入", "100", "12.000", "1200.00", "A1"),
        _row("600000", "证券买入", "100", "12.500", "1250.00", "A2"),
    ]
    r = _match_market_fill(before, after, "600000", "买入", 300)
    assert r["status"] == "partially_filled"
    assert r["filled_amount"] == 200
    assert r["avg_price"] == 12.25  # (1200+1250)/200


def test_no_match_returns_unknown():
    before = []
    after = [_row("000001", "证券买入", "100", "10.000", "1000.00", "X1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "unknown"
    assert r["filled_amount"] == 0


def test_ignores_opposite_op_same_code():
    before = []
    after = [_row("600000", "证券卖出", "100", "12.000", "1200.00", "S1")]
    r = _match_market_fill(before, after, "600000", "买入", 100)
    assert r["status"] == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_market_fill.py -v`
Expected: FAIL — `ImportError: cannot import name '_match_market_fill'`。

- [ ] **Step 3: Write minimal implementation**

在 `const.py` 追加（真机核对 `parse_table` 表头后如不符只改此处）：

```python
# 成交表(orders_filled)列名——真机以 parse_table 解析结果为准
FILLED_COL_CODE = "证券代码"
FILLED_COL_OP = "操作"
FILLED_COL_QTY = "成交数量"
FILLED_COL_PRICE = "成交均价"
FILLED_COL_AMOUNT = "成交金额"
FILLED_COL_DEAL_NO = "成交编号"
```

在 `win.py` 顶部 `from .const import ...` 处补入这些常量，并加模块级函数（放在 class 外、`logger` 定义之后）：

```python
def _match_market_fill(before, after, stock_no, op_keyword, requested_amount):
    """前后成交表差分 → 市价单成交回执。before/after 为 get_filled_orders 的 data。"""
    def _key(r):
        return r.get(FILLED_COL_DEAL_NO, "").strip() or (
            r.get(FILLED_COL_CODE, ""), r.get(FILLED_COL_QTY, ""),
            r.get(FILLED_COL_PRICE, ""), r.get(FILLED_COL_AMOUNT, ""))

    seen = {_key(r) for r in before}
    filled_qty = 0
    filled_amt = 0.0
    for r in after:
        if _key(r) in seen:
            continue
        if r.get(FILLED_COL_CODE, "").strip() != str(stock_no):
            continue
        if op_keyword not in r.get(FILLED_COL_OP, ""):
            continue
        try:
            qty = int(float(r.get(FILLED_COL_QTY, "0") or 0))
            amt = float(r.get(FILLED_COL_AMOUNT, "0") or 0)
        except ValueError:
            continue
        filled_qty += qty
        filled_amt += amt

    if filled_qty <= 0:
        return {"code": 2, "status": "unknown", "stock_no": str(stock_no),
                "op": op_keyword, "requested_amount": int(requested_amount),
                "filled_amount": 0}

    avg = round(filled_amt / filled_qty, 3)
    status = "filled" if filled_qty >= int(requested_amount) else "partially_filled"
    return {"code": 0, "status": status, "stock_no": str(stock_no),
            "op": op_keyword, "requested_amount": int(requested_amount),
            "filled_amount": filled_qty, "avg_price": avg}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_market_fill.py -v`
Expected: PASS（4 用例全绿；`avg_price` 12.34 与 12.25 精确）。

- [ ] **Step 5: Commit**

```bash
git add src/trader/ths/const.py src/trader/ths/win.py tests/test_market_fill.py
git commit -m "feat(ths): _match_market_fill 成交回执匹配纯函数 + 成交表列名常量

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 工具描述改写（`buy`/`sell` 两条路径语义）

**Files:**
- Modify: `src/trader/dispatcher.py`（`FALLBACK_TOOLS_SCHEMA` 内嵌副本，`buy` L77-91、`sell` L104-118）
- Modify: `docs/tools_schema.json`（`buy` L72-88、`sell` L102-117）
- Test: `tests/test_tools_schema_sync.py`（新建：保证两处描述一致 + 不含旧文案）

> 编辑目标是 `dispatcher.FALLBACK_TOOLS_SCHEMA`（PyInstaller 打包兜底副本）与磁盘 `docs/tools_schema.json`，**两处必须逐字同步**。`load_tools_schema()` 依赖 `config`（`enable_ths_plugin` 决定是否过滤 buy/sell），测试不走它，直接打这两个源。

**Interfaces:**
- Consumes: 无。
- Produces: `buy`/`sell` 的 `description` 与 `price.description` 反映"不传=五档即成剩撤市价、无残留、回执带成交量均价；传=限价挂单需自行撤单管理"。

- [ ] **Step 1: Write the failing test**

新建 `tests/test_tools_schema_sync.py`：

```python
"""工具描述：市价/限价两条路径语义 + FALLBACK_TOOLS_SCHEMA 与 tools_schema.json 同步。"""
import json
from pathlib import Path

from trader.dispatcher import FALLBACK_TOOLS_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def _tool(tools, name):
    return next(t for t in tools if t["name"] == name)


def test_buy_sell_desc_mentions_market_and_limit():
    for name in ("buy", "sell"):
        t = _tool(FALLBACK_TOOLS_SCHEMA["tools"], name)
        price_desc = t["inputSchema"]["properties"]["price"]["description"]
        assert "五档即成剩撤" in price_desc
        assert "限价" in price_desc
        # 旧文案不得残留
        assert "对手价市价单" not in price_desc


def test_fallback_matches_tools_schema_json():
    disk = json.loads((ROOT / "docs/tools_schema.json").read_text("utf-8"))
    for name in ("buy", "sell"):
        code_desc = _tool(FALLBACK_TOOLS_SCHEMA["tools"], name)["inputSchema"]["properties"]["price"]["description"]
        disk_desc = _tool(disk["tools"], name)["inputSchema"]["properties"]["price"]["description"]
        assert code_desc == disk_desc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tools_schema_sync.py -v`
Expected: FAIL — 现描述含"对手价市价单"、不含"五档即成剩撤"。

- [ ] **Step 3: Write minimal implementation**

`dispatcher.py` 与 `docs/tools_schema.json` **两处同步**改（`buy`/`sell` 各一份，文案逐字一致）：

- `buy` 顶层 `description`：
  `"下买入委托单。**会真实下单**，慎重调用。不传 price=五档即成剩撤市价单(立即成交、剩余自动撤销、无残留挂单)，回执 status/filled_amount/avg_price 为实际成交；传 price=限价挂单，返回 entrust_no，未成交需自行用 orders_active+cancel 管理。"`
- `buy` 的 `price.description`：
  `"限价买入价格。不传则走同花顺市价委托(五档即成剩撤)立即成交、剩余自动撤销、无残留挂单；传则限价挂单，需自行 orders_active/cancel 管理。"`
- `sell` 同构（把"买入"换"卖出"、去掉数量倍数说明保持原样）。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tools_schema_sync.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/trader/dispatcher.py docs/tools_schema.json tests/test_tools_schema_sync.py
git commit -m "docs(tools): buy/sell 描述改写为市价(五档即成剩撤)/限价两条路径语义

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 【Windows 真机】dump 市价委托面板控件 → 写入 const.py

**Files:**
- Modify: `tools/ths_diag.py`（临时加/复用 `EnumChildWindows` dump 段）
- Modify: `src/trader/ths/const.py`（写入市价委托面板控件常量）

**Interfaces:**
- Produces（值以真机 dump 为准）：
  - `MARKET_TREE_PARENT = "市价委托"`（父节点文字）
  - `MARKET_CODE_ID`（证券代码 Edit，预期 `0x408`）
  - `MARKET_AMOUNT_ID`（数量 Edit，预期 `0x40A`）
  - `MARKET_STRATEGY_COMBO_ID`（委托策略 ComboBox 控件 ID）
  - `MARKET_STRATEGY_TEXT = "五档即成剩撤"` 及其 `MARKET_STRATEGY_INDEX`（下拉项索引）

- [ ] **Step 1: 登录 xiadan，切到目标皮肤**

在 Windows 真机启动 xiadan 并登录（新版/旧版皮肤都要各测一遍——见 Task 7）。

- [ ] **Step 2: 手动导航到「市价委托 └ 买入」面板**

左侧树点开「市价委托」→「买入」，确认右侧出现委托策略下拉（截图对照 spec §1）。

- [ ] **Step 3: dump 该面板控件**

复用 `tools/ths_diag.py` 骨架，在其上临时加一段：定位当前右侧面板 `get_right_hwnd()` → `EnumChildWindows` 打印每个子控件 `(hex(id), class, text, visible)`。运行：
`python tools/ths_diag.py`
记录：证券代码 Edit id、数量 Edit id、委托策略 ComboBox 的 id 与 class（`ComboBox` / `ComboBoxEx32` / 同花顺自绘？）。

- [ ] **Step 4: 读委托策略下拉项与索引**

对 ComboBox 发 `CB_GETCOUNT`/`CB_GETLBTEXT` 枚举各项文字，找到"五档即成剩撤"对应索引（截图里显示为第 4 项"4-五档即成剩撤"，索引大概率 3，**以枚举结果为准**）。若非标准 ComboBox（同花顺自绘），记录其实际交互方式（键盘下拉 / 点击项），留给 Task 6。

- [ ] **Step 5: 写入 const.py 并提交**

把实测值写入 `const.py`（`0x...` 十六进制 + 索引）。删掉 `ths_diag.py` 里的临时 dump 段（或保留为注释掉的 probe）。

```bash
git add src/trader/ths/const.py tools/ths_diag.py
git commit -m "chore(ths): 真机 dump 市价委托面板控件 ID/委托策略索引 写入 const

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 【Windows 真机】树子节点导航 `_select_tree_child`

**Files:**
- Modify: `src/trader/ths/win.py`（在 `_select_tree_node_by_text` 附近新增/扩展）

**Interfaces:**
- Consumes: 现有 `_select_tree_node_by_text` 的跨进程 TreeView 读写原语（`TVM_GETITEMW`/`TVM_GETNEXTITEM`/`TVM_SELECTITEM`/rect→点击、32/64 位 `TVITEM` 选择、Per-Monitor-V2 DPI）。
- Produces: `_select_tree_child(self, parent_text, child_text) -> bool`：先按 `parent_text` 定位父节点句柄，再 `TVGN_CHILD` 取首子、沿 `TVGN_NEXT` 遍历子节点按 `child_text` **精确匹配（去空格）**，命中则 `TVM_SELECTITEM` + 真实鼠标点击。用于区分「市价委托」子节点"买入"/"卖出" 与顶层"买入[F1]"。

- [ ] **Step 1: 抽取可复用的树遍历原语**

`_select_tree_node_by_text` 里的跨进程读写（OpenProcess/VirtualAllocEx/read_text/rect 点击）逻辑较重且与选择策略耦合。抽出私有辅助：`_tree_open(tree)`→句柄+资源、`_tree_read_text(...)`、`_tree_click_node(tree, node, h_proc, remote_text)`，供两个选择方法共用（DRY）。**保持 `_select_tree_node_by_text` 行为不变**（既有交割单/自选股导航依赖它）。

- [ ] **Step 2: 实现 `_select_tree_child`**

```python
    def _select_tree_child(self, parent_text: str, child_text: str) -> bool:
        """先定位 parent_text 父节点，再在其【直接子节点】里精确匹配 child_text 选中。
        用于市价委托 └ 买入/卖出——子节点文字'买入'与顶层'买入[F1]'前缀相同，
        深度优先的 _select_tree_node_by_text 会先撞顶层，故必须限定在父节点子树内。"""
        # 用与 _select_tree_node_by_text 相同的跨进程原语：
        # 1) walk 根找到 _norm(txt)==_norm(parent_text) 的父节点句柄
        # 2) child = TVM_GETNEXTITEM(TVGN_CHILD, parent)
        # 3) 沿 TVGN_NEXT 遍历兄弟，_norm(txt)==_norm(child_text) 精确命中
        # 4) TVM_SELECTITEM(TVGN_CARET) + rect→屏幕坐标真实点击（DPI 换算）
        # 返回是否命中。实现照搬 _select_tree_node_by_text 的资源管理/位数处理。
```

> 完整实现照 `_select_tree_node_by_text` 的结构逐段落地（资源 alloc/free、32/64 位 `TVITEM`、DPI 上下文、点击）——**父节点匹配用整串精确、子节点匹配用整串精确**（非子串），避免"买入" 撞 "买入[F1]"。

- [ ] **Step 3: 真机验证导航命中**

真机手动调用（临时脚本或 REPL）：`backend._select_tree_child("市价委托", "买入")` → 观察右侧确实切到市价买入面板、返回 `True`；`"卖出"` 同理。切树到别处再调一次，确认可从任意起点导航。

- [ ] **Step 4: Commit**

```bash
git add src/trader/ths/win.py
git commit -m "feat(ths): _select_tree_child 树子节点精确导航(市价委托 买入/卖出)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 【Windows 真机】`_submit_market_trade` 完整实现

**Files:**
- Modify: `src/trader/ths/win.py`（把 Task 1 的桩换成真实现）

**Interfaces:**
- Consumes: `switch_to_normal`、`_activate_window`、`_select_tree_child`（Task 5）、`get_right_hwnd`、`_find_input`、`input_ocr`、`get_filled_orders`、`get_active_orders`、`_match_market_fill`（Task 2）、`const.MARKET_*`（Task 4）。
- Produces: `_submit_market_trade(self, op_keyword, stock_no, amount) -> dict` 真实现，返回 Task 2 定义的回执结构。

- [ ] **Step 1: 实现主体**

```python
    def _submit_market_trade(self, op_keyword, stock_no, amount):
        """市价委托(五档即成剩撤)：导航子面板→设策略→填代码/数量→提交→查成交回执。"""
        self.switch_to_normal()
        _activate_window(self.hwnd_main)
        # 下单前快照成交表，供 _match_market_fill 差分
        pre = self.get_filled_orders()
        before = pre.get("data", []) if pre.get("code") == 0 else []

        if not self._select_tree_child(const.MARKET_TREE_PARENT, op_keyword):
            return {"code": 1, "status": "failed",
                    "msg": "未能导航到市价委托面板"}
        time.sleep(sleep_time)
        hwnd = self.get_right_hwnd()

        # 设委托策略=五档即成剩撤（真机确认 ComboBox 交互方式，见 Task 4 Step 4）
        combo = self._find_ctrl_by_id(hwnd, const.MARKET_STRATEGY_COMBO_ID, visible=True) \
            or self._find_ctrl_by_id(hwnd, const.MARKET_STRATEGY_COMBO_ID)
        win32gui.SendMessage(combo, win32con.CB_SETCURSEL,
                             const.MARKET_STRATEGY_INDEX, 0)
        # CB_SETCURSEL 不发 CBN_SELCHANGE，xiadan 可能不认 → 补一发父窗通知
        cid = win32gui.GetDlgCtrlID(combo)
        parent = win32gui.GetParent(combo)
        win32gui.SendMessage(parent, win32con.WM_COMMAND,
                             (win32con.CBN_SELCHANGE << 16) | (cid & 0xFFFF), combo)
        time.sleep(short_sleep_time)

        set_text(self._find_input(hwnd, const.MARKET_CODE_ID), stock_no)
        time.sleep(sleep_time)
        set_text(self._find_input(hwnd, const.MARKET_AMOUNT_ID), str(amount))
        time.sleep(sleep_time)

        hot_key(["enter"])   # 提交 → 确认框
        hot_key(["enter"])   # 确认
        self.input_ocr()     # 反机器人验证码（无弹窗即返回）
        hot_key(["enter"])   # 关结果弹窗
        time.sleep(sleep_time)

        # 查成交回执：轮询成交表拿新增成交
        deadline = time.time() + 8.0
        while time.time() < deadline:
            post = self.get_filled_orders()
            if post.get("code") == 0:
                r = _match_market_fill(before, post.get("data", []),
                                       stock_no, op_keyword, amount)
                if r["code"] == 0:
                    return r
            time.sleep(0.3)
        # 成交表没查到 → 看是否残留在委托表（理论上五档即成剩撤不会）
        return {"code": 2, "status": "unknown", "stock_no": str(stock_no),
                "op": op_keyword, "requested_amount": int(amount),
                "filled_amount": 0,
                "msg": "已提交但未能在成交表确认成交，请自行核对成交/委托"}
```

> `const` 需在 win.py 顶部已 import（Task 2 已加列名常量的 import，这里追加 `MARKET_*`）。`win32con.CBN_SELCHANGE` 若未定义则用字面量 `1`。ComboBox 若为同花顺自绘（Task 4 Step 4 判定）→ 改用键盘选择（点开下拉 + `down_arrow`×index + `enter`），按真机结果替换本段。

- [ ] **Step 2: 真机小额实单验证（买）**

真机跑一手：`asyncio.run(backend.buy("<流动性好的低价股>", 100))`（不传 price）。核对：面板是市价委托买入、委托策略显示五档即成剩撤、代码/数量正确、（若弹）验证码通过、返回 `status=filled`、`filled_amount=100`、`avg_price` 与客户端成交一致、`orders_active` 无残留。

- [ ] **Step 3: 真机小额实单验证（卖）**

对刚买入的持仓卖 100：`asyncio.run(backend.sell("<同一代码>", 100))`。核对同上（`op=卖出`）。

- [ ] **Step 4: 限价路径回归**

`asyncio.run(backend.buy("<代码>", 100, <远离现价的低价>))` 确认仍走 F1 限价、留在 `orders_active`、能 `cancel`——证明限价路径未被破坏。

- [ ] **Step 5: Commit**

```bash
git add src/trader/ths/win.py
git commit -m "feat(ths): _submit_market_trade 市价委托(五档即成剩撤)真实现

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: 【Windows 真机】双皮肤联调 + 文档收尾

**Files:**
- Modify: `docs/ths_architecture.md`（补市价委托路径章节）

**Interfaces:** 无新增。

- [ ] **Step 1: 新版皮肤全流程复测**

新版皮肤下重跑 Task 6 Step 2/3/4，确认控件递归查找 + 可见性过滤在多套一层容器下仍命中。

- [ ] **Step 2: 旧版皮肤全流程复测**

右上角切旧版皮肤，重跑同样三单。若控件 ID/策略索引与新版不一致 → 在 `const.py` 记录差异并让 `_submit_market_trade` 兼容（参考架构文档 §0 "两套皮肤基本一致"的既有处理方式）。

- [ ] **Step 3: 补架构文档**

在 `docs/ths_architecture.md` 加一节「市价委托路径」：菜单位置（市价委托 └ 买入/卖出，无热键 → `_select_tree_child`）、委托策略固定五档即成剩撤及理由、控件 ID 表、回执查成交表而非委托表的原因、与限价路径的分工。

- [ ] **Step 4: 全量回归 + Commit**

Run（macOS 侧纯逻辑）: `pytest tests/ -v`
Expected: 全绿（含 Task 1/2/3 新测 + 既有 `test_market_price`/`test_order_watch*` 等）。

```bash
git add docs/ths_architecture.md
git commit -m "docs(ths): 架构文档补市价委托路径(五档即成剩撤/子节点导航/成交回执)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage：**
- spec §2 选五档即成剩撤 → Task 4/6 固定该策略；理由入 Task 7 文档。✓
- spec §3 方案 A 接口 → Task 1 分派。✓
- spec §4.1 真机 dump 控件 → Task 4。✓
- spec §4.3 `_submit_market_trade` 流程 → Task 6。✓
- spec §4.4 树子节点导航 → Task 5。✓
- spec §4.5 查成交表回执 + 部分成交 → Task 2（纯函数三态）+ Task 6（轮询接入）。✓
- spec §4.6 工具描述 → Task 3。✓
- spec §5 语义变更/回执时序/验证码复用/真机依赖 → Task 3 文案说明 + Task 6 轮询 8s + `input_ocr` 复用 + Task 4/5/6/7 真机门。✓
- spec §6 测试（macOS 桩测三态 + 真机实单 + 回归）→ Task 1/2/3 桩测、Task 6/7 实单、Task 7 全回归。✓

**Placeholder scan：** Task 4/5/6 含"真机以…为准"是**如实的已知未知**（控件值只能真机取），非可现在消除的 TODO；纯逻辑任务（1/2/3）代码完整、无占位。Task 5 Step 2 给的是结构骨架 + 明确"照 `_select_tree_node_by_text` 逐段落地"，因该方法 ~140 行跨进程代码全量复制进计划无意义且违 DRY——以复用其原语的方式指明。✓

**Type consistency：** `_submit_market_trade(op_keyword, stock_no, amount)` 签名在 Task 1（桩）/Task 6（实现）一致；`_match_market_fill(before, after, stock_no, op_keyword, requested_amount)` 在 Task 2 定义、Task 6 调用一致；回执字段 `status/filled_amount/avg_price/requested_amount/op` 全程一致；`const.MARKET_*` / `FILLED_COL_*` 命名 Task 2/4/6 一致。✓
