# guling-trader v3 架构与界面对齐方案

## 1. 状态机定义

### 5 个 UI 状态及 SharedState 字段映射

v3 设计稿定义的 5 个用户可见状态：

| UI 状态标签 | 驱动字段组合 | 新增/修改字段 |
|-----------|-----------|------------|
| ① 连接中 | `connection_state="DIALING"` + `pairing_code=None` | - |
| ② 等待配对 | `connection_state="AWAITING_BIND"` + `pairing_code="XXX-YYY"` + `pairing_expires_at > now` | - |
| ③ 到期·自动刷新 | `connection_state="AWAITING_BIND"` + `pairing_code="XXX-YYY"` + `pairing_expires_at <= now` + `ths_refreshing=True` | **新增** `ths_refreshing: bool = False` |
| ④ 已连接·THS引导 | `connection_state="CONNECTED"` + `ths_steps_complete < 4` | **新增** `ths_steps_complete: int = 0`<br>**新增** `ths_expanded: bool = True` |
| ⑤ 全部就绪 | `connection_state="CONNECTED"` + `ths_steps_complete == 4` | 同上；`ths_expanded=False` |

### SharedState 完整字段清单

**现有字段** (保持不变)：
- `connection_state: str = "UNPAIRED"` — WS 连接状态（UNPAIRED / DIALING / AWAITING_BIND / CONNECTED / DISCONNECTED）
- `account_name: str = ""` — 来自 bind_ok 帧的账户标识
- `pairing_code: Optional[str] = None` — 当前配对码
- `pairing_expires_at: Optional[float] = None` — 配对码过期时间戳（unix timestamp）
- `xiadan_path: Optional[str] = None` — xiadan.exe 完整路径
- `last_pong_at: Optional[float] = None` — 最后一次心跳时间
- `fatal_reason: Optional[str] = None` — 致命错误描述
- `install_progress: Optional[tuple[int, int]] = None` — 安装进度 (bytes_done, bytes_total)
- `log_messages: queue.Queue` — 日志队列
- `_lock: threading.Lock` — 线程安全锁

**新增字段** (v3 实现):
```python
ths_steps_complete: int = 0  # [0..4] 已完成的 THS 4 步数
ths_expanded: bool = True     # THS 区是否展开显示 4 步；False 时显示折叠行
ths_refreshing: bool = False  # 配对码到期，自动刷新中
```

### 线程安全处理

所有字段修改通过 `state.update(**kwargs)` 调用，内部持有 `_lock`。读取通过 `state.snapshot()` 返回副本。当前实现已充分——无需新增 lock。

---

## 2. THS 4 步检测协议

### 检测函数与来源

| 步骤 | UI 标签 | 检测内容 | 检测函数 | 实现文件 | 可用性 |
|------|--------|--------|---------|--------|------|
| 1 | 同花顺行情 | hexin.exe 进程存活 | `detect.find_via_process("hexin.exe")` / psutil | `src/trader/installer/detect.py` | ✓ 现有 |
| 2 | 委托窗口 | xiadan.exe 进程存活 | `detect.find_via_process("xiadan.exe")` / psutil | `src/trader/installer/detect.py` | ✓ 现有 |
| 3 | 旧版界面 | 窗口标题包含"网上股票交易系统5.0" | `bootstrap._detect_xiadan_window("网上股票交易系统5.0")` / Win32 EnumWindows | `src/trader/bootstrap.py` | ✓ 现有 |
| 4 | xiadan 就绪 | 读 xiadan.exe 可执行路径（验证真实可用） | `psutil.Process(pid).exe()` 或 `GetModuleFileNameEx(handle)` | `src/trader/bootstrap.py` | ✓ 现有 |

### Polling 任务架构

**推荐方案**：asyncio 周期任务 + 每步超时 + exception safe

**理由**：
- tkinter 主线程的 `after(2000, poll)` 不可靠（频繁堵塞，间隔不稳定）
- asyncio loop 已在后台运行（ws_client），纳入 asyncio 任务复用基础设施
- 与 ws_client 生命周期绑定：重连时自动重启 poll

**实现位置**：`src/trader/main.py::_async_main` 中 WS 启动前新增独立 task

```
asyncio 后台任务：
  - 每 2 秒轮询一次（asyncio.sleep(2)）
  - 按步骤 1→2→3→4 顺序检测
  - 若第 n 步失败，后续步骤标记为未完成
  - 若第 n 步成功，`ths_steps_complete = n`；继续检测 n+1
  - 任何 exception 都 log + 继续（不能炸 loop）
  - 在 connection_state == CONNECTED 时启动，DISCONNECTED 时暂停
```

**错误处理**：
```python
try:
    result = check_step_n(...)
    if result:
        state.update(ths_steps_complete=n)
    else:
        state.update(ths_steps_complete=max(0, n-1))
except Exception as e:
    state.log(f"步骤 {n} 检测异常: {e}")
    # 不重新抛出，继续轮询
```

---

## 3. 配对码自动刷新流程

### 时序与触发

**检测**：`main.py::_sync_poll_task` 或 `main_window::_sync_state` 中每 100ms 检查一次
```python
if pairing_expires_at and time.time() >= pairing_expires_at:
    # 过期标志
    trigger_refresh()
```

**刷新操作**：
1. `state.update(ths_refreshing=True)` — UI 显示"正在获取新配对码..."
2. 调用 `ws_client.send_frame({"type": "pair_init", ...})` 或直接 `ws.close()` 让 reconnect 自动触发 pair_init
   - **选择 ws.close()**：更清晰，遵循既有"重新配对"流程
3. server 返回 `pair_pending` → 调用 `on_pair_pending(code, expires_at)` → `state.update(pairing_code=..., pairing_expires_at=..., ths_refreshing=False)`

### 配对码清理

过期码不清理（UI 会显示删除线），等新码到来后覆盖。

### 用户提示

无需额外提示。UI 自动显示"正在获取新配对码..."spinner，用户输入新码后自动绑定（server 端逻辑）。

---

## 4. THS 区折叠/展开规则

### 展开触发

- 状态 ①②③④（AWAITING_BIND / DIALING / AWAITING_BIND + 到期）：**必须展开**，显示 4 步进度
- 状态 ④（CONNECTED + ths_steps_complete < 4）：**必须展开**，继续显示未完成步骤
- 状态 ①③ 进入时：自动 `state.update(ths_expanded=True)` 

### 折叠触发

- 状态 ⑤（ths_steps_complete == 4）完成时：自动 `state.update(ths_expanded=False)` → 显示单行"✓ C:\...xiadan.exe"
- 4 步轮询仍在后台继续（监听进程断连，若 xiadan.exe 消失则回到状态 ④）

### 重新展开

若 `ths_expanded=False` 且轮询发现 `ths_steps_complete < 4`（进程断连）：
```python
state.update(ths_expanded=True)
state.log(f"xiadan 进程消失，重新启动引导")
```

### UI 表现

- **展开**：`<div class="ths-card"><ul class="steps">...4 行...</ul></div>`
- **折叠**：`<div class="ths-card"><div class="ths-done">✓ 路径... <button>更换</button></div></div>`

驱动字段：`ths_expanded` boolean

---

## 5. UI 组件改动清单

### `src/trader/main_window.py`

| 行号范围 | 改动点 | 一句话原因 |
|---------|------|---------|
| ~27-58 | SharedState 新增 3 字段 | 驱动 THS 状态机和自动刷新 |
| ~126-222 | 配对码区重构（pair_frame 内部）| 实现v3设计的大字+黄底+倒计时+复制按钮 |
| ~145-200 | 新增"到期刷新"视图分支 | 显示旧码删除线 + spinner "正在获取新配对码..." |
| ~244-267 | 配对码过期判断逻辑 | 区分 AWAITING_BIND 状态和已过期状态 |
| ~268-290 | 账户名及"解除绑定"按钮| 已连接时显示账户名旁边的"解除绑定"按钮（删除确认） |
| ~新增 | THS 卡片展开/折叠切换逻辑 | 根据 ths_expanded / ths_steps_complete 动态显示 |
| ~新增 | THS 步骤进度同步 | 每 100ms 检查 ths_steps_complete，更新 UI 中的 ✓/⏳/○ 状态 |

### `src/trader/main.py`

| 行号范围 | 改动点 | 一句话原因 |
|---------|------|---------|
| ~133-223 | _async_main() 新增 THS polling task | 定时检测 4 步，更新 state.ths_steps_complete |
| ~178-204 | on_pair_pending() 增强 | 自动刷新时也要调用此回调更新 state |
| ~324-336 | on_open_xiadan() 保持不变 | 已用 os.startfile() 解决启动方式 |
| ~375-393 | on_reset_pair() 增强 | 清除时同时重置 ths_steps_complete / ths_expanded |

### `src/trader/ws_client.py`

| 行号范围 | 改动点 | 一句话原因 |
|---------|------|---------|
| ~200-235 | _handle_frame() pair_pending 分支 | 过期刷新触发重连时，新的 pair_pending 帧也要更新 pairing_code + pairing_expires_at |

### `src/trader/bootstrap.py` 与 `src/trader/installer/detect.py`

无需改动。现有检测函数足够复用。

### `src/trader/dispatcher.py`

无需改动。7 个 method 白名单保持。

### `src/trader/config.py`

无需改动。config 字段不新增——状态全在 SharedState 里。

---

## 6. 任务拆分建议

### Task A：SharedState 新增字段 + _sync_state() 同步逻辑
- **目标**：配对码到期判断、THS 展开/折叠、自动刷新 spinner
- **文件所有权**：`main_window.py` 
- **依赖**：无
- **验收标准**：
  - [ ] UI 能显示"已过期"状态（红色）+ spinner
  - [ ] 手动更新 state 后 UI 立即响应
  - [ ] 没有 tkinter 错误输出

### Task B：THS 4 步轮询 asyncio 任务 + exception handling
- **目标**：后台定时检测 4 步，安全更新 ths_steps_complete
- **文件所有权**：`main.py` (新增 polling coroutine)
- **依赖**：Task A （需要 ths_steps_complete 字段）
- **验收标准**：
  - [ ] 轮询任务在 _async_main() 中启动
  - [ ] 每步超时/异常不会炸 loop（log only）
  - [ ] 步骤检测到时 state 更新及时（手动 mock 检测函数验证）
  - [ ] connection_state 变为 DISCONNECTED 时轮询自动暂停

### Task C：配对码自动刷新流程（timeout 触发 + ws.close()）
- **目标**：检测过期 → ws.close() → 重连 → 新 pair_init → 新 pair_pending
- **文件所有权**：`main.py` (on_pair_pending) + `ws_client.py` (_handle_frame pair_pending)
- **依赖**：Task A, B （需要检测逻辑和字段更新）
- **验收标准**：
  - [ ] 手动改 pairing_expires_at 为过期时间，verify ws.close() 被调用
  - [ ] 新的 pair_pending 帧到来后，UI 显示新配对码
  - [ ] 自动刷新期间 UI 显示 "正在获取新配对码..." spinner

### Task D：THS 4 步 UI 展开/折叠 + "更换"按钮
- **目标**：展开（4 步进度）/ 折叠（✓ 路径 + 更换按钮）动态切换
- **文件所有权**：`main_window.py` (_build_ui / _sync_state)
- **依赖**：Task B （需要 ths_steps_complete 准确）
- **验收标准**：
  - [ ] ths_steps_complete==4 时自动折叠，显示单行 + 更换按钮
  - [ ] 若 xiadan 进程消失（ths_steps_complete 回退），自动重新展开
  - [ ] "更换"按钮打开文件对话框，手动指定新 xiadan.exe 路径
  - [ ] 没有闪烁（展开/折叠频繁切换）

### Task E：集成测试 + 烟测
- **目标**：模拟 5 个状态流转，验证 UI / 日志 / state 一致性
- **文件所有权**：`tests/` (新增 test_v3_state_flow.py 等)
- **依赖**：Task A~D
- **验收标准**：
  - [ ] mock WS 发送 pair_pending / bind_ok，验证 UI 状态变化
  - [ ] 模拟过期码，验证自动刷新流程
  - [ ] 模拟进程检测结果，验证 4 步进度同步
  - [ ] 所有路径无未捕获异常

---

## 7. 风险与开放问题

### OPEN-1: THS polling 与 bootstrap.ensure_xiadan_async() 的生命周期

**问题**：bootstrap 的 ensure_xiadan_async() 会自动安装同花顺，完成后设置 state.xiadan_path。polling task 何时启动？

**判断**：
- bootstrap 完成（xiadan_path 已设置或已尝试）后启动 polling
- 若 xiadan 不存在，polling 也会运行但 step 1 会失败，继续轮询等待用户手动装或指定路径
- **不是问题**——polling 是容错的

### OPEN-2: "更换"按钮的确认流程

**问题**：v3 设计稿只显示"✓ 路径 ... [更换]"，点击后用户选新路径。是否需要"确认"对话框？

**推荐**：无需。直接选择 → 保存 → 日志"✓ 已设置 xiadan 路径"即可。与现有 on_set_xiadan_path() 保持一致。

### OPEN-3: 配对码过期的"正在获取..."消息显示时长

**问题**：从 timeout 检测到新 pair_pending 到来，UI 要显示多久的 spinner？

**推荐**：一直显示直到新 pair_pending 帧到来。timeout 本身很快（检测 + ws.close())，server 重连通常 < 3 秒，用户能感知。

### OPEN-4: THS polling 的 2 秒周期是否确定？

**问题**：v3 设计稿写了"2秒轮询中..."，是固定还是可配置？

**推荐**：固定 2 秒。用户不需要可配置。若后续有性能问题再调。

### OPEN-5: 折叠状态下，4 步轮询是否继续？

**问题**：状态 ⑤（已折叠）时，是否还要检测 xiadan 进程是否消失？

**推荐**：**是的，继续轮询**。若进程消失则自动回到状态 ④ 并展开重新引导。这是"网络恢复力"需求。

### OPEN-6: "解除绑定"按钮的删除确认

**问题**：v3 原型没显示确认对话框，直接点即清除？

**推荐**：加一个简单确认弹窗："确定解除绑定？"，防止误操作。细节在工程师实现时定。

---

## 总结

v3 方案的核心架构：
1. **5 状态流**：由 connection_state + pairing_code/expires_at + ths_steps_complete 组合驱动
2. **自动刷新**：expires_at 过期 → ws.close() → reconnect + pair_init → 新码
3. **4 步轮询**：asyncio 后台任务，2 秒周期，exception safe，检测全程不阻塞 UI
4. **展开/折叠**：ths_steps_complete==4 时折叠，否则展开；进程断连时自动重新展开
5. **文件隔离**：修改集中在 main_window.py + main.py，ws_client.py 改动最小

所有现有检测函数复用，无需新增库依赖。
