# 同花顺下单客户端（xiadan）控件架构与自动化策略

本文档记录 `guling-trader` 自动化的目标——同花顺**独立委托下单客户端**
（`xiadan.exe`，窗口标题 `网上股票交易系统5.0`）——的控件架构、菜单布局，以及
读取/导航所采用的健壮化策略。**当同花顺出新版本、换券商、或疑似控件对不上时**，
先跑 `python tools/ths_diag.py` 把实际结构 dump 出来，对照本文排查。

> 相关代码集中在 `src/trader/ths/win.py`；控件 ID 常量在 `src/trader/ths/const.py`。

## 0. 关键前提

- **两套皮肤**：客户端右上角有「新版 / 旧版」切换按钮，切换后**同一个 `xiadan.exe`
  进程内换皮肤**（不是两个程序）。两套皮肤的**窗口标题、控件 ID、类名结构基本一致**，
  差异主要是外观与个别时序（大查询在旧版更容易触发超时框）。本项目对两版都验证通过。
- **xiadan 是 32 位进程**。这台开发/运行机若用 **64 位 Python**，跨进程读 TreeView
  文字时结构指针大小必须匹配（见 §4），否则读空——历史上"树文字读不到"的真因。
- **数据提取靠剪贴板**：表格控件是同花顺自绘的 `CVirtualGridCtrl`，无公开读单元格的
  消息 API，`Ctrl+C` 拷贝是行业标准提取路径。本项目把它降级为"毫秒级中转"（见 §5）。

## 1. 控件骨架（类名链）

```
xiadan.exe 顶层窗口「网上股票交易系统5.0[ - 券商后缀]」   ← FindWindow / 标题前缀匹配
└─ AfxMDIFrame<MFC版本>s                                  ← 前缀匹配，不锁版本号
   ├─[GetDlgItem 0xE901]→ 右侧面板容器                    ← get_right_hwnd()
   │    └─（新版皮肤多套一层）→ 面板内控件（用递归查找，不假设精确层级）
   ├─ AfxWnd*s → HexinScrollWnd → AfxWnd*s → SysTreeView32 ← get_tree_hwnd()（左侧树菜单）
   └─ AfxWnd*s → CCustomTabCtrl                            ← get_left_bottom_tabs()（底部 股票/信用/港股通）
```

- `AfxMDIFrame140s` / `AfxWnd140s` 里的 `140` 是 **MFC 版本号（14.0 = VS2015）**。同花顺
  换工具链重编会变（如 `142s`）→ 精确匹配会断链。代码用 `_child_by_class_prefix`
  **按前缀 `AfxMDIFrame`/`AfxWnd` 匹配**，版本号无关。
- `HexinScrollWnd`（窗口名）/ `SysTreeView32` / `CCustomTabCtrl` / `0xE901` 名称稳定，精确匹配。

## 2. 左侧树菜单布局（SysTreeView32）

```
买入[F1] · 卖出[F2] · 撤单[F3]
自选股 └ 行情
条件单 └ 条件单监控/股价条件/止盈止损/涨停买入/涨跌幅条件/反弹买入/回落卖出/通用回购
新股申购 └ 新股申购/新股批量申购/新股配号/新股中签/新股申购额度查询
北交所交易 └ 北交所新股发行 └ 公开发行询价/申购/批量.../发行询价委托查询/...
双向委托 · 市价委托(└买入/卖出)
查询[F4] └ 资金股票·当日委托·当日成交·历史委托·历史成交·历史持仓·资金明细·对帐单·交割单·新股申购额度查询·账户分析
通用回购 · 新三板交易 · 银证转帐 · 场内基金 · ETF业务 · 修改密码 · 多银行存管
批量下单 · 其它交易 · 基金盘后业务 · 隔日证券预委托 · 盘后定价委托 · 修改联系信息 · 风险提示 · ...
```

顶层项自带快捷键标注（`买入[F1]`…`查询[F4]`）——热键是最稳的导航锚。

## 3. 控件 ID 表（实测，新旧版一致）

| 用途 | 控件 | ID |
|---|---|---|
| 资金字段（get_right_hwnd 下）| Static | 资金余额 `0x3F4`、可用 `0x3F8`、可取 `0x3F9`、市值 `0x3F6`、总资产 `0x3F7`、持仓盈亏 `0x403`、当日盈亏 `0x402`、当日盈亏比 `0x405`（见 `const.BALANCE_CONTROL_ID_GROUP`）|
| 表格（持仓/委托/成交/交割单）| `CVirtualGridCtrl` | `0x417`（每个面板各一张，靠**可见性**区分当前激活面板）|
| 下单表单 | Edit | 证券代码 `0x408` · 价格 `0x409` · 数量 `0x40A` |
| 批量撤单按钮 | Button | 全撤 `0x7531` · 撤买 `0x7532` · 撤卖 `0x7533` · 撤最后 `0x079A` |
| 交割单时段 | Button | 近一周 `0x14BC` · 近一月 `0x14BD` · 近三月 `0x14BE` · 近一年 `0x14BF` · 自定义 `0x14C5`（5 个面板各有副本，靠可见性取当前面板的）|

## 4. 32 / 64 位跨进程读 TreeView 文字

读 `SysTreeView32` 节点文字要发 `TVM_GETITEMW` 并传一个指向 `TVITEMW` 结构的**跨进程指针**。
xiadan 是 32 位，`TVITEMW` 的 `hItem/pszText/lParam` 是 4 字节；64 位 Python 的默认结构是
8 字节 → 发过去布局错位 → 消息失败（返回 0）、文字读空。

代码用 `_proc_is_wow64(pid)` 检测目标位数，`_TVITEMW`（64位）/ `_TVITEM32`（32位）二选一。
这是 `_select_tree_node_by_text` 能按标签定位树节点的前提。

## 5. 数据读取：剪贴板毫秒级中转 + 内存 state

`read_table_text(hwnd)`：清空剪贴板 → 取 `GetClipboardSequenceNumber` 基线 → `Ctrl+C` →
**确认序列号变化（本次拷贝真落定）** → 读文本 → **立刻清空**。序列号没变 = 拷贝没落定
（窗口没焦点/被弹窗挡），返回 `None` 让调用方重试，**绝不返回上一次遗留的陈旧表格**。

各查询结果落 `ThsState`（线程安全内存态，`state.get(key, max_age=)` 读 last-known）。
剪贴板里几乎不留数据，不受并发/同步干扰。

## 6. 两套导航原语（零脆弱依赖）

- **顶层面板** → 热键 `F1/F2/F3/F4`（菜单自带 accelerator，最稳）。
- **查询子面板**：
  - 持仓/委托/成交 → 热键组合（F1+F6 / F1+F8 / F2+F7）+ 递归找可见表格。
  - 交割单 → `_select_tree_node_by_text("交割单")` **按标签选中树节点**（免疫菜单重排，
    取代已废弃的"数 8 次 Down 键"走位）。
- **控件访问** → `_find_ctrl_by_id(root, id, cls, visible=True)` **递归查找**（不假设精确
  层级，兼容新版多套一层）；表格用 `_find_grid`（优先可见的 `CVirtualGridCtrl`）。
- **时段切换** → 按控件 ID（`0x14BC~0x14BF`）+ 可见性点击。

## 7. 已内建的健壮性

| 机制 | 应对 |
|---|---|
| `_ensure_bound` 用 `IsWindow` + 标题前缀校验句柄活性 | xiadan 重启/重登/切皮肤 → 句柄失效（错误1400），自动重绑新窗口 |
| `_child_by_class_prefix` 前缀匹配类名 | MFC 版本号变化（140s→142s）断链 |
| `_find_ctrl_by_id` 递归 + `GetDlgItem` 弃用 | 新版皮肤给面板多套一层容器 → `GetDlgItem` 报 1421 |
| `visible=True` 过滤 | 多面板同 ID 控件抓错（orders 误读成 position）|
| 位数感知 `TVITEM` | 64位Python vs 32位xiadan 读树文字读空 |
| 序列号确认 + 读后即清空 | 剪贴板陈旧数据 / 并发冲突 |
| 交割单大查询重点重试 + 回车关超时框 | 近一年等大查询首次「查询超时」、表格读空 |

## 7.5 自选股（watchlist）——截图 + OCR（新版专有）

自选股在**新版 xiadan** 里是**内嵌 CEF(Chromium) 渲染的网页**，不是原生表格：

- 原生控件读不到（整窗只有 2 张 CVirtualGridCtrl，都是持仓相关，无自选股 grid）；
- CEF **没开 remote-debugging 端口**（探测 xiadan 及子进程 HxExternal 只有一个私有 IPC 端口），CDP 注入走不通；
- 本地 `SelfStockInfo.json` 由**行情主应用**写，行情不常开 → 常过期数天、成分都旧；
- **旧版 xiadan 没有自选股菜单**。

所以唯一实时途径是**截图 + OCR**（`ths/win.get_watchlist`）：

1. `_select_tree_node_by_text("自选股")` 导航到自选股节点；
2. `_capture_window_png(hwnd_main)`：**DPI 感知（Per-Monitor-V2）的 `PrintWindow(PW_RENDERFULLCONTENT)`**
   截整个窗口——能截到内嵌 CEF 的网页内容（`BitBlt` 会黑屏），2x 屏（Parallels/Retina）不截半张；
3. `_ocr_leftmost_codes`：`pytesseract.image_to_data` 拿词框，**只取 x 最小那一簇 6 位数字 = 代码列**，
   天然排除左侧菜单与右侧数字列（主力净额/总金额的 6 位数假阳性）；
4. Tesseract 路径复用 `installer.tesseract.detect_tesseract()`（与验证码识别同一个）。

限制：CEF 分页，`WM_MOUSEWHEEL` 滚不动，只读**第一屏（顶部）**，返回 `partial=true`。
但**同花顺习惯：新加入的自选股出现在顶部**，所以只比顶部即可捕捉"新增"。

**看门狗**（`watchlist_watch.py`）：定点整点（默认 8/12/16/20，避开交易时段）同步顶部
→ 与上次 diff → 有变化经 WS 推 `watchlist_event`（含 `added`/`codes`/`partial`）。仿 `order_watch`。
配置：`enable_watchlist_watch` / `watchlist_sync_hours`。

## 7.6 市价委托路径（`buy`/`sell` 不传 price）

`buy`/`sell` 有两条路径，在 `_do_buy`/`_do_sell` 内按 `price` 分派：

- **传 `price`** → `_submit_trade("F1"/"F2", …)`：**限价挂单**（原逻辑），未成交留 `orders_active`，
  由 agent 用 `cancel` 管理。
- **不传 `price`（`None`）** → `_submit_market_trade(op, code, amount)`：走左树 **市价委托 └ 买入/卖出**
  面板，**委托策略固定「五档即成剩撤」**——扫对手方最优五档立即成交、剩余自动撤销、**无残留挂单**。

**为什么是五档即成剩撤**：市价 5 个策略里唯一同时满足"立即成交 + 剩余自动撤 + 沪深北全市场通用"。
「对手方最优」只吃一档且深市会转限价留单、**上交所主板根本没有**；FOK 太刚。详见
`docs/superpowers/specs/2026-07-04-ths-market-order-design.md`。

**面板与控件（原生控件，非 CEF；新旧皮肤一致，见 `const.MARKET_*`）**：

| 用途 | 控件 | ID |
|---|---|---|
| 证券代码 | Edit | `0x408`（与 F1/F2 同）|
| 数量 | Edit | `0x40A`（与 F1/F2 同）|
| 提交 | Button | `0x3EE` |
| 委托策略 | ComboBox | `0x605`（标准 `ComboBox`）|

**导航**：市价买入/卖出**无 F 快捷键**，且子节点文字"买入"/"卖出"与顶层"买入[F1]"前缀相同 →
用 `_select_tree_child("市价委托", "买入"/"卖出")`：**先定位父节点、再在其直接子节点里整串精确匹配**
（深度优先的 `_select_tree_node_by_text` 会先撞顶层，不能用）。跨进程 TreeView 读写/位数/DPI
点击与 `_select_tree_node_by_text` 同构。

**委托策略设置**：买卖下拉不同 → 用**键盘位置数字**切换（`_set_market_strategy`：`SetFocus` +
`AttachThreadInput` + `WM_CHAR`，`CB_GETCURSEL` 校验，未命中回退 `CB_SETCURSEL`）。
**买入**发 `"1"`（五档=index 0，已是默认）；**卖出**发 `"4"`（五档=index 3，默认 index 2=即成剩撤是
**深市专有、沪市会拒** → 卖出必须显式设，否则下错单）。设不中即中止，不硬下。

**回执**：五档即成剩撤下单后**几乎不留 `orders_active`**（全成→成交表；部分成→成交部分进成交表、
剩余被撤）→ 市价回执**查成交表 `orders_filled`**（不是委托表），下单前快照 `before` 基线、下单后轮询
差分（`_match_market_fill`）。可能**部分成交** → 回执带回真实 `filled_amount` / `avg_price`（按金额加权）
和 `status`（`filled`/`partially_filled`）。8s 内查不到成交 → `status:"unknown"` 并提示**可能非连续
竞价时段/涨跌停被拒/无成交**，绝不当成功。

## 8. 出新版本时怎么排查

1. 切到目标皮肤、登录 xiadan。
2. `python tools/ths_diag.py` → 得到骨架 + 菜单树（含位数、类名后缀、节点文字）。
3. 对照本文 §1/§2：类名后缀变了？前缀匹配已兜住。菜单结构变了？看树 dump。
4. 若某面板控件对不上：临时加一段 `EnumChildWindows` dump 该面板的 `(id, class, text,
   visible)`（历史上用过的 probe/dump 脚本模式），拿到新 ID 更新 `const.py`。
5. 所有读取走递归查找 + 可见性，通常控件 ID 稳定、无需改动。
