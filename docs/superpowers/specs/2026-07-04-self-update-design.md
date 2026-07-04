# guling-trader 自动更新提醒 + 一键更新 —— 设计

## 背景与目标

guling-trader 通过 GitHub Releases 分发单文件 exe（build.yml 已在每次打 tag 时自动打包 `guling-trader.exe` + `guling-trader.exe.sha256` 并发 Release）。目前用户升级需要自己发现新版本、手动下载替换。本设计给客户端加两件事：

1. **启动时检查更新**：对比 GitHub 最新 Release 版本号与当前版本，有新版本就提醒。
2. **一键更新**：用户确认后，程序自己下载新版本、校验、替换自身、重启，不需要手动下载操作。

## 范围边界（已与用户确认）

- 只做「启动时检查一次」，不做后台定时轮询（7×24 常驻但更新检查这件事不值得为它开一个额外的定时器）。
- 交易时段（9:30-11:30 / 13:00-15:00）内允许用户随时点「立即更新」，不做时段拦截或二次确认——由用户自己把控更新时机。
- 不做静默自动更新；提醒 + 用户手动点确认，是这次的终点。
- 不做「忽略此版本，以后都不再提示」这类状态持久化——跳过就只影响本次启动，下次启动重新检查（YAGNI，多一个持久化状态换不来实际收益）。
- 仅 Windows 生效（exe 只发 Windows）。检查这一步也一并做 `platform.system() == "Windows"` 门控，不只是执行替换的那步——非 Windows 上弹出一个"有新版但点了也更新不了"的提示没有意义，直接不查。

## 架构与线程模型（关键：本 app 的核心复杂度在这里）

`main.py` 模块 docstring 已经写明：**主线程跑 Tk mainloop，asyncio 事件循环跑在后台 daemon thread，两边靠 `SharedState`（`main_window.py:31`，内置 `threading.Lock`）交换数据**；Tk 组件只能在主线程创建/操作。这条约束必须贯穿本设计，不能假设"随便找个地方弹个窗"是安全的。

进一步核实发现：`ui_dialogs.py` 里定义的弹窗类（`InstallProgressWindow`、`UpgradeAvailableDialog` 等）在 `src/`、`tests/` 里**没有任何一处被实例化**——整个模块目前是死代码，不是"已验证可复用的现有基础设施"。真正在跑、被验证过的 UI 更新路径是：

```
后台线程（auto_install 下载 THS）
  → on_event(...) 回调  (main.py:_make_installer_event_handler)
  → state.update(install_progress=(done, total))   # SharedState，线程安全
                    ↓
主线程 root.after(100, self._schedule_poll)         # main_window.py:592，每 100ms 轮询一次
  → self._sync_state(): snap = state.snapshot()
  → 按 snap 内容 pack()/pack_forget() 显隐已存在的 widget（main_window.py:640-670 一带的写法）
```

本设计**照抄这条已验证路径**，不新增任何 Toplevel 弹窗，也不去碰 `ui_dialogs.py`：

```
src/trader/selfupdate/
├── __init__.py
├── check.py     # 查 GitHub 最新 Release，跟当前 __version__ 比较，返回 UpdateInfo | None
└── apply.py     # 下载新 exe → 校验 SHA256 → 重命名自替换 → 拉起新进程 → 退出
```

三处跨线程/跨模块交接点，逐一挑明：

1. **check.py 的结果 → UI**：`check.py` 是 async 函数，在 Step 2 内、后台 asyncio 线程里跑（和 `bootstrap.maybe_upgrade_async()` 并列）。拿到 `UpdateInfo` 后**不直接碰 Tk**，而是 `state.update(self_update_info={...})`（新增 `SharedState` 字段，走现成的 lock）。主线程的 `_schedule_poll` 下一次轮询（≤100ms 后）在 `_sync_state()` 里读到它，`pack()` 出一个之前 `pack_forget()` 隐藏着的横幅区（在 `_build_ui()` 阶段就创建好，和 `ths_install_box` 那种条件显隐 widget 是同一套写法）。
2. **「立即更新」按钮点击 → 触发下载**：按钮回调本身在**主线程**（Tk 事件），但 `apply.py` 要跑 `aiohttp` 异步下载，不能在主线程里直接 `await`。做法：`asyncio.run_coroutine_threadsafe(selfupdate.apply.run_update(info, on_progress), loop)`，`loop` 取自 `main.py:127` 现成的 `_ws_client_holder["loop"]`——这个"主线程发起、丢给后台 loop 跑"的模式在 `main.py:591`（关闭 ws 连接那处）已有先例，不是新发明。
3. **下载进度 → UI**：`apply.py` 内 `download_with_progress()` 的 `on_progress(done, total)` 回调运行在**后台线程**，调用 `state.update(self_update_progress=(done, total))`（同样走 lock，安全）；`_sync_state()` 轮询到后更新横幅里的进度条/百分比文字——和现有 `install_progress` 字段渲染 `install_progress_var`/`install_pct_lbl` 的写法完全对称，是同一模式的兄弟字段（不复用同一个字段，避免 THS 安装进度和自更新进度语义混在一起）。
4. **失败反馈**：`apply.py` 里任何异常 → `state.log(...)`（`log_messages` 是 `queue.Queue`，本身线程安全，`_drain_log_queue()` 已在轮询里跑）+ `state.update(self_update_status="error")`；横幅切换成错误文案，同样不用弹窗。

复用 `installer/download.py` 的 `download_with_progress()` / `verify_sha256()`——这两个函数本身是通用的（参数是任意 url/dest/expected_hash，不含 THS 专属逻辑），这条复用是成立的，和 UI 层的死代码问题无关。

## 检查流程（`check.py`）

时机：`main.py` 现有 Step 2「升级检查」区域内，与 THS 自身的 `maybe_upgrade_async()` 并列、各自独立 try/except（一个失败不影响另一个），且整体包一层 `if platform.system() == "Windows"`。

1. `GET https://api.github.com/repos/Guling-Pro/guling-trader/releases/latest`（无需鉴权，公开仓库；未鉴权限额 60 次/小时/IP，启动查一次完全够用；命中限额或任何网络/解析异常都静默跳过、记 warning 日志，不打扰用户——升级检查从不是关键路径）
2. 从响应解析 `tag_name`（形如 `v0.5.0`）与 `assets[].browser_download_url`，按资产名精确匹配 `guling-trader.exe` 和 `guling-trader.exe.sha256`（build.yml 用 `softprops/action-gh-release` 上传，资产名即文件 basename，已核实）
3. 去掉 `v` 前缀，与 `trader.__version__` 做三元组数字比较（`(0,5,0) < (0,6,0)`）；项目版本号格式稳定是三段式，不为此引入 `packaging` 依赖
4. 有新版本 → 返回 `UpdateInfo(tag, current_version, exe_url, sha256_url)`；否则返回 `None`

## 更新执行流程（`apply.py`）—— Windows 下自替换

Windows 不允许进程覆盖自己正在运行的 exe 文件内容，但允许**重命名/删除**一个正在运行的 exe（文件数据靠已打开的句柄维持，只是不能同名覆盖）。据此设计（`run_update()` 是跑在后台 asyncio loop 上的协程，由主线程按上文机制 1 用 `run_coroutine_threadsafe` 触发）：

```
run_update(info, on_progress)  # 后台线程/loop
  → 前置 sanity check：Path(sys.executable).name 必须等于预期的 "guling-trader.exe"
     （防的是 PyInstaller 改成 onedir 打包、或开发环境直接跑 python.exe 时误触发替换逻辑；
     不满足就直接放弃更新+记错误日志，不是本次要处理的正常路径）
  → download_with_progress(exe_url, dest=<程序目录>/guling-trader.exe.new, on_progress=...)
  → 下载对应 .sha256（内容格式 `<hash>  guling-trader.exe`，标准 sha256sum 格式），取首个空白分隔字段作为期望哈希
  → verify_sha256(guling-trader.exe.new, expected)
      ✗ 失败 → 删除 .new，state.log 错误 + state.update(self_update_status="error")，终止，不触碰当前运行中的 exe
      ✓ 通过 → 继续
  → os.rename(sys.executable, sys.executable + ".old")
  → os.rename(guling-trader.exe.new, sys.executable)
  → main.release_singleton_mutex()   # 见下方"mutex 释放"
  → subprocess.Popen([sys.executable], creationflags=DETACHED_PROCESS, close_fds=True)
  → os._exit(0)
```

**mutex 释放**：`_SINGLETON_MUTEX` 是 `main.py` 模块级私有全局（`main.py:452`），`apply.py` 不应该伸手直接改别的模块的私有状态。在 `main.py` 新增一个小函数 `release_singleton_mutex()`（内部做 `ctypes.windll.kernel32.CloseHandle(_SINGLETON_MUTEX)` + 置空），`apply.py` 只调用这个公开函数。必须在拉起新进程**之前**调用——否则新进程的 `_enforce_single_instance()`（用的是 session-local 命名 mutex，靠 `GetLastError()==ERROR_ALREADY_EXISTS` 判定）会撞见旧 mutex 还没释放，误判"已有实例在跑"而直接退出。

失败兜底：
- 「重命名当前 exe」这步本身失败（权限异常等极少数情况，包括程序目录本身不可写——例如放在 `Program Files` 下的场景）→ 放弃更新，若已重命名则改回原名，不进入半成品状态，走上面第 4 点的错误反馈路径，文案提示用户改走手动下载（附 Releases 页面链接文本）。这依赖"程序目录可写"这个前提；本项目发的是裸 exe（非安装包），用户通常放在自己可写的目录，不是安装包分发那种默认落在 `Program Files` 的场景，但仍在此处显式记录这个假设。
- 下载/校验失败 → 同上，当前运行中的程序完全不受影响，用户可以直接继续使用旧版本。

`.old` 文件清理：新进程启动、Step 2 升级检查之前，顺手尝试删除同目录的 `guling-trader.exe.old`——此时旧进程大概率已完全退出、文件已解锁；删不掉就静默跳过、下次启动再试，不影响功能，最多留一个几十 MB 的孤儿文件。

**已知的小局限（不阻塞，记录即可）**：`subprocess.Popen([sys.executable], ...)` 拉起新进程时不会带上原始 argv/cwd。当前 app 不吃命令行参数，无影响；若日后加了启动参数，这里需要一并透传（`sys.argv[1:]`），届时再补。

## 安全性

- 校验手段是 GitHub Release 里由 build.yml 自动生成的 SHA256，防的是下载损坏/传输篡改；GitHub Releases 资产本身走 HTTPS，进一步降低中间人篡改风险。
- **已知局限**：这不是代码签名，无法防御"仓库本身被攻破、恶意 exe 和匹配的 sha256 一起被发布"这种供应链层面的攻击。这次不引入代码签名（需要付费证书，且 build.yml 当前也未签名旧版本，属于已有基线，非本次引入的新缺口）——记在此处作为已知限制，不阻塞本次功能。
- 下载目标固定为程序自身所在目录，不落到临时目录/用户下载目录，避免路径混淆。
- 仓库地址硬编码为 `Guling-Pro/guling-trader`，不做成可配置项，避免被引导指向别的仓库。

## UI/UX 流程（内联横幅，不新增弹窗）

在主窗口里新增一个条件显隐的横幅区（`_build_ui()` 阶段创建、默认 `pack_forget()` 隐藏，风格参照 `ths_install_box` 那一类卡片），由 `_sync_state()` 按 `SharedState` 新增的三个字段驱动：`self_update_info`（`{tag, current_version, latest_version}` 或 `None`）、`self_update_progress`（`(done, total)` 或 `None`）、`self_update_status`（`idle`/`downloading`/`error`）。

- 检测到新版本 → 横幅显示"发现新版本 v{latest_version}（当前 v{current_version}）" + 「立即更新」「跳过」两个按钮。
- 「跳过」：`state.update(self_update_info=None)`（或等效的本地隐藏标记）——本次启动不再提示；不做任何磁盘持久化，下次启动照常重新检查。
- 「立即更新」：按机制 2 丢给后台 loop 跑 `run_update()`；横幅切到进度态，显示百分比（复用 `install_progress_var` 那一套渲染写法，但用独立的 widget/变量，不和 THS 安装进度混用）。
- 成功：旧进程退出、新进程启动，用户体感等同于"程序自己重启了一次"，不需要额外的"更新完成"提示——新窗口出现本身就是最直接的反馈。
- 失败：横幅切到错误文案"更新失败，可稍后重试或前往 GitHub Releases 手动下载"（附链接文本），「立即更新」按钮保留可再次点击重试。

## 测试

- `tests/selfupdate/test_check.py`：mock aiohttp 响应 —— 有新版本时正确返回 `UpdateInfo`；版本相同/当前更高时返回 `None`；网络异常/限流时静默返回 `None`（不抛异常向上传播）；非 Windows 平台直接跳过不发请求。
- `tests/selfupdate/test_apply.py`：sha256 校验失败分支下不触碰当前 exe、不发生重命名（`verify_sha256` 本身已有测试覆盖，这里只测调用方分支）；`sys.executable` basename 不符预期时提前中止；Windows-only 的重命名/重启逻辑在 CI（ubuntu-latest 跑 `test.yml`）上通过 `platform.system() != "Windows"` 提前 skip，与现有 `bootstrap.maybe_upgrade_async` 的跳过模式一致。
- 不做端到端的真实"下载/替换/重启"自动化测试（需要真机+网络+进程生命周期，自动化 ROI 低）；改为发布前人工在真机走一遍完整流程验证。

## 影响文件清单

- 新增：`src/trader/selfupdate/__init__.py`、`check.py`、`apply.py`
- 新增：`tests/selfupdate/test_check.py`、`test_apply.py`
- 修改：`src/trader/main.py`（Step 2 附近接入检查调用；新增 `release_singleton_mutex()`）
- 修改：`src/trader/main_window.py`（`SharedState` 新增 3 个字段——`snapshot()` 是手工维护的 dict 字面量、不是从 dataclass 字段自动生成，新字段要在类属性声明和 `snapshot()` 里各加一处，两处不同步会导致轮询读不到值且不报错；`_build_ui()` 新增横幅 widget；`_sync_state()` 新增对应渲染分支）
- 不改动：`src/trader/ui_dialogs.py`（保持不动——它现在是未使用的死代码，本次不为了这个功能去验证/复活它）
