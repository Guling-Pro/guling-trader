# 同花顺市价委托路径接入设计

> 让 `buy`/`sell` 的"市价单"走同花顺**真·市价委托面板 + 五档即成剩撤**，
> 实现"不计价、立即成交、剩余自动撤销、无残留挂单"，与限价挂单路径分工清晰。

- 状态：设计已确认，待实现
- 影响文件：`src/trader/ths/win.py`（新路径）、`src/trader/dispatcher.py` +
  `docs/tools_schema.json`（工具描述）
- 相关背景：`docs/ths_architecture.md`（控件架构、导航原语）

## 1. 背景与问题

现在 `buy`/`sell` 不传 `price` 时走的是 **买入[F1]/卖出[F2] 限价面板**，把价格框留空，
让 xiadan 自动带出**对手价**填入价格框——本质上是**挂在卖一/买一的限价单**
（marketable limit），**不是**同花顺「市价委托」面板那种真·市价委托。

后果：遇到价格瞬间跳动、对手盘量不足时，这张"市价单"**照样会留一张未成交挂单**，
照样要 `orders_active` 查 + `cancel` 撤。它并没有兑现"市价单省掉查/撤单"的初衷。

真正能兑现的，是同花顺左侧树菜单里的 **市价委托 └ 买入/卖出** 面板：无价格框，
选「委托策略」下拉，其中 **五档即成剩撤** = 扫对手方最优五档立即成交、未成交部分
自动撤销、不留挂单。

## 2. 为什么选「五档即成剩撤」

同花顺市价委托下拉 5 个选项，逐一排除后只有五档即成剩撤同时满足
"立即成交 + 无残留 + 全市场通用"：

| 选项 | 吃几档 | 剩余处理 | 交易所支持 | 结论 |
|---|---|---|---|---|
| 1 对手方最优 | 只吃对手方最优**一档** | 深交所→**转限价挂单留存** | **上交所主板无此类型**；深/北有 | ✗ 会留单 + 沪市不可用 |
| 2 本方最优 | 挂本方价 | 大概率不成交挂着 | — | ✗ 不立即成交 |
| 3 即成剩撤(FAK) | 与对手方**全部**申报成交 | 剩余撤销 | 深交所 | △ 沪市支持形态不同 |
| 4 **五档即成剩撤** | 扫对手方**最优五档** | **剩余自动撤销** | **沪/深/科创/北交所都支持** | ✓ **选它** |
| 5 全额成交或撤(FOK) | — | 部分成交也全撤 | 深交所 | ✗ 太刚，不适合默认 |

来源：[深交所五种市价委托业务说明](https://docs.static.szse.cn/www/marketServices/technicalservice/history/W020180328468088728908.doc)、
[上交所科创板市价申报类型](https://xueqiu.com/3661631621/134484965)。

**路径分工定为：**
- **市价路径**（要立即成交、不纠结价格）→ 市价委托面板 + 五档即成剩撤，无残留、免查撤单
- **限价路径**（要指定价挂单）→ F1/F2 面板填价 → 保留 `orders_active` + `cancel` 兜底

## 3. 接口设计（方案 A：复用 `buy`/`sell`，以 `price` 有无区分语义）

```
buy(stock_no, amount, price=None) / sell(stock_no, amount, price=None)
    price is None  → 真·市价委托（五档即成剩撤，立即成交、无残留）
    price 有值      → F1/F2 限价挂单（原逻辑不变）
```

Agent 心智：**要立刻成交就别给价，要挂单就给价**。工具数量不变，`client_order_id`
形参保持透传。

## 4. 实现设计

### 4.1 前提：控件参数需在真机 dump

市价委托面板的以下参数现有代码**没有**，须先在 Windows 真机跑 `tools/ths_diag.py`
（必要时临时加 `EnumChildWindows` dump 该面板 `(id, class, text, visible)`）确认后写入
`const.py`，实现+联调只能在真机进行：

- **市价委托 └ 买入/卖出** 是树菜单子节点，**无热键**；子节点文字为"买入"/"卖出"，
  与顶层"买入[F1]"文字前缀相同 → 深度优先 walk 会先撞顶层节点。
  **须先定位「市价委托」父节点，再取其子节点**（见 4.3）。
- **委托策略 ComboBox** 的控件 ID + "五档即成剩撤"选项索引 —— 未知，dump。
- **证券代码 / 数量 Edit 的 ID** —— 预期仍为 `0x408`/`0x40A`，须确认；
  市价面板**无价格框 `0x409`**。

### 4.2 路径分派

`buy` → `_do_buy`，`sell` → `_do_sell` 内按 `price` 分流：

```
price is None → _submit_market_trade(child, op_keyword, stock_no, amount)
price 有值     → _submit_trade(panel_key, op_keyword, stock_no, amount, price)  # 原样
```

（`buy`/`sell` 顶层的 `_ensure_bound` + `asyncio.to_thread` 包装不变。）

### 4.3 新方法 `_submit_market_trade(child, op_keyword, stock_no, amount)`

参数 `child` ∈ {"买入","卖出"}（市价委托的子节点文字），`op_keyword` ∈ {"买入","卖出"}
（成交表操作列匹配用）。

流程：

1. `switch_to_normal()` + `_activate_window(self.hwnd_main)`。
2. **导航到市价委托子面板**：新增树导航小工具（见 4.4），选中「市价委托」父节点下的
   `child` 子节点，触发右侧面板切换。
3. **设委托策略**：定位委托策略 ComboBox，置为「五档即成剩撤」。**每次都显式设置**，
   不依赖上次残留；设置后确保 xiadan 收到选择变化（`CB_SETCURSEL` + 必要的
   `WM_COMMAND/CBN_SELCHANGE` 或键盘选择，真机验证哪种稳）。
4. **填表**：`_find_input(hwnd, 0x408)` 写代码、`_find_input(hwnd, 0x40A)` 写数量。
   **不碰价格框**。
5. **提交**：`hot_key(["enter"])`（表单→确认框）→ `hot_key(["enter"])`（确认→提交）
   → `input_ocr()`（复用反机器人验证码流程，无弹窗即返回）→ `hot_key(["enter"])`
   （关结果弹窗）。
6. **回执**：见 4.5。

### 4.4 树导航小工具（父节点 → 子节点）

现有 `_select_tree_node_by_text` 深度优先返回**首个**子串命中，无法区分市价委托的
"买入"/"卖出" 与顶层 "买入[F1]"。新增能力：

- 定位「市价委托」父节点句柄 → `TVM_GETNEXTITEM(TVGN_CHILD)` 取首子 →
  沿 `TVGN_NEXT` 遍历子节点，按 `child` 文字精确匹配 → 对该子节点做
  `TVM_SELECTITEM` + 真实鼠标点击（复用现有 rect→屏幕坐标→点击 + Per-Monitor-V2 DPI 换算）。
- 实现方式：给 `_select_tree_node_by_text` 加一个"限定在某父节点子树内 / 只在直接子节点里精确匹配"
  的参数，或抽一个 `_select_tree_child(parent_text, child_text)`。真机联调时择稳者。

### 4.5 市价单回执（查成交表，非未成交表）

五档即成剩撤"立即成交、剩余立即撤销"，下单后**几乎不留在 `orders_active`**，
故**不能**复用 `_lookup_entrust_no`（它查 active）。市价单改查 **`orders_filled`（成交）**：

- 轮询 `get_filled_orders()`，按 `证券代码 == stock_no` + `op_keyword in 操作` 匹配
  本次新增的成交行（可用"下单前记录成交表已有行 → 下单后取新增"避免撞历史成交；
  或按时间/合同编号取最新，真机验证字段名）。
- **五档即成剩撤可能部分成交**（五档量不足时），回执**必须带回真实成交数量与均价**：
  ```
  {code:0, status:"filled"/"partially_filled", stock_no, filled_amount, avg_price,
   requested_amount, op}
  ```
- 超时未在成交表匹配到、且 active 也无 → `{code:2, status:"unknown", msg:"已提交但未能确认成交，请自行核对成交/委托"}`。

（成交表字段名——成交数量/成交均价/操作——以 `get_filled_orders()` 实际返回为准，真机确认。）

### 4.6 工具描述改写

`src/trader/dispatcher.py` 的内嵌 schema 与 `docs/tools_schema.json` 同步改：

- `buy`/`sell` 顶层 `description`：点明两条路径——
  "不传 `price` = 五档即成剩撤市价单（立即成交、剩余自动撤销、**无残留挂单**，回执带实际成交量/均价）；
  传 `price` = 限价挂单（未成交会留单，需自行用 `orders_active` + `cancel` 管理）"。
- `price` 字段 `description` 同步为上述语义（替换现有"按同花顺客户端对手价市价单执行"的旧描述）。

## 5. 兼容性与风险

- **语义变更**：`price=None` 的行为从"F1 对手价限价单"变为"市价委托五档即成剩撤"。
  调用方若曾依赖旧的"挂在对手价的限价单会留在 active"，行为会变（市价单不再留 active）。
  这是本设计的**预期改进**，非回归。工具描述须清楚说明，避免 agent 误判。
- **回执时序**：市价单成交极快，但成交表刷新有毫秒级延迟 → 轮询要给足超时（参考现有
  `_lookup_entrust_no` 的 8s）。
- **验证码**：市价委托提交同样可能弹反机器人验证码，`input_ocr()` 直接复用。
- **真机依赖**：控件 ID / ComboBox 选择方式 / 树子节点导航 / 成交表字段名，全部须在
  Windows + xiadan 真机确认后才能定稿实现；本 spec 只锁定架构与语义。

## 6. 测试

- **单元/桩测**（macOS 可跑）：路径分派逻辑（`price` 有无 → 调 `_submit_trade` vs
  `_submit_market_trade`，用 mock 断言分派正确）；回执组装（给定 mock 成交表 → 断言
  `filled_amount`/`avg_price`/`status` 正确，覆盖全成 / 部分成 / 未匹配三态）。
- **真机联调**（Windows）：市价买/卖各一手小额实单，验证——面板导航命中、委托策略确为
  五档即成剩撤、代码/数量正确、验证码处理、回执成交量/均价与客户端一致、active 表无残留。
- 现有 `tests/test_market_price.py`、`tests/test_order_watch*.py` 回归。
