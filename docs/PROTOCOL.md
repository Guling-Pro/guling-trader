# guling-trader & Gateway/Relay Communication Protocol (V1)

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

### 3.2. Trader-to-Gateway: `reply` Envelope
The Trader executes the transaction and responds with a single-layer reply frame:

#### Successful response:
```json
{
  "type": "reply",
  "id": "transaction-unique-id",
  "ok": true,
  "result": {
    "code": 0,
    "status": "succeed",
    "entrust_no": "1928374",
    "msg": "下单成功"
  }
}
```

#### Unconfirmed order response (`code == 2`):
Crucial safety safeguard for network jitter, verification popups, or delay in local order reflection:
```json
{
  "type": "reply",
  "id": "transaction-unique-id",
  "ok": false,
  "result": {
    "code": 2,
    "status": "unknown",
    "msg": "已提交但未能在未成交委托列表中匹配到对应订单，请自行人工或重试查询确认状态"
  },
  "error": "已提交但未确认，请勿重复下单，需人工或查询确认状态"
}
```
*Relays/gateways MUST preserve this detailed error text to prevent the AI from mistaking this as a trade failure and issuing a duplicated buy order.*

#### Ordinary failure response (`code == 1`):
```json
{
  "type": "reply",
  "id": "transaction-unique-id",
  "ok": false,
  "result": {
    "code": 1,
    "status": "failed",
    "msg": "可用资金不足"
  },
  "error": "可用资金不足"
}
```

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
