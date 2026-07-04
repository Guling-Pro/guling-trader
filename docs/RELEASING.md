# 发布流程（RELEASING）

guling-trader 通过 GitHub Releases 分发单文件 exe。打 `v*` tag 会触发 `.github/workflows/build.yml`
在云端 Windows 机器上用 PyInstaller `--onefile` 编译 `guling-trader.exe` + `guling-trader.exe.sha256`
并自动发一个 Release。老用户的自更新器（v0.6.0 起内置）查的就是这个 `releases/latest`。

## 一条命令发布

```bash
scripts/release.sh 0.6.1            # 改版本号 → 提交 → 打 tag → 推 main + tag → 触发编译
scripts/release.sh 0.6.1 --dry-run  # 只预览版本号改动,不提交/不推送
scripts/release.sh 0.6.1 --watch    # 发布后阻塞等构建完成并核对 Release 两个资产
```

脚本做的事、以及它替你守的规矩：

1. **前置检查**：必须工作区干净、tag 不存在、在 main（或 detached 在 origin/main）。
2. **版本号 4 处同步**（历史上出过 pyproject 与 __init__ 不一致的坑，脚本一次性改全并校验）：
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `src/trader/__init__.py` → `__version__ = "X.Y.Z"`
   - `src/trader/handshake.py` → `CLIENT_VERSION = "X.Y.Z"`（握手上报的客户端版本）
   - `src/trader/main_window.py` → UI 页脚 `股灵交易助手 vX.Y.Z`
3. **格式校验**：版本号必须三段式数字 `X.Y.Z`（项目不引 `packaging` 依赖，自更新的版本比较也依赖此格式）。
4. **改动集合校验**：改完必须恰好这 4 个文件变化，否则判定某处 pattern 漂移、中止并回滚。
5. **发布前测试**：默认跑 `pytest`（`--skip-tests` 可跳过）。
6. **不可逆步骤前确认**：推 main / 推 tag 前要人工确认（`--yes` 可免）。

## 发布前必须想清楚的一件事：自更新"上膛"

自更新代码从 **v0.6.0** 起内置，但它在 v0.6.0 上是**休眠**的（v0.6.0 是最新版，查不到更新就不跑）。
**一旦发布下一个版本，v0.6.0 及以后的客户端就会看到更新提示，点"立即更新"即触发自替换/重启逻辑。**

因此：**在把自更新推给真实用户之前，必须先在 Windows 真机跑完自更新验证清单**
（重命名运行中的 exe、单例 mutex 时序、内联横幅渲染——CI 覆盖不到）。
验证清单见 `docs/superpowers/plans/2026-07-04-self-update.md` 的 Task 6。

## 发布后核对

- Actions 构建成功：<https://github.com/Guling-Pro/guling-trader/actions>
- Release 带两个资产：`guling-trader.exe` + `guling-trader.exe.sha256`
  ```bash
  gh release view v0.6.1 --json assets -q '.assets[].name'
  ```
- 本地 main 同步：`git pull --rebase origin main`

## 手动兜底（脚本不可用时）

```bash
# 在 main、工作区干净
# 1) 改上面 4 处版本号为新版本
# 2)
git commit -am "chore(release): v0.6.1"
git tag v0.6.1
git push origin main
git push origin v0.6.1        # 触发编译
```
