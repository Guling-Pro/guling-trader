# 股灵交易 (guling-trader) UI 规格：MCP 网关接入优化 + 市场链接

**制定日期：** 2026-05-23  
**涉及文件：** `src/trader/main_window.py`、`src/trader/tray.py`  
**优先级：** P1（改善用户入场和品牌露出）

---

## 一、现状分析

### 1.1 代码现状结构

#### `src/trader/main_window.py` 类 `MainWindow`

| 功能区域 | 函数/行号 | 当前实现 |
|---------|---------|--------|
| **状态条** | `_build_ui()` L167-195 | 左侧：状态圆点 + 中文标签 + 账户名；右侧：「解除绑定」/「复制 Token」按钮（仅 CONNECTED 可见）|
| **配对码区容器** | `_build_ui()` L197-208 | `pair_frame`（`fill="x"`）包含两个互斥视图：A（等待配对）/ B（刷新中） |
| **配对码等待视图** | `_build_pair_awaiting_view()` L254-297 | 黄底（`#fffbe6`），含顶行（黄点 + 倒计时）、中行（大字配对码 `pair_await_code_label`）、**底行：灰色提示文案「前往股灵pro聊天窗口输入配对码完成绑定」+ [复制] 按钮（L295-297）** |
| **配对码刷新中视图** | `_build_pair_refreshing_view()` L299-333 | 灰底（`#f0f0f0`），含刷新中提示；与视图 A 互斥 |
| **状态同步** | `_sync_state()` L393-456 | `root.after(100ms)` 周期拉 `SharedState` snapshot；配对码更新到 `pair_await_code_label` L448；倒计时计算 L450-453 |
| **日志区** | `_build_ui()` L236-243 | `log_frame`（LabelFrame），ScrolledText，`fill="both" expand=True` |
| **按钮区** | `_build_ui()` L245-252 | `btn_frame`，含两个按钮：「打开同花顺」+ 「退出」|
| **复制配对码方法** | `_copy_pairing_code()` L536-542 | 仅在配对等待按钮（L296）被调用；复制配对码本身到剪贴板；写日志 |

**关键发现：** `_copy_pairing_code()` 仅被底部提示按钮引用，无其他使用点，可安全删除或改造。

#### `src/trader/tray.py` 类 `TrayManager`

| 功能 | 函数/行号 | 当前菜单项 |
|------|---------|----------|
| **菜单定义** | `_run()` L116-128 | 1. 显示窗口（default）；2. —— separator；3. 配对码...；4. 连接状态；5. 打开 xiadan；6. —— separator；7. 退出 |

---

### 1.2 版本号管理

- **pyproject.toml**：`version = "0.4.12"` （L3）
- **src/trader/__init__.py**：`__version__ = "0.4.11"`（注意：与 pyproject.toml 不同步，实现者应注意统一）

对本 spec 无直接影响，但 footer 版本号若需要从代码读取，建议优先用 `pyproject.toml` 的值。

---

## 二、改动规格

### 改动 1：配对区指令文案替换

**位置：** `main_window.py` → `_build_pair_awaiting_view()` L254-297（现有视图 A 的底行）

**当前状态：**
- L289-293：灰色标签 `ttk.Label(..., text="前往股灵pro聊天窗口输入配对码完成绑定", ...)`
- L295-297：[复制] 按钮调用 `_copy_pairing_code()`

**改动内容：**

#### 2.1.1 移除旧文案和旧按钮

删除以下行：
- L289-293：旧提示文案标签
- L295-297：旧 [复制] 按钮定义

#### 2.1.2 新增指令区域

在 L285 （原 `bottom_row` Frame 创建处之后，新增以下结构：

```
底行容器（ bottom_row，bg="#fffbe6"，fill="x"）:
  ├─ caption_label（小字，灰色，`foreground="#666"`）
  │  └─ 文案：「复制发给你的 AI 助手（Claude / Cursor / codex / openclaw 等），它会自动帮你接入：」
  │
  ├─ instruction_row（新增水平容器，bg="#fffbe6"，fill="x"，pady=4）
  │  ├─ instruction_label（紧凑等宽字体如 Menlo/Consolas 10pt，foreground="#222"）
  │  │  └─ 动态文案：「打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 {CODE}」
  │  │     其中 {CODE} 绑定到当前 pair_await_code_label 的值（见下文）
  │  │
  │  └─ copy_btn（单个 ttk.Button，text="复制"）
  │     └─ command=self._copy_instruction_command
```

#### 2.1.3 新方法 `_copy_instruction_command()`

新增方法替代旧的 `_copy_pairing_code()`：

```python
def _copy_instruction_command(self) -> None:
    """复制完整指令到剪贴板"""
    snap = self.state.snapshot()
    code = snap.get("pairing_code", "")
    if not code:
        self.state.log("⚠ 配对码暂无，无法复制指令")
        return
    
    instruction = f"打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 {code}"
    self.root.clipboard_clear()
    self.root.clipboard_append(instruction)
    self.state.log(f"已复制接入指令到剪贴板：{instruction}")
```

#### 2.1.4 动态更新逻辑

修改 `_sync_state()` L448-456（配对码更新的块）：

当 `pair_await_code_label` 更新配对码时，**同步更新 `instruction_label` 的文本**：

```python
# 既有逻辑（L448-449）
if snap["pairing_code"]:
    self.pair_await_code_label.config(text=snap["pairing_code"])
    
    # 新增：同步更新指令
    code = snap["pairing_code"]
    instruction_text = f"打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 {code}"
    self.instruction_label.config(text=instruction_text)
```

#### 2.1.5 URL 可点击（可选，无强制）

指令文案中的 URL `https://mcp.guling.pro` 可通过以下方案实现点击打开（优先级：Nice-to-have）：

**Option A（推荐）：** 在 caption 和 instruction 之间新增一行小字链接标签，或直接让 instruction 的 URL 部分可点击（需自定义 Text 或 Label 绑定事件）。

**Option B（简单）：** 保持 instruction_label 为纯文本；用户复制后黏贴到聊天框，AI 会打开链接。

当前 spec 倾向 **Option B**（保持简洁），但实现时可参考 `_show_download_info()` L592-612 中的模式（弹窗+复制 URL）。

---

### 改动 2：主窗口底部 Footer

**位置：** `main_window.py` → `_build_ui()` L245-252（`btn_frame` 下方，新增）

**新增内容：**

在 `btn_frame` 的 `.pack()` 之后新增：

```python
# ---- Footer ----
footer_frame = ttk.Frame(self.root, padding=(12, 4, 12, 6))
footer_frame.pack(fill="x")

# 左侧：版本号标签（可选）
version_label = ttk.Label(footer_frame, text="股灵交易助手", foreground="#999", font=("Helvetica", 9))
version_label.pack(side="left")

# 右侧：可点击官网链接
# 使用 ttk.Label 或自定义链接（此处用 Label + 鼠标绑定）
website_link = ttk.Label(footer_frame, text="股灵 guling.pro ↗", foreground="#4a90e2", cursor="hand2")
website_link.pack(side="right")
website_link.bind("<Button-1>", self._on_footer_link_click)
```

**新方法 `_on_footer_link_click()`：**

```python
def _on_footer_link_click(self, event=None) -> None:
    """Footer 中的官网链接点击处理"""
    import webbrowser
    webbrowser.open("https://guling.pro")
    self.state.log("打开官网：https://guling.pro")
```

**UI 设计建议：**

- **颜色：** 灰色（`foreground="#999"`）低调，链接部分蓝色（`foreground="#4a90e2"`）；或统一灰色，鼠标 hover 时变蓝
- **字体：** 9pt 左右，比主内容小
- **分隔：** footer 可用细线（`ttk.Separator` 或 1px Frame）与上方按钮区分隔
- **内容：** 左侧版本号简化为「股灵交易助手」或「v0.4.12」；右侧官网链接 `股灵 guling.pro ↗`（向右箭头表示外链）

**版本号来源优先级：**

1. 如需动态读取，优先用 pyproject.toml（L3：`0.4.12`）
2. 或直接硬编码字符串「股灵交易助手」（spec 推荐，避免版本号不同步问题）
3. 若用 `__init__.py` 的 `__version__`，需先统一至 pyproject.toml

---

### 改动 3：Tray 菜单新增官网项

**位置：** `tray.py` → `_run()` L116-128（菜单定义）

**当前菜单顺序：**

```
1. 显示窗口 [default]
2. ——
3. 配对码...
4. 连接状态
5. 打开 xiadan
6. ——
7. 退出
```

**改动：** 在 5 和 6 之间（即「打开 xiadan」和「——」之间）插入新菜单项：

```python
pystray.Menu(
    pystray.MenuItem("显示窗口", self._on_show_window_menu, default=True),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("配对码...", self._on_show_pairing_code),
    pystray.MenuItem("连接状态", self._on_show_status),
    pystray.MenuItem("打开 xiadan", self._on_open_xiadan),
    pystray.MenuItem("访问 股灵官网", self._on_visit_website),  # ← 新增
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("退出", self._on_exit),
)
```

**新方法 `_on_visit_website()`（加在 L162 之后）：**

```python
def _on_visit_website(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
    """菜单：访问官网"""
    import webbrowser
    webbrowser.open("https://guling.pro")
```

---

## 三、验收标准 / Sprint Contract

### 改动 1 验收标准

#### 主检查项

- [ ] 配对等待视图底行：旧文案「前往股灵pro聊天窗口...」已删除，旧 [复制] 按钮已删除
- [ ] 新增 caption 小字：「复制发给你的 AI 助手（Claude / Cursor / codex / openclaw 等），它会自动帮你接入：」显示无误
- [ ] 新增指令行：格式为「打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 {CODE}」，配对码为当前实时值（如 482-739）
- [ ] [复制] 按钮可点击，复制**整句指令**（含当前配对码）到剪贴板
- [ ] 日志记录：复制成功后在日志区输出「已复制接入指令到剪贴板：...」

#### 边界情况检查

- [ ] **配对码刷新：** 当 `SharedState.pairing_code` 更新时（如 5 分钟过期，服务端生成新码），指令行同步更新（不需要用户重新操作）；验证方法：启动应用在配对等待状态，通过日志观察配对码刷新时指令是否同步
- [ ] **配对码暂无：** 若应用初始化时或配对码过期期间，`pairing_code` 为空，[复制] 按钮点击后应输出日志「⚠ 配对码暂无，无法复制指令」，且不改变剪贴板
- [ ] **已连接状态：** 应用连接成功（`connection_state == "CONNECTED"`）后，整个配对区（包括指令行）应隐藏，验证 `pair_frame.pack_forget()` 正常工作（现有逻辑 L420-423）
- [ ] **非 Windows 环境：** 在 macOS/Linux（wine 下）运行，指令行显示正常，[复制] 按钮功能正常

#### 不可妥协项

- 指令内容必须**字节级相同**：「打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 {CODE}」（中文空格、标点符号准确）
- 配对码必须**动态更新**，不可硬编码
- 不能破坏现有 `_sync_state()` 周期同步机制

---

### 改动 2 验收标准

#### 主检查项

- [ ] 新 footer 在主窗口最下方，位于 `btn_frame`（打开同花顺 / 退出）之下，日志区之外
- [ ] footer 左侧显示「股灵交易助手」（或版本号，待定），灰色（`#999`）低调
- [ ] footer 右侧显示「股灵 guling.pro ↗」，蓝色可链接（`#4a90e2`），鼠标悬停显示手型光标 `cursor="hand2"`
- [ ] 点击右侧链接，系统默认浏览器打开 https://guling.pro；日志记录「打开官网：https://guling.pro」
- [ ] footer 与上方按钮区有轻微视觉分隔（可用 `pady` 或细线）

#### 边界情况检查

- [ ] **窗口最小化/恢复：** footer 显示始终正常，不因窗口尺寸变化而隐藏或错位
- [ ] **不同状态下：** footer 在 UNPAIRED、CONNECTED、FATAL 等各连接状态下始终可见
- [ ] **非 Windows 环境：** 在 macOS/Linux（wine）下，`webbrowser.open()` 正常调用（系统可能跳转到 wine 内浏览器或主机浏览器，属于 OS 行为，不算问题）
- [ ] **屏幕尺寸：** 在较小窗口（最小 420x400，见 L150）下，footer 链接文本不被截断

#### 不可妥协项

- footer 不能破坏现有日志区的 `fill="both" expand=True` 布局（日志应保留足够高度）
- footer 必须在 `btn_frame` 之**下方**（pack 顺序或视觉层级）
- 点击链接必须调用 `webbrowser.open()`，确保跨平台行为一致

---

### 改动 3 验收标准

#### 主检查项

- [ ] tray 菜单项目顺序：显示窗口 / —— / 配对码 / 连接状态 / **打开 xiadan / 访问 股灵官网 / ——** / 退出（新项在「打开 xiadan」下）
- [ ] 「访问 股灵官网」菜单项可点击
- [ ] 点击后系统默认浏览器打开 https://guling.pro

#### 边界情况检查

- [ ] **非 Windows 环境：** tray 菜单初始化不报错（现有逻辑 L108-110 已处理，`platform.system() != "Windows"` 时 return），新方法中 `import webbrowser` 不破坏此逻辑
- [ ] **连接状态切换：** 点击菜单项时，应用处于任何连接状态（UNPAIRED、CONNECTED 等），菜单项都可用且行为一致

#### 不可妥协项

- 菜单项文案必须为「访问 股灵官网」（注意中文空格）
- 链接 URL 必须为 https://guling.pro（与 footer 保持一致）

---

## 四、涉及的文件和函数清单

**给实现者的快速参考：**

### src/trader/main_window.py

| 操作 | 函数/行 | 修改内容 |
|------|--------|--------|
| **删除** | `_build_pair_awaiting_view()` L289-297 | 删除底行提示标签和旧 [复制] 按钮 |
| **新增** | `_build_pair_awaiting_view()` 内，bottom_row 后 | 新增 caption 标签、instruction_row、instruction_label、新 [复制] 按钮；绑定到 `_copy_instruction_command()` |
| **新增方法** | `_copy_instruction_command()` 无具体行号 | 新方法，复制完整指令到剪贴板 |
| **删除方法（可选）** | `_copy_pairing_code()` L536-542 | 若无其他引用可删除（已确认仅 L296 引用，新改动后可删） |
| **修改** | `_sync_state()` L448-456 | 配对码更新块内新增 `instruction_label.config(text=...)` 同步逻辑 |
| **新增** | `_build_ui()` L252 之后 | 新增 footer_frame + version/website labels + 绑定事件 |
| **新增方法** | `_on_footer_link_click()` 无具体行号 | 新方法，点击官网链接，调 `webbrowser.open("https://guling.pro")` |

### src/trader/tray.py

| 操作 | 函数/行 | 修改内容 |
|------|--------|--------|
| **修改** | `_run()` L116-128 | 菜单 pystray.Menu 定义中，「打开 xiadan」和第二个 SEPARATOR 之间插入新菜单项 `pystray.MenuItem("访问 股灵官网", self._on_visit_website)` |
| **新增方法** | `_on_visit_website()` 无具体行号 | 新方法，`import webbrowser; webbrowser.open("https://guling.pro")` |

---

## 五、实现注意事项

### 5.1 配对码的动态更新机制

- `SharedState.pairing_code` 由后台 asyncio 线程更新（WebSocket 握手时从服务端获取）
- `_sync_state()` 每 100ms 被调用一次（L391），拉 snapshot 更新 UI
- 新指令行必须跟随配对码刷新**自动更新**，**不能依赖用户手动操作**

**实现建议：** 在 L448-456 的既有配对码更新块内直接同步更新 `instruction_label`，无需额外的 state flag。

### 5.2 文案准确性

指令文案是给 AI 助手（Claude / Cursor 等）解析的，**字符级别必须准确**：

```
打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 {CODE}
```

特别注意：
- 「打开」和「https」之间有一个**空格**
- 「mcp.guling.pro」后面有**空格**「帮我」
- 「接入」和「股灵」之间**无空格**
- 「交易」和「配对码」之间有**逗号+空格**

### 5.3 UI 布局兼容性

- 新 instruction_label 使用等宽字体（Menlo / Consolas）以对齐 URL
- 新 footer 不能挤压日志区高度（日志 `expand=True` 必须保留）
- 在 420x400 最小窗口下，指令行和 footer 应该不被截断（可能需要自动换行或滚动）

### 5.4 跨平台 webbrowser 行为

Python 标准库 `webbrowser` 在各平台行为：
- **Windows：** 调用系统默认浏览器（如 Chrome、Edge）
- **macOS：** 调用 Safari 或 默认浏览器
- **Linux/wine：** 调用 xdg-open 或 wine 内的浏览器

无需特殊处理，`webbrowser.open()` 自动适配；若链接打不开是用户环境问题（如无默认浏览器），属于操作系统约束。

### 5.5 日志记录规范

指令复制、footer 链接点击都应在日志区输出：

```python
self.state.log("已复制接入指令到剪贴板：打开 https://mcp.guling.pro 帮我接入股灵交易，配对码 XXX")
self.state.log("打开官网：https://guling.pro")
```

格式：`self.state.log(msg)`（自动添加时间戳 `[HH:MM:SS]`）

---

## 六、总结

| 改动 | 影响范围 | 风险等级 | 关键依赖 |
|------|--------|--------|--------|
| **改动 1：指令文案替换** | 配对等待视图底部；影响 UI 布局（同样 20px 高度） | 低 | 无；仅改动视图内容 |
| **改动 2：Footer** | 主窗口最底部；不影响既有控件 | 低 | 无；新增 frame 和事件处理 |
| **改动 3：Tray 菜单** | Windows tray 菜单；不影响 tkinter 主窗口 | 极低 | 无；仅菜单定义扩展 |

**总体风险：** 极低（全为新增或低耦合替换）

**推荐实现顺序：**
1. 改动 1（配对指令）— 影响核心业务流程，优先验证
2. 改动 2（footer）— UI 增强，无依赖
3. 改动 3（tray 菜单）— 最独立，可并行实现

