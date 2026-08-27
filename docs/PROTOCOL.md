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
    "client_order_id": "gl-0198f6a1-0001-7000-8000-000000000001"
  }
}
```
*Note that the Gateway has completely stripped standard MCP `"tools/call"` wrappers here, presenting pure naked broker commands to the Trader.*

### 3.2. Trader-to-Gateway: `reply` Envelope（契约 v2）

reply 帧本身仍是单层 `{type,id,ok,result|error}`；除发现接口 `tools/list` 外，业务工具的
**`result` 一律是契约 v2 信封**，含 buy/sell/cancel/confirm_external_cancel 的失败与 busy：

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

`tools/list` 是唯一的发现接口例外：其 `reply.result` 为裸
`{"tools": [...]}`，方便网关直接取得 JSON Schema；它不是业务回执，消费侧不能把它按
契约 v2 信封解析。

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
| `confirmation_required` | 未登记订单撤单需要显式确认，尚未点击 GUI | failed |
| `aborted` | 本笔已被超时作废（代次机制） | failed |
| `unsupported_method` | 方法不在白名单 | failed |
| `internal_error` | 受控端内部错误 | failed |

#### ⚠️ `status: failed` **不等于**「未提交」

`code == submitted_unconfirmed` 时交易动作**可能已经在柜台**。判定必须看 `code`，不能看
`status`。此时调用方唯一安全动作是**用同一 `client_order_id` 原样重发**（幂等，见
3.2.3），或调 `query_order` 核实；**禁止改单重下**。

#### `error.class` 两层分类（C2）

* **结构性判定**（我方控制流得出，可靠）：`busy` `call_timeout` `unknown_outcome`
  `not_bound` `plugin_disabled` `read_failed` `table_mismatch` `invalid_params`
  `ledger_unavailable` `not_found` `confirmation_required` `aborted` `internal_error`
* **柜台原文尽力映射**：`insufficient_funds` `price_out_of_limit` `invalid_quantity`
  `suspended` `no_permission` `broker_timeout`，**认不出一律 `unknown`**。

`broker_msg` 永远保留柜台原文。**`class == unknown` 与所有 unknown_outcome
一律不可自动重试**——关键词表是尽力而为的，误判「可重试」会真的重复下单。
不可自动重试集合：`unknown` `unknown_outcome` `insufficient_funds` `no_permission`
`invalid_quantity` `invalid_params` `ledger_unavailable` `confirmation_required`。后者必须由
用户显式确认后再调用 `confirm_external_cancel`，不能由通用重试器代为继续。

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
* **必填、格式与责任边界**：`buy`/`sell`/`cancel`/`confirm_external_cancel` 的 coid 必须为
  `gl-<小写 UUID v7>`，例如 `gl-0198f6a1-0001-7000-8000-000000000001`。UUID v7
  含毫秒时间戳和随机位；调用方必须在创建业务请求时生成并持久保存它，网络重发、
  `query_order` 查询同一交易动作时原样复用。交易端只验证格式和台账幂等，不生成、
  不改写，也不能替调用方判断两个未持久化的请求是否属于同一笔业务订单。
* **幂等**：同 id 重复提交**绝不产生第二次提交**，返回首次记录的回执；首次结果
  尚未落定时返回 `submitted_unconfirmed`——这是合法态，不是 bug（最危险那一刻台账
  自己也不知道结果）。同 id 不同参数仍会被拒绝。
* 同 id **不同参数** → `invalid_params` 拒绝执行（调用方 id 复用 bug，不静默）。
* **台账不可用一律拒单**（`ledger_unavailable`），禁静默降级为无幂等下单。
* **回显是尽力而为**：`orders_active` / `orders_filled` 仅按原买卖单的 entrust_no join
  回显 coid；撤单动作引用同一编号但不会覆盖原买卖单的回显。回查不到合同编号的单
  （超时那批）与外部/人工单为 `null`。**对账主键是 entrust_no，coid 是增强关联**。
* coid 应全局唯一；调用方的持久记录必须把它与目标账户绑定。固定 UUID v7 格式不
  携带账户文本；受控端启动后不会把任何账户视为已核验。连接完成后会发送一次只读的
  `account_event`，列出可选账户，供调用方选择；该事件丢失时调用方必须使用
`list_accounts` 查询。每次连接后的首次 `buy`/`sell`/`cancel`/`confirm_external_cancel` 前必须
  成功调用 `switch_account(slot)` 明确选择账户，即使目标已经是当前账户也一样。该工具会把
  槽位对应的下拉列表文本与控件 `0x094C` 当前文本核对；目标已是当前账户时不发送热键，
  否则才发送 `Alt+N`。成功后才建立本进程基线。此后每笔交易前都会重新读取并比对；读取
  失败或文本变化时禁止买卖和撤单，不读取订单表、不消费人工撤单令牌，也不向同花顺发送
  交易输入。
* `buy`/`sell` 首次或幂等重放返回 `submitted_unconfirmed` 时，受控端会自动执行**一次**
  只读 `query_order`，把结果放入 `data.auto_query`；顶层仍保持
  `submitted_unconfirmed`，不会把启发式命中伪装成精确确认，**绝不自动重发下单**。
* `cancel` 点击确认后会短暂只读轮询 F3 委托表。只有唯一目标行明确显示 `已撤`/`部撤` 才
  返回成功；目标消失、仍在飞、状态不明、错表或读取失败都返回 `submitted_unconfirmed`，因为
  F3 中消失也可能是订单已全部成交，不能伪装成撤单成功。其首次或幂等重放返回
  `submitted_unconfirmed` 时，受控端会用该撤单请求保存的目标 `entrust_no`，自动读取**一次**
  含终态的内部全量委托表，结果同样放入 `data.auto_query`；它不会调用买卖单的启发式
  查询，也**绝不自动重发撤单**。只在全量表精确命中且 `cancel_state` 为 `已撤` 或
  `部成后已撤` 时，才可判定柜台已确认撤单；`已成`、`仍在飞`、`废单`、`未知`都不是撤单成功。
  表读取失败、零命中或多命中时保守返回 `未知`，不把“查不到”当作“已撤”。

##### 未登记订单撤单二次确认

本地配置 `external_cancel_confirmation` 的默认值是 `two_step`。订单是否“已登记”只以本系统
本地台账中保存的目标 `entrust_no` 为准，不能用 `order_event.source` 等提示字段替代。

* 已登记订单：`cancel(entrust_no, client_order_id)` 直接走普通撤单路径。
* 未登记/人工/外部订单：`cancel` 会先从含终态的全量委托表唯一读取目标，再返回
  `code=confirmation_required`、`error.class=confirmation_required` 和 60 秒一次性的
  `data.confirmation_token`；此阶段 `submitted=false`，**绝不点击 GUI**。
* 调用方展示该订单摘要后，必须以**新的** `client_order_id` 调用
  `confirm_external_cancel(confirmation_token, client_order_id)`；不能复用产生令牌的
  `cancel` ID。网络重发同一确认仍须复用该确认 ID，令牌本身只能消费一次。
* 消费令牌后，受控端再次读取全量委托表，并按合同号唯一匹配，逐项比较合同号、证券代码、
  方向、委托价、委托数量、已成数量及可撤状态。订单已成交、订单变化、零/多行匹配、表读取失败、
  令牌过期或已使用时均停止，不执行撤单。
* 令牌只保存在当前进程内，且绝不写入本地台账；进程重启或 WebSocket 连接代次切换会使它失效。
  调用方用**原 `cancel` 的同一 client_order_id** 再次调用 `cancel`，受控端会重新读表并换发
  令牌；旧令牌立即失效，整个刷新过程不点击 GUI。令牌的有效期固定为 60 秒。
* 设置为 `external_cancel_confirmation=direct` 时，任何订单的 `cancel` 都按普通撤单路径
  直接执行，不创建令牌，也不要求 `confirm_external_cancel`。

无论处于哪种模式，超时、未知结果或自动回查都**绝不自动重发真实撤单**。调用方只能显式地
使用同一动作的 `client_order_id` 进行幂等重放，或调用 `query_order` 核验。

#### 3.2.4 查单（C5b）

买卖 `query_order(client_order_id)` → `state` ∈ 未报/已报/部成/已成/已撤/废单/**未知**，
并给出 `resolution`：`by_entrust_no`（精确命中）/ `heuristic`（台账无合同编号时，
活跃委托须代码、方向、数量一致；限价单还须委托价一致；成交表须代码、方向、数量一致；
**同参重复单仍有歧义**）/ `unresolved`（零命中或多命中 → `state=未知`，需人工）。

对实际执行撤单的 `cancel` 或 `confirm_external_cancel` 的 `client_order_id`，`query_order`
按该撤单动作保存的目标 `entrust_no` 精确读取含终态的全量委托表，返回原委托的 `state` 与
专用 `cancel_state`：`已撤`、`部成后已撤`、`已成`、`仍在飞`、`废单` 或 `未知`。这两类
撤单查询都不使用买卖单的启发式匹配；只有 `resolution=by_entrust_no` 才表示精确关联。
全量表读取失败或找不到唯一目标则 `resolution=unresolved`、`cancel_state=未知`。`unknown`
态被收窄到「回查确认前」，但**不可能被消灭**。
为兼容升级前已写入的台账，`query_order` 可读取历史的非 UUID v7 ID；新建的
`buy`/`sell`/`cancel` 仍只接受规范 UUID v7。

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

### 3.3. Trader-to-Gateway: `account_event` Push (Unsolicited)

每次 WebSocket 连接完成后，受控端会**尝试一次**只读读取同花顺账户下拉列表，并发送：

```json
{
  "type": "account_event",
  "event": "available",
  "accounts": [
    {"slot": 1, "shortcut": "Alt+1", "text": "券商-王*甲"}
  ],
  "current_account_text": "券商 王*甲",
  "partial": false,
  "ts": 1782900000.0
}
```

`event=unavailable` 表示连接时未能读取列表，会有空 `accounts`、`partial=true` 和可读的
`message`。此事件不选择账户、不发送热键、不建立交易账户基线，且仅作提示：主动事件在
断线时可能丢失，调用方必须可通过 `list_accounts` 重新查询。调用方展示列表后，必须调用
`switch_account(slot)` 明确核验用户选择的账户，哪怕该槽位已经是当前账户。

与 `order_event` 一样，`account_event` 没有 `id`，由网关通用非 reply 路径广播给当前
控制会话；无需网关改动。它不应被当作可靠状态存储或交易授权依据。

### 3.4. Trader-to-Gateway: `order_event` Push (Unsolicited)

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
  "order_price": 7.58,
  "filled_qty": 0,
  "avg_price": null,
  "note": "已报",
  "seq": 12,
  "ts": 1782900000.0
}
```

- `event`: one of `placed` | `partially_filled` | `filled` | `canceled`.
  `partially_filled` may fire multiple times; `filled` is terminal.
- All business fields use the **verbatim THS column names** as source and are
  normalized before transmission:
  `stock_no`=证券代码, `op`=操作(买入/卖出), `order_qty`=委托数量,
  `order_price`=委托价格, `filled_qty`=成交数量, `avg_price`=成交均价,
  `note`=备注. `order_price`/`avg_price` are `number | null`; `null` means the
  client did not provide a usable numeric value (for example a market order,
  or before any fill).

#### Gateway handling
`order_event` is **not** a `reply` and carries no `id`, so the gateway does
**not** route it through RPC correlation. It falls through to the gateway's
generic non-reply path and is **broadcast to the live SSE control session(s)**
bound to this connection's `agent_token` (e.g. `guling-mcp-gateway`'s
`BroadcastToControl`, logged as `trader.event`). **No gateway change is
required** to relay it.

#### Contract notes (consumers MUST honor)
- **网关路由身份仍由连接 token 决定，而不是券商账户文本。** 一个受控端连接可登录
  多个同花顺账户；`account_event` 和 `switch_account` 回执中的账户文本只用于让用户
  选择和让受控端做本地核验，绝不能作为网关路由或跨账户订单归属的依据。消费者应把
  `agent_token` 映射到用户/受控端，再根据其明确的账户选择维护自己的业务归属。
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
            "text": "{\"status\":\"succeed\",\"code\":\"ok\",\"data\":{...},\"error\":null,\"contract_version\":\"2\"}"
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
            "text": "{\"status\":\"failed\",\"code\":\"rejected\",\"data\":null,\"error\":{\"class\":\"insufficient_funds\",\"broker_msg\":\"可用资金不足\",\"message\":\"柜台拒绝本次委托\"},\"contract_version\":\"2\"}"
           }
         ],
         "isError": true
       }
     }
     ```
     *Crucial Rule: Do NOT collapse the standard HTTP or JSON-RPC transport layer with error statuses (-32xxx codes) during tool failures. Tool errors must be mapped as HTTP 200 containing `isError: true` inside standard JSON-RPC results. The `text` field must retain the complete v2 envelope rather than flattening it to a bare error message.*

---

## 5. Keep-Alive / Heatbeat
To remain resilient across diverse networks and private custom relays:
- **Gentle Client Ping**: The `guling-trader` client maintains a gentle ping configuration:
  - `ping_interval = 30` seconds
  - `ping_timeout = 60` seconds
- **Server Response**: The gateway or custom relay MUST automatically reply with a protocol-level `PONG` upon receiving client `PING` frames to prevent connection degradation.
