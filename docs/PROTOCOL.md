# guling-trader & Gateway/Relay Communication Protocol (V1 传输层 / 回执契约 v2)

This document formally specifies the communication protocol between the Windows Trader client (`guling-trader.exe`) and the Cloud Gateway (`guling-mcp-gateway` or any custom private relay). 

By adhering to this protocol, developers can write custom relays (e.g. standard terminal stdio scripts or SSE endpoints) to control the trader without requiring any changes to the core client executable.

---

## 1. Architectural Role Breakdown

The communication model is strictly outbound-only from the trader client to simplify networking and security:
- **Windows Trader (`guling-trader`)**: Functions as a persistent, outbound-only **WebSocket Client**. It does not listen on any network ports. It expects standard JSON-RPC raw commands and processes them locally on the Windows desktop (via the THS Broker API).
- **Gateway / Relay**: Functions as the **WebSocket Server**. It receives the inbound WebSocket connection from the Windows Client, exposes standard Model Context Protocol (MCP) or customized APIs to consumer applications (like Cursor, Claude, or custom algos), and handles all protocol translation (unwrapping/wrapping).

```
   [ AI Client / Cursor ] (SSE or stdio)
             │
             ▼ standard MCP (initialize, tools/call {name, arguments})
      [ Gateway / Relay ] (Go/Python Server)
             ▲
             │ WebSocket Connection (outbound-only from Trader)
     [ Windows Trader Client ] (python/PyInstaller exe)
```

---

## 2. Handshake Phase & Session Upgrades

Every WebSocket connection starts with an initial handshake exchange to pair the client or resume an existing session.

### Scenario A: First-time Pairing (Pairing Code Workflow)
1. **Trader Sends Hello (Initiate Pair)**:
   ```json
   {
     "type": "hello",
     "mode": "pair_init",
     "device_id": "unique-uuid-v4-string"
   }
   ```
2. **Gateway Responds Pending**:
   ```json
   {
     "type": "pair_pending",
     "code": "XXXXXX",
     "expires_at": "2026-05-22T20:15:00+08:00"
   }
   ```
   *The gateway generates a 6-digit validation code and sets an expiry. The trader displays this code on screen.*
3. **User submits code via AI Client to Gateway**: The AI Client calls `pair_with_code` on the Gateway.
4. **Gateway binds and Upgrades connection**:
   ```json
   {
     "type": "bind_ok",
     "account_name": "主账户",
     "agent_token": "secure-agent-token-random-uuid-or-hash",
     "session_id": "session-uuid"
   }
   ```
   *The trader receives `bind_ok`, saves the `agent_token` locally into its `config.json`, and enters the running loop.*

---

### Scenario B: Session Resumption (Skip Pairing Workflow)
For user-defined private relays or re-connecting clients, pairing can be skipped entirely by pre-configuring `agent_token` in `config.json`.
1. **Trader Sends Hello (Resume)**:
   ```json
   {
     "type": "hello",
     "mode": "resume",
     "device_id": "unique-uuid-v4-string",
     "agent_token": "stored-agent-token"
   }
   ```
2. **Gateway Responds Welcome**:
   If the token is valid, the Gateway upgrades the session immediately and replies:
   ```json
   {
     "type": "welcome"
   }
   ```
3. **Gateway Responds Reject (On failure)**:
   If the token is invalid, expired, or rejected for other security reasons:
   ```json
   {
     "type": "reject",
     "reason": "token_invalid"
   }
   ```

#### Reject Reason Code Specifications:
- `token_invalid` or `account_removed`: The trader client immediately **clears its local config** (removing the token) and drops back to `pair_init`.
- `evicted_by_other_session`: Another client session connected with the same token. The trader enters a **30-second cool-down delay** before attempting a reconnection, avoiding rapid reconnect looping.
- `brute_force_blocked`: Too many failed pairing attempts. The trader client enters a **60-second cool-down delay**.

---

## 3. Running Phase (RPC Call & Reply Envelopes)

Once paired and welcomed, all commands use standard single-layer raw JSON-RPC envelopes.

### 3.1. Gateway-to-Trader: `call` Envelope
When the AI Client triggers a tool, the Gateway unwraps the tool parameter block `{name, arguments}` and sends a naked method call to the Trader client:
```json
{
  "type": "call",
  "id": "transaction-unique-id",
  "method": "buy",
  "params": {
    "stock_no": "600000",
    "amount": 100,
    "price": 7.58,
    "client_order_id": "custom-uuid"
  }
}
```
*Note that the Gateway has completely stripped standard MCP `"tools/call"` wrappers here, presenting pure naked broker commands to the Trader.*

### 3.2. Trader-to-Gateway: `reply` Envelope（契约 v2）

reply 帧本身仍是单层 `{type,id,ok,result|error}`；**`result` 一律是契约 v2 信封**，
所有工具无例外形（含 buy/sell/cancel，含失败与 busy）：

```json
{
  "status": "succeed" | "failed" | "busy",
  "code": "<机器枚举串>",
  "data": <载荷或 null>,
  "error": {"class": "<枚举>", "broker_msg": "<柜台原文或 null>", "message": "<我方人话>"} | null,
  "contract_version": "2"
}
```

`contract_version` 亦通过网关 `initialize` 的 `serverInfo.contract_version` 暴露，
消费侧无需先调业务工具即可判版。

#### `code` 值域（机器枚举）

| code | 含义 | status |
|---|---|---|
| `ok` | 成功 | succeed |
| `busy` | 受控端窗口忙，**本笔未执行** | busy |
| `call_timeout` | 查询类超时 | failed |
| `submitted_unconfirmed` | **已点提交，结果不可知** | failed |
| `rejected` | 柜台明确拒绝 | failed |
| `read_failed` | 抓不到数据 | failed |
| `table_mismatch` | 抓到的不是本次请求的表（已拒绝返回错表） | failed |
| `not_bound` | 未检测到 xiadan 窗口 | failed |
| `plugin_disabled` | 交易插件被禁用 | failed |
| `invalid_params` | 参数非法 / coid 复用冲突 | failed |
| `ledger_unavailable` | 下单台账不可用（**已拒单**） | failed |
| `not_found` | query_order 查无此单 / 撤单找不到该委托 | failed |
| `aborted` | 本笔已被超时作废（代次机制） | failed |
| `unsupported_method` | 方法不在白名单 | failed |
| `internal_error` | 受控端内部错误 | failed |

#### ⚠️ `status: failed` **不等于**「未提交」

`code == submitted_unconfirmed` 时委托**可能已经在柜台**。判定必须看 `code`，不能看
`status`。此时调用方唯一安全动作是**用同一 `client_order_id` 原样重发**（幂等，见
3.2.3），或调 `query_order` 核实；**禁止改单重下**。

#### `error.class` 两层分类（C2）

* **结构性判定**（我方控制流得出，可靠）：`busy` `call_timeout` `unknown_outcome`
  `not_bound` `plugin_disabled` `read_failed` `table_mismatch` `invalid_params`
  `ledger_unavailable` `not_found` `aborted` `internal_error`
* **柜台原文尽力映射**：`insufficient_funds` `price_out_of_limit` `invalid_quantity`
  `suspended` `no_permission` `broker_timeout`，**认不出一律 `unknown`**。

`broker_msg` 永远保留柜台原文。**`class == unknown` 与所有 unknown_outcome
一律不可自动重试**——关键词表是尽力而为的，误判「可重试」会真的重复下单。
不可自动重试集合：`unknown` `unknown_outcome` `insufficient_funds` `no_permission`
`invalid_quantity` `invalid_params` `ledger_unavailable`。

#### 3.2.1 busy 背压语义（G3）

受控端对 THS 单窗口全程串行（`win_lock`）。排队超过 5 s 即回：

```json
{"status": "busy", "code": "busy", "data": {"submitted": false, "retry_after_secs": 3},
 "error": {"class": "busy", "broker_msg": null, "message": "..."}, "contract_version": "2"}
```

`submitted: false` 是硬保证——busy 时指令**根本没执行**。建议退避 `retry_after_secs`
（当前 3 s）后重试。busy 是背压信号，不是故障。

受控端单笔总预算 25 s（低于网关 30 s），保证网关总能等到带语义的 reply。超时后受控端
会作废在飞线程（代次机制）并置 degraded，下一笔进入前先清残留弹窗。

#### 3.2.2 空表语义（B3，永久锁定）

**「真的没有」与「拿不到」必须可区分**，这是消费侧一切降级判断的地基：

* 今天无挂单 / 无成交 → `status: succeed`，`data: []`。**空表是成功**。
* 抓不到 / 抓到错表 → `status: failed`，`code: read_failed | table_mismatch`，
  **绝不返回空数组冒充「没有」**。

#### 3.2.3 client_order_id 与幂等（C4/C5a）

* coid **不写入柜台**（同花顺委托无自定义字段），仅存于受控端本地台账（SQLite，
  保留 ≥5 交易日）。
* **幂等**：`buy`/`sell`/`cancel` 传 coid 后，同 id 重复提交**绝不产生第二次提交**，
  返回首次记录的回执；首次结果尚未落定时返回 `submitted_unconfirmed`——
  这是合法态，不是 bug（最危险那一刻台账自己也不知道结果）。
* 同 id **不同参数** → `invalid_params` 拒绝执行（调用方 id 复用 bug，不静默）。
* **台账不可用一律拒单**（`ledger_unavailable`），禁静默降级为无幂等下单。
* **回显是尽力而为**：`orders_active` / `orders_filled` 按 entrust_no join 回显 coid；
  回查不到合同编号的单（超时那批）与外部/人工单为 `null`。**对账主键是 entrust_no，
  coid 是增强关联**。
* 建议 coid 全局唯一且含账户维度——受控端 `switch_account` 是盲切，对账户身份无感知。

#### 3.2.4 查单（C5b）

`query_order(client_order_id)` → `state` ∈ 未报/已报/部成/已成/已撤/废单/**未知**，
并给出 `resolution`：`by_entrust_no`（精确命中）/ `heuristic`（台账无合同编号，按
代码+数量匹配，**同参重复单存在歧义**）/ `unresolved`（零命中或多命中 → `state=未知`，
需人工）。`unknown` 态被收窄到「回查确认前」，但**不可能被消灭**。

#### 3.2.5 数值与单位（C6）

数值字段一律 JSON number：金额单位元（取整到分）、价格单位元（到厘）、数量单位股（int）、
百分比键名以 `_pct` 结尾（不带 % 符号）。**THS 的 `--`/空占位符一律映射 `null`，
绝不映射 0**——0 是真实数字，把「没有」写成 0 会被下游当真值用。
键名保留中文，与同花顺界面列名同字面（人工对屏审计零翻译成本）。

#### 3.2.6 委托表语义（C3）

`orders_active` **只返回在飞单**（未报/已报/部成）；已成/已撤/废单不出现。
**状态识别不出的行按「在飞」保守返回**——宁可多给一行，也不能把一张活着的挂单藏起来
（孤儿挂单架空止损哨兵是最险的失效模式）。行结构：`client_order_id, entrust_no,
证券代码, 证券名称, 方向, 委托价, 委托数量, 已成数量, 成交均价, 状态, 柜台备注`。

`order_event` 推送读的是**含终态的全量委托表**（内部通道），不受上述过滤影响。

#### 3.2.7 成交时间与时区（B2）

`orders_filled.成交时间` 为 ISO 8601 带偏移。THS 成交表只给 `HH:MM:SS`，
**日期与时区由受控端本机时钟补齐，不是柜台时间**——对账时按此理解。

任何 reply 都可能携带 `data.dialogs` 数组（受控端自动处置的客户端弹窗存证：
`[{"title","text","action"}]`，仅作取证，无需动作）。

#### Gateway-side call timeout (MANDATORY semantics)

受控端在 25 s 内必给回执（低于网关 30 s）。若网关自身超时仍未收到 `reply`
（受控端离线/断网），网关**不得**只回裸传输错误（如 `-32003`）：下单类命令缺回执
意味着委托**可能已提交**，必须给出等价于 `submitted_unconfirmed` 的语义文本：

> 受控端未在时限内响应，委托**可能已提交**。安全动作=用同一 `client_order_id`
> 原样重发（幂等），或调 `query_order`/`orders_active` 核实；**禁止改单重下**。

Rationale：2026-07-13「报错但静默成交」几乎导致重复下单。

网关另有两条硬性要求：

1. **失败也必须把完整信封交给客户端**（`isError: true` + `content[0].text` 为信封
   JSON），只回一句散文等于在网关层丢掉机器分类能力；
2. **回执配对键 = (agentToken, 网关自生成 id)**，客户端 JSON-RPC id 只回填响应、
   **不参与配对**——id 唯一性不是客户端的契约义务（G1/G2；2026-08-03 串线事故根因）。

#### 3.2.8 会话生命周期（G4）

| 场景 | 返回 |
|---|---|
| sid 过期/失效 | JSON-RPC error `-32001`，文案含 `Session expired or invalid` → 重新握手 |
| 未带凭证 | `-32001`，文案含 `Missing agent token` |
| 受控端离线 | `-32001`，文案含「Windows 交易端 WebSocket 未在线」→ 非会话问题，勿重握手 |
| 网关等待超时 | `-32003` + 上述 unknown 语义 |

#### 3.2.9 消费侧节奏建议（S2）

受控端对 THS 单窗口全程串行，吞吐上限由 RPA 决定，不是并发能力问题：

* 单账户建议**并发 1**（多客户端并发只会互相 busy）；
* 最小轮询间隔建议 ≥ 60 s（查询类单笔典型 1–3 s，交割单可达数十秒）；
* 收到 busy 按 `retry_after_secs` 退避，不要立即重试；
* 下单类务必带 coid，超时后**重发同 id**而不是新建单。

### 3.3. Trader-to-Gateway: `order_event` Push (Unsolicited)

Unlike `reply` (which always answers a preceding `call` and carries its `id`),
`order_event` is an **unsolicited push** emitted by the trader on its own
initiative — there is **no preceding `call` and no `id`**. The trader
periodically snapshots the broker's *today's orders* table (`orders_active`,
which re-queries the broker via F5) and diffs successive snapshots; when an
order's lifecycle changes it pushes one frame. Because the snapshot reflects
the **broker's server-side order book**, this covers orders from **any
source** — agent-placed RPC orders, manual orders placed on the Windows
client, and orders placed from the user's **mobile app** on the same account.

```json
{
  "type": "order_event",
  "event": "placed",
  "source": "external",
  "entrust_no": "1928374",
  "stock_no": "600000",
  "op": "买入",
  "order_qty": 100,
  "order_price": "7.580",
  "filled_qty": 0,
  "avg_price": "",
  "note": "已报",
  "seq": 12,
  "ts": 1782900000.0
}
```

- `event`: one of `placed` | `partially_filled` | `filled` | `canceled`.
  `partially_filled` may fire multiple times; `filled` is terminal.
- All business fields use the **verbatim THS column names** as source:
  `stock_no`=证券代码, `op`=操作(买入/卖出), `order_qty`=委托数量,
  `order_price`=委托价格, `filled_qty`=成交数量, `avg_price`=成交均价,
  `note`=备注. `order_price`/`avg_price` are strings and **may be empty**
  (e.g. market orders, or before any fill).

#### Gateway handling
`order_event` is **not** a `reply` and carries no `id`, so the gateway does
**not** route it through RPC correlation. It falls through to the gateway's
generic non-reply path and is **broadcast to the live SSE control session(s)**
bound to this connection's `agent_token` (e.g. `guling-mcp-gateway`'s
`BroadcastToControl`, logged as `trader.event`). **No gateway change is
required** to relay it.

#### Contract notes (consumers MUST honor)
- **Account identity is carried by the connection token, not the frame.**
  One WebSocket connection = one THS account; the frame body intentionally
  omits any account/portfolio field. Consumers map `agent_token` →
  (user, portfolio). (An optional `account` echo field MAY be added later for
  defensive logging only; it must never be used for routing.)
- **Delivery is best-effort and lossy.** If no live SSE control session
  exists for the token, if the buffer is full, or during disconnects, events
  are **dropped and never replayed**. Consumers MUST keep a
  reconciliation/query path as the source of truth and treat `order_event`
  purely as a latency optimization; re-run reconciliation on SSE reconnect.
- **Deduplicate by `entrust_no`** (optionally with `filled_qty`). Do **NOT**
  use `seq`: `seq` is a per-process monotonic counter that **resets to 0 on
  trader restart**.
- **`source` is best-effort.** It is `"agent"` if the order was placed via
  this trader's own RPC `buy`/`sell`, else `"external"` (mobile/manual). A
  trader restart clears the in-memory set (agent orders then look
  `external`), and a race can briefly mislabel. Consumers SHOULD determine
  authoritative source from their own known `entrust_no` set and treat
  `source` as a hint.
- **`ts`** is the trader's local wall-clock epoch seconds (`time.time()`) at
  detection — **advisory only**. Use your own receive/reconcile time for the
  ledger; the trader clock may drift.
- **First snapshot after (re)start establishes a baseline only** — no
  historical events are replayed.
- **Cadence is adaptive, minute-scale** (idle ≈ 5 min, ≈ 1 min while any
  order is open; configurable). Events therefore carry **minute-scale
  latency, not real-time**. Higher frequency is constrained by THS captcha
  popups triggered on each re-query.
- **Known gap:** a cancellation that manifests as the order row *disappearing*
  from today's orders (rather than `备注` turning `已撤`) emits **no event**
  (conservative, to avoid false cancels). This relies on the broker retaining
  same-day `已成`/`已撤` rows; verify per broker/version and rely on
  reconciliation as the backstop.

---

## 4. MCP Server Translation Responsibilities

To achieve a "dumb/raw" client design, the Gateway/Relay acts as the exclusive **MCP Translator**.

### 4.1. Lifecycle interception
The Gateway handles MCP lifecycle calls (`initialize`, `notifications/initialized`, `ping`) **locally** in the Gateway:
- **Unpaired State**: Intercepts `initialize` and returns success with an empty tools schema, offering only the `pair_with_code` tool under `tools/list` to prevent standard AI clients (like Cursor) from crashing or aborting the SSE connection.
- **Paired State**: Intercepts `initialize` and answers locally, preserving the tool list definitions mapped dynamically from the trader.

### 4.2. Action/Method translation
When an AI client issues an MCP tool call:
1. **Unwrap**: The gateway intercepts `method: "tools/call"`, extracts `params.name` (e.g. `"buy"`) and `params.arguments` (e.g. `{"stock_no": "600000", ...}`).
2. **Naked Forward**: The gateway translates this into a standard raw frame `method: "buy"`, `params: {...}` and routes it through the WebSocket channel.
3. **Wrap Result**: Upon receiving the `reply` envelope from the WebSocket channel, the gateway packages the result into a standard-compliant MCP `CallToolResult`:
   - **`ok: true` (Success)**:
     ```json
     {
       "jsonrpc": "2.0",
       "id": "original-client-id",
       "result": {
         "content": [
           {
             "type": "text",
             "text": "{\"code\": 0, \"status\": \"succeed\", ...}"
           }
         ],
         "isError": false
       }
     }
     ```
   - **`ok: false` (Failure or Code 2 Warning)**:
     ```json
     {
       "jsonrpc": "2.0",
       "id": "original-client-id",
       "result": {
         "content": [
           {
             "type": "text",
             "text": "可用资金不足"
           }
         ],
         "isError": true
       }
     }
     ```
     *Crucial Rule: Do NOT collapse the standard HTTP or JSON-RPC transport layer with error statuses (-32xxx codes) during tool failures. Tool errors must be mapped as HTTP 200 containing `isError: true` inside standard JSON-RPC results, ensuring diagnostics are clearly read by Cursor/Claude.*

---

## 5. Keep-Alive / Heatbeat
To remain resilient across diverse networks and private custom relays:
- **Gentle Client Ping**: The `guling-trader` client maintains a gentle ping configuration:
  - `ping_interval = 30` seconds
  - `ping_timeout = 60` seconds
- **Server Response**: The gateway or custom relay MUST automatically reply with a protocol-level `PONG` upon receiving client `PING` frames to prevent connection degradation.
