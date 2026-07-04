#!/usr/bin/env bash
#
# guling-trader 发布脚本 —— 把"改版本号 → 提交 → 打 tag → 推送 → 触发云端编译"固化成一条命令。
#
# 用法:
#   scripts/release.sh 0.6.1              # 正式发布 0.6.1
#   scripts/release.sh 0.6.1 --dry-run    # 只预览版本号改动,不提交/不推送/不打 tag
#   scripts/release.sh 0.6.1 --watch      # 发布后阻塞等待 GitHub Actions 构建完成并核对 Release 资产
#   scripts/release.sh 0.6.1 --skip-tests # 跳过发布前的 pytest(默认会跑)
#
# 版本号统一维护在 4 处(见 docs/RELEASING.md),本脚本一次性同步,并校验确实改到 4 个文件,
# 防止某处 pattern 漂移导致版本号不一致(历史上出现过 pyproject 与 __init__ 不一致的坑)。
#
# 前提: 在 main 分支(或 detached 在 origin/main)、工作区干净、gh 已登录。
# 推 main + 推 tag 是不可逆的对外动作,脚本会在执行前打印将要做什么并要求确认(除非 --yes)。

set -euo pipefail

# ---- 参数解析 ----
NEW_VERSION=""
DRY_RUN=0
WATCH=0
SKIP_TESTS=0
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY_RUN=1 ;;
    --watch)      WATCH=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --yes|-y)     ASSUME_YES=1 ;;
    -*)           echo "未知选项: $arg" >&2; exit 2 ;;
    *)            NEW_VERSION="$arg" ;;
  esac
done

if [[ -z "$NEW_VERSION" ]]; then
  echo "用法: scripts/release.sh <version> [--dry-run] [--watch] [--skip-tests] [--yes]" >&2
  exit 2
fi

# 版本号必须是三段式数字(项目约定,不引 packaging 依赖,自更新版本比较也依赖此格式)
if [[ ! "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "✗ 版本号格式必须是 X.Y.Z(三段式数字),收到: $NEW_VERSION" >&2
  exit 2
fi
TAG="v${NEW_VERSION}"

# ---- 定位仓库根 ----
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---- 前置检查 ----
if [[ -n "$(git status --porcelain)" ]]; then
  echo "✗ 工作区不干净,先提交或清理再发布:" >&2
  git status --short >&2
  exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "✗ tag $TAG 已存在,换一个版本号或先删除旧 tag。" >&2
  exit 1
fi

echo "▶ 准备发布 $TAG(当前 HEAD: $(git rev-parse --short HEAD))"

# ---- 版本号 4 处同步 ----
# perl -pi 跨 macOS/Linux 可移植(BSD sed 与 GNU sed 的 -i 语法不一致,故用 perl)
perl -pi -e "s/^version = \".*\"/version = \"${NEW_VERSION}\"/"            pyproject.toml
perl -pi -e "s/^__version__ = \".*\"/__version__ = \"${NEW_VERSION}\"/"     src/trader/__init__.py
perl -pi -e "s/^CLIENT_VERSION = \".*\"/CLIENT_VERSION = \"${NEW_VERSION}\"/" src/trader/handshake.py
perl -pi -e "s/股灵交易助手 v[0-9][0-9.]*/股灵交易助手 v${NEW_VERSION}/"        src/trader/main_window.py

# 校验: 必须恰好这 4 个文件被改动,且每处都出现了新版本号
CHANGED="$(git diff --name-only | sort | tr '\n' ' ')"
EXPECT="pyproject.toml src/trader/__init__.py src/trader/handshake.py src/trader/main_window.py"
EXPECT_SORTED="$(echo "$EXPECT" | tr ' ' '\n' | sort | tr '\n' ' ')"
if [[ "$CHANGED" != "$EXPECT_SORTED" ]]; then
  echo "✗ 版本号改动的文件集合与预期不符(可能某处 pattern 漂移):" >&2
  echo "  实际: $CHANGED" >&2
  echo "  预期: $EXPECT_SORTED" >&2
  git checkout -- . 2>/dev/null || true
  exit 1
fi
echo "✓ 版本号已同步 4 处 → ${NEW_VERSION}"
git --no-pager diff --stat

# ---- dry-run: 到此为止,回滚改动 ----
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "— dry-run,回滚改动,不提交不推送。"
  git checkout -- .
  exit 0
fi

# ---- 发布前测试 ----
if [[ "$SKIP_TESTS" -eq 0 ]]; then
  echo "▶ 跑发布前测试(uv run pytest)…"
  uv run --with pytest --with pytest-asyncio pytest tests/ -q
fi

# ---- 确认(不可逆步骤前) ----
if [[ "$ASSUME_YES" -eq 0 ]]; then
  echo
  echo "即将执行(不可逆): 提交 chore(release): ${TAG} → 推送 main → 打并推送 tag ${TAG} → 触发云端编译发 Release。"
  read -r -p "确认继续? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "已取消,回滚改动。"; git checkout -- .; exit 0; }
fi

# ---- 提交 + tag + 推送 ----
git add pyproject.toml src/trader/__init__.py src/trader/handshake.py src/trader/main_window.py
git commit -q -m "chore(release): ${TAG}"
git tag "$TAG"

echo "▶ 推送 main…"
git push origin HEAD:main
echo "▶ 推送 tag ${TAG}(触发 build.yml 编译 exe + 发 Release)…"
git push origin "$TAG"

echo "✓ 已推送。构建进度: https://github.com/Guling-Pro/guling-trader/actions"

# ---- 可选: 等构建完成并核对资产 ----
if [[ "$WATCH" -eq 1 ]]; then
  echo "▶ 等待 GitHub Actions 构建…"
  RUN_ID=""
  for _ in 1 2 3 4 5 6; do
    RUN_ID="$(gh run list --workflow=build.yml --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
    [[ -n "$RUN_ID" ]] && break
    sleep 5
  done
  if [[ -n "$RUN_ID" ]]; then
    gh run watch "$RUN_ID" --exit-status --interval 20 || { echo "✗ 构建失败,查 Actions 日志。" >&2; exit 1; }
  fi
  echo "▶ 核对 Release 资产…"
  gh release view "$TAG" --json assets -q '.assets[].name'
fi

echo "✅ 发布完成: ${TAG}"
