# guling-trader 自动更新提醒 + 一键更新 —— 设计

## 背景与目标

guling-trader 通过 GitHub Releases 分发单文件 exe（build.yml 已在每次打 tag 时自动打包 `guling-trader.exe` + `guling-trader.exe.sha256` 并发 Release）。目前用户升级需要自己发现新版本、手动下载替换。本设计给客户端加两件事：

1. **启动时检查更新**：对比 GitHub 最新 Release 版本号与当前版本，有新版本就弹窗提醒。
2. **一键更新**：用户确认后，程序自己下载新版本、校验、替换自身、重启，不需要手动下载操作。

## 范围边界（已与用户确认）

- 只做「启动时检查一次」，不做后台定时轮询（7×24 常驻但更新检查这件事不值得为它开一个额外的定时器）。
- 交易时段（9:30-11:30 / 13:00-15:00）内允许用户随时点「立即更新」，不做时段拦截或二次确认——由用户自己把控更新时机。
- 不做静默自动更新；提醒 + 用户手动点确认，是这次的终点。
- 不做「忽略此版本，以后都不再提示」这类状态持久化——跳过就只影响本次启动，下次启动重新检查（YAGNI，多一个持久化状态换不来实际收益）。

## 架构

新增 `src/trader/selfupdate/` 包，与 `src/trader/installer/`（负责给 xiadan 装/升级）平级、职责分离：

```
src/trader/selfupdate/
├── __init__.py
├── check.py     # 查 GitHub 最新 Release，跟当前 __version__ 比较，返回 UpdateInfo | None
└── apply.py     # 下载新 exe → 校验 SHA256 → 重命名自替换 → 拉起新进程 → 退出
```

复用现有基础设施，不重复造轮子：
- `installer/download.py` 的 `download_with_progress()` / `verify_sha256()`（二者本就是通用函数，不含 THS 专属逻辑，直接调用）
- `ui_dialogs.py` 的 `InstallProgressWindow`（构造函数已支持自定义 `title`，无需改动）
- 仿 `UpgradeAvailableDialog` 的样式新增 `SelfUpdateAvailableDialog`（同一文件 `ui_dialogs.py`）

## 检查流程（`check.py`）

时机：`main.py` 现有 Step 2「升级检查」区域内，与 THS 自身的 `maybe_upgrade_async()` 并列、各自独立 try/except（一个失败不影响另一个）。

1. `GET https://api.github.com/repos/Guling-Pro/guling-trader/releases/latest`（无需鉴权，公开仓库；未鉴权限额 60 次/小时/IP，启动查一次完全够用；命中限额或任何网络/解析异常都静默跳过、记 warning 日志，不打扰用户——升级检查从不是关键路径）
2. 从响应解析 `tag_name`（形如 `v0.5.0`）与 `assets[].browser_download_url`，按资产名精确匹配 `guling-trader.exe` 和 `guling-trader.exe.sha256`（build.yml 用 `softprops/action-gh-release` 上传，资产名即文件 basename，已核实）
3. 去掉 `v` 前缀，与 `trader.__version__` 做三元组数字比较（`(0,5,0) < (0,6,0)`）；项目版本号格式稳定是三段式，不为此引入 `packaging` 依赖
4. 有新版本 → 返回 `UpdateInfo(tag, current_version, exe_url, sha256_url)`；否则返回 `None`

## 更新执行流程（`apply.py`）—— Windows 下自替换

Windows 不允许进程覆盖自己正在运行的 exe 文件内容，但允许**重命名/删除**一个正在运行的 exe（文件数据靠已打开的句柄维持，只是不能同名覆盖）。据此设计：

```
用户点"立即更新"
  → InstallProgressWindow(title="正在更新 guling-trader")
  → download_with_progress(exe_url, dest=<程序目录>/guling-trader.exe.new)
  → 下载对应 .sha256（内容格式 `<hash>  guling-trader.exe`，标准 sha256sum 格式），取首个空白分隔字段作为期望哈希
  → verify_sha256(guling-trader.exe.new, expected)
      ✗ 失败 → 删除 .new，弹错误提示（见下），终止，不触碰当前运行中的 exe
      ✓ 通过 → 继续
  → os.rename(sys.executable, sys.executable + ".old")
  → os.rename(guling-trader.exe.new, sys.executable)
  → 显式释放单实例命名 mutex（main.py 中"进程存活期间不释放"的唯一例外——
     必须在拉起新进程前主动 CloseHandle，否则新进程的 _enforce_single_instance()
     会撞见旧 mutex 还没释放而误判"已有实例在跑"）
  → subprocess.Popen([sys.executable], creationflags=DETACHED_PROCESS, close_fds=True)
  → os._exit(0)
```

失败兜底：
- 「重命名当前 exe」这步本身失败（权限异常等极少数情况）→ 放弃更新，若已重命名则改回原名，不进入半成品状态，提示用户改走手动下载（附 Releases 页面链接文本）。
- 下载/校验失败 → 同上，当前运行中的程序完全不受影响，用户可以直接继续使用旧版本。

`.old` 文件清理：新进程启动、Step 2 升级检查之前，顺手尝试删除同目录的 `guling-trader.exe.old`——此时旧进程大概率已完全退出、文件已解锁；删不掉就静默跳过、下次启动再试，不影响功能，最多留一个几十 MB 的孤儿文件。

## 安全性

- 校验手段是 GitHub Release 里由 build.yml 自动生成的 SHA256，防的是下载损坏/传输篡改；GitHub Releases 资产本身走 HTTPS，进一步降低中间人篡改风险。
- **已知局限**：这不是代码签名，无法防御"仓库本身被攻破、恶意 exe 和匹配的 sha256 一起被发布"这种供应链层面的攻击。这次不引入代码签名（需要付费证书，且 build.yml 当前也未签名旧版本，属于已有基线，非本次引入的新缺口）——记在此处作为已知限制，不阻塞本次功能。
- 下载目标固定为程序自身所在目录，不落到临时目录/用户下载目录，避免路径混淆。
- 仓库地址硬编码为 `Guling-Pro/guling-trader`，不做成可配置项，避免被引导指向别的仓库。

## UI/UX 流程

- 检测到新版本 → `SelfUpdateAvailableDialog`：展示当前版本号/最新版本号 + 「立即更新」「跳过」两个按钮（样式仿 `UpgradeAvailableDialog`）。
- 「跳过」：本次启动不再提示；不做任何持久化，下次启动照常重新检查。
- 「立即更新」：走上述执行流程；成功后旧进程退出、新进程启动，用户体感等同于"程序自己重启了一次"，不额外弹"更新完成"确认框（新窗口出现本身就是最直接的反馈）。
- 失败：关闭进度窗，`tk.messagebox` 弹出错误提示，文案给出"可稍后重试，或前往 GitHub Releases 手动下载"+ 链接文本。

## 测试

- `tests/selfupdate/test_check.py`：mock aiohttp 响应 —— 有新版本时正确返回 `UpdateInfo`；版本相同/当前更高时返回 `None`；网络异常/限流时静默返回 `None`（不抛异常向上传播）。
- `tests/selfupdate/test_apply.py`：sha256 校验失败分支下不触碰当前 exe、不发生重命名（`verify_sha256` 本身已有测试覆盖，这里只测调用方分支）；Windows-only 的重命名/重启逻辑在 CI（ubuntu-latest 跑 `test.yml`）上通过 `platform.system() != "Windows"` 提前 skip，与现有 `bootstrap.maybe_upgrade_async` 的跳过模式一致。
- 不做端到端的真实"下载/替换/重启"自动化测试（需要真机+网络+进程生命周期，自动化 ROI 低）；改为发布前人工在真机走一遍完整流程验证。

## 影响文件清单

- 新增：`src/trader/selfupdate/__init__.py`、`check.py`、`apply.py`
- 新增：`tests/selfupdate/test_check.py`、`test_apply.py`
- 修改：`src/trader/main.py`（Step 2 附近接入检查调用）
- 修改：`src/trader/ui_dialogs.py`（新增 `SelfUpdateAvailableDialog`）
