# guling-trader 自动更新提醒 + 一键更新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** guling-trader 启动时对比 GitHub 最新 Release 版本号，发现新版本在主窗口内联横幅提醒；用户点「立即更新」后程序自己下载新 exe、校验 SHA256、Windows 下重命名自替换并重启，无需手动下载操作。

**Architecture:** 新增 `src/trader/selfupdate/` 包（`check.py` 查版本、`apply.py` 下载+校验+自替换）；不新增任何 Toplevel 弹窗（`ui_dialogs.py` 是死代码，保持不动），走已验证的 `SharedState`（线程安全）+ `_schedule_poll`（主线程 100ms 轮询）+ 内联横幅显隐这条现有真实路径，和 THS 安装进度条同一套写法。三处跨线程交接：`check` 结果经 `SharedState` 传给主线程渲染；「立即更新」按钮（主线程）经 `asyncio.run_coroutine_threadsafe` 丢给后台 loop 跑下载；下载进度回调（后台线程）经 `SharedState` 传回渲染。

**Tech Stack:** Python 3.11+，`aiohttp`（已是依赖，GitHub API 查询 + sha256 文件拉取）、`tkinter`/`ttk`（已用）、`pytest` + `pytest-asyncio`（已用，`@pytest.mark.asyncio` 显式标注模式）。

## Global Constraints

- 仅 Windows 生效：`check`（查询）和 `apply`（自替换）都在 `platform.system() == "Windows"` 门控之后才跑；非 Windows 上不查、不弹、不报错。
- 只做「启动时检查一次」，不做后台定时轮询。
- 交易时段内允许用户随时点「立即更新」，不做时段拦截或二次确认。
- 不做「忽略此版本」的磁盘持久化——跳过只影响本次启动。
- 不引入 `packaging` 依赖，版本比较用三元组数字比较（项目版本号格式固定三段式，如 `0.5.0`）。
- 仓库地址硬编码 `Guling-Pro/guling-trader`，不做成配置项。
- 下载目标固定落在程序自身所在目录（`Path(sys.executable).resolve().parent`），不落临时目录/下载目录。
- 不新增/复活 `ui_dialogs.py` 里的任何 Toplevel 弹窗类；UI 一律走 `SharedState` + 主窗口内联横幅。
- `apply.py` 不直接触碰 `main.py` 的私有全局 `_SINGLETON_MUTEX`；改用依赖注入的 `release_singleton_mutex` 回调参数。
- 「立即更新」按钮防重入：`self_update_status == "downloading"` 时忽略重复点击。

---

## Task 1: `selfupdate/check.py` —— 版本比较 + GitHub Release 查询

**Files:**
- Create: `src/trader/selfupdate/__init__.py`（空文件，标记包）
- Create: `src/trader/selfupdate/check.py`
- Test: `tests/selfupdate/__init__.py`（空文件）
- Test: `tests/selfupdate/test_check.py`

**Interfaces:**
- Produces: `UpdateInfo`（dataclass：`tag: str`、`current_version: str`、`latest_version: str`、`exe_url: str`、`sha256_url: str`）；`async def check_for_update(current_version: str) -> Optional[UpdateInfo]`——供 Task 3（`apply.run_update` 的调用方，即 Task 5 main.py）消费。

- [ ] **Step 1: 创建包目录**

```bash
mkdir -p src/trader/selfupdate tests/selfupdate
touch src/trader/selfupdate/__init__.py tests/selfupdate/__init__.py
```

- [ ] **Step 2: 写失败测试 `tests/selfupdate/test_check.py`**

```python
"""selfupdate.check 模块测试"""
from unittest.mock import patch

import pytest


class MockResponse:
    def __init__(self, status, json_data):
        self.status = status
        self._json_data = json_data

    async def json(self):
        return self._json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockSession:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def get(self, *args, **kwargs):
        return self.resp


def _release_json(tag_name, has_exe=True, has_sha256=True):
    assets = []
    if has_exe:
        assets.append({
            "name": "guling-trader.exe",
            "browser_download_url": f"https://github.com/x/releases/download/{tag_name}/guling-trader.exe",
        })
    if has_sha256:
        assets.append({
            "name": "guling-trader.exe.sha256",
            "browser_download_url": f"https://github.com/x/releases/download/{tag_name}/guling-trader.exe.sha256",
        })
    return {"tag_name": tag_name, "assets": assets}


@pytest.mark.asyncio
async def test_new_version_available_returns_update_info():
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.6.0"))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is not None
    assert info.tag == "v0.6.0"
    assert info.current_version == "0.5.0"
    assert info.latest_version == "0.6.0"
    assert info.exe_url.endswith("guling-trader.exe")
    assert info.sha256_url.endswith("guling-trader.exe.sha256")


@pytest.mark.asyncio
async def test_same_version_returns_none():
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.5.0"))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_current_version_newer_returns_none():
    """防御性：当前版本比 Release 还新（比如本地跑的是未发布分支），不应回退提示"""
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.4.0"))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_missing_assets_returns_none():
    from trader.selfupdate import check

    mock_resp = MockResponse(200, _release_json("v0.6.0", has_sha256=False))
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_non_200_status_returns_none():
    from trader.selfupdate import check

    mock_resp = MockResponse(403, {})  # 例如限流
    mock_session = MockSession(mock_resp)

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", return_value=mock_session):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_network_exception_returns_none_not_raises():
    from trader.selfupdate import check

    with patch("trader.selfupdate.check.platform.system", return_value="Windows"), \
         patch("aiohttp.ClientSession", side_effect=RuntimeError("network down")):
        info = await check.check_for_update("0.5.0")

    assert info is None


@pytest.mark.asyncio
async def test_non_windows_skips_without_http_call():
    from trader.selfupdate import check

    def _boom(*args, **kwargs):
        raise AssertionError("非 Windows 不应发起 HTTP 请求")

    with patch("trader.selfupdate.check.platform.system", return_value="Darwin"), \
         patch("aiohttp.ClientSession", side_effect=_boom):
        info = await check.check_for_update("0.5.0")

    assert info is None


def test_version_tuple_ordering():
    from trader.selfupdate.check import _is_newer

    assert _is_newer("0.6.0", "0.5.0")
    assert not _is_newer("0.5.0", "0.5.0")
    assert not _is_newer("0.4.0", "0.5.0")
    assert _is_newer("1.0.0", "0.9.9")
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/selfupdate/test_check.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'trader.selfupdate.check'`

- [ ] **Step 4: 实现 `src/trader/selfupdate/check.py`**

```python
"""guling-trader 自更新检查：查 GitHub Releases 最新版本，跟当前版本比较。"""
from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_REPO = "Guling-Pro/guling-trader"
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_EXE_ASSET_NAME = "guling-trader.exe"
_SHA256_ASSET_NAME = "guling-trader.exe.sha256"


@dataclass
class UpdateInfo:
    tag: str
    current_version: str
    latest_version: str
    exe_url: str
    sha256_url: str


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


async def check_for_update(current_version: str) -> Optional[UpdateInfo]:
    """查 GitHub 最新 Release，跟 current_version 比较。有新版本返回 UpdateInfo，否则 None。

    任何网络/解析异常都吞掉返回 None——升级检查从不是关键路径，不能因为它影响启动。
    """
    if platform.system() != "Windows":
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _RELEASES_LATEST_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning("GitHub Release API 返回非 200：%s", resp.status)
                    return None
                data = await resp.json()
    except Exception as e:
        logger.warning("检查 guling-trader 更新失败：%s", e)
        return None

    tag = data.get("tag_name", "")
    latest_version = tag.lstrip("v")
    if not latest_version or not _is_newer(latest_version, current_version):
        return None

    exe_url = None
    sha256_url = None
    for asset in data.get("assets", []):
        name = asset.get("name")
        if name == _EXE_ASSET_NAME:
            exe_url = asset.get("browser_download_url")
        elif name == _SHA256_ASSET_NAME:
            sha256_url = asset.get("browser_download_url")

    if not exe_url or not sha256_url:
        logger.warning("Release %s 缺少 %s 或 %s 资产，跳过", tag, _EXE_ASSET_NAME, _SHA256_ASSET_NAME)
        return None

    return UpdateInfo(
        tag=tag,
        current_version=current_version,
        latest_version=latest_version,
        exe_url=exe_url,
        sha256_url=sha256_url,
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/selfupdate/test_check.py -v`
Expected: 8 个测试全部 PASS

- [ ] **Step 6: Commit**

```bash
git add src/trader/selfupdate/__init__.py src/trader/selfupdate/check.py tests/selfupdate/__init__.py tests/selfupdate/test_check.py
git commit -m "feat(selfupdate): check.py 查 GitHub Release 比版本号"
```

---

## Task 2: `selfupdate/apply.py` —— 纯逻辑部分（sha256 解析 / 文件重命名互换 / 孤儿清理）

**Files:**
- Create: `src/trader/selfupdate/apply.py`（本任务只写纯逻辑部分，`run_update` 编排逻辑留给 Task 3 在同一文件里追加）
- Test: `tests/selfupdate/test_apply.py`

**Interfaces:**
- Consumes: 无（纯文件系统操作，无跨任务依赖）
- Produces: `SelfUpdateError(Exception)`；`_parse_sha256_file(content: str) -> str`；`_swap_files(exe_path: Path, new_path: Path, old_path: Path) -> None`（失败自动回滚，异常上抛）；`cleanup_orphan_files(exe_dir: Path) -> None`；模块级常量 `EXPECTED_EXE_NAME = "guling-trader.exe"`——Task 3 的 `run_update` 直接调用这三个函数。

- [ ] **Step 1: 写失败测试 `tests/selfupdate/test_apply.py`（先写这部分，Task 3 会在同一文件里追加更多测试）**

```python
"""selfupdate.apply 模块测试"""
import os

import pytest


def test_parse_sha256_file_standard_format():
    from trader.selfupdate.apply import _parse_sha256_file

    assert _parse_sha256_file("deadbeef  guling-trader.exe\n") == "deadbeef"


def test_parse_sha256_file_single_space():
    from trader.selfupdate.apply import _parse_sha256_file

    assert _parse_sha256_file("deadbeef guling-trader.exe") == "deadbeef"


def test_parse_sha256_file_empty_raises():
    from trader.selfupdate.apply import _parse_sha256_file, SelfUpdateError

    with pytest.raises(SelfUpdateError):
        _parse_sha256_file("   \n")


def test_swap_files_success(tmp_path):
    from trader.selfupdate.apply import _swap_files

    exe = tmp_path / "guling-trader.exe"
    new = tmp_path / "guling-trader.exe.new"
    old = tmp_path / "guling-trader.exe.old"
    exe.write_text("old-content")
    new.write_text("new-content")

    _swap_files(exe, new, old)

    assert exe.read_text() == "new-content"
    assert old.read_text() == "old-content"
    assert not new.exists()


def test_swap_files_rollback_on_second_rename_failure(tmp_path, monkeypatch):
    from trader.selfupdate import apply

    exe = tmp_path / "guling-trader.exe"
    new = tmp_path / "guling-trader.exe.new"
    old = tmp_path / "guling-trader.exe.old"
    exe.write_text("old-content")
    new.write_text("new-content")

    real_rename = os.rename
    call_count = {"n": 0}

    def flaky_rename(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated failure")
        real_rename(src, dst)

    monkeypatch.setattr(apply.os, "rename", flaky_rename)

    with pytest.raises(OSError):
        apply._swap_files(exe, new, old)

    # 回滚：exe_path 内容恢复成旧的，old_path 不再单独存在
    assert exe.read_text() == "old-content"
    assert not old.exists()


def test_cleanup_orphan_files_removes_existing(tmp_path):
    from trader.selfupdate.apply import cleanup_orphan_files, EXPECTED_EXE_NAME

    old = tmp_path / (EXPECTED_EXE_NAME + ".old")
    new = tmp_path / (EXPECTED_EXE_NAME + ".new")
    old.write_text("x")
    new.write_text("y")

    cleanup_orphan_files(tmp_path)

    assert not old.exists()
    assert not new.exists()


def test_cleanup_orphan_files_noop_when_absent(tmp_path):
    from trader.selfupdate.apply import cleanup_orphan_files

    cleanup_orphan_files(tmp_path)  # 不应抛异常
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/selfupdate/test_apply.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'trader.selfupdate.apply'`

- [ ] **Step 3: 实现 `src/trader/selfupdate/apply.py`（本任务只写这部分内容）**

```python
"""guling-trader 自更新执行：下载新 exe → 校验 SHA256 → Windows 重命名自替换 → 拉起新进程 → 退出。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

EXPECTED_EXE_NAME = "guling-trader.exe"


class SelfUpdateError(Exception):
    """自更新过程中的可恢复错误（下载/校验/替换失败），调用方捕获后走错误提示分支。"""


def _parse_sha256_file(content: str) -> str:
    """解析 sha256sum 格式：'<hash>  guling-trader.exe' → 取首个空白分隔字段。"""
    stripped = content.strip()
    if not stripped:
        raise SelfUpdateError("sha256 文件内容为空")
    return stripped.split()[0]


def _swap_files(exe_path: Path, new_path: Path, old_path: Path) -> None:
    """把 exe_path 换成 new_path 的内容：exe_path→old_path，new_path→exe_path。

    第二步失败时自动把 old_path 改回 exe_path（回滚），不留半成品；异常继续上抛给调用方。
    """
    os.rename(exe_path, old_path)
    try:
        os.rename(new_path, exe_path)
    except Exception:
        os.rename(old_path, exe_path)
        raise


def cleanup_orphan_files(exe_dir: Path) -> None:
    """启动时清理上一次更新可能留下的 .old/.new 孤儿文件（尽力而为，失败静默跳过）。"""
    for suffix in (".old", ".new"):
        path = exe_dir / (EXPECTED_EXE_NAME + suffix)
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("清理孤儿文件 %s 失败（下次再试）：%s", path, e)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/selfupdate/test_apply.py -v`
Expected: 7 个测试全部 PASS

- [ ] **Step 5: Commit**

```bash
git add src/trader/selfupdate/apply.py tests/selfupdate/test_apply.py
git commit -m "feat(selfupdate): apply.py 纯逻辑——sha256解析/文件互换/孤儿清理"
```

---

## Task 3: `selfupdate/apply.py` —— `run_update` 编排逻辑（下载 + 校验 + 自替换 + 重启）

**Files:**
- Modify: `src/trader/selfupdate/apply.py`（在 Task 2 基础上追加）
- Modify: `tests/selfupdate/test_apply.py`（追加测试）

**Interfaces:**
- Consumes: `check.UpdateInfo`（`exe_url`、`sha256_url` 字段）；`installer.download.download_with_progress(url, dest, on_progress=None, chunk_size=65536) -> Path`（已存在，async）；`installer.download.verify_sha256(file_path, expected_sha256) -> bool`（已存在，async）；Task 2 的 `_parse_sha256_file`/`_swap_files`/`SelfUpdateError`/`EXPECTED_EXE_NAME`。
- Produces: `async def run_update(info: UpdateInfo, on_progress: Callable[[int, int], None], release_singleton_mutex: Callable[[], None]) -> None`——供 Task 5（`main.py` 的按钮回调）调用。**注意**：`release_singleton_mutex` 是依赖注入的回调（不在 `apply.py` 里导入 `main.py`，避免循环导入和反向耦合私有全局）；成功路径末尾会调用 `os._exit(0)`，函数正常情况下不会返回。

- [ ] **Step 1: 在 `tests/selfupdate/test_apply.py` 追加失败测试**

```python
# 追加到 tests/selfupdate/test_apply.py 文件末尾
import sys
from pathlib import Path
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_run_update_success(tmp_path, monkeypatch):
    from trader.selfupdate import apply
    from trader.selfupdate.check import UpdateInfo

    exe_path = tmp_path / "guling-trader.exe"
    exe_path.write_text("old-binary")
    monkeypatch.setattr(apply.sys, "executable", str(exe_path))

    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    async def fake_download(url, dest, on_progress=None, chunk_size=65536):
        Path(dest).write_text("new-binary")
        if on_progress:
            on_progress(10, 10)
        return Path(dest)

    async def fake_verify(file_path, expected):
        assert expected == "deadbeef"
        return True

    class MockShaResp:
        status = 200

        async def text(self):
            return "deadbeef  guling-trader.exe\n"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class MockShaSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, *a, **kw):
            return MockShaResp()

    monkeypatch.setattr(apply, "download_with_progress", fake_download)
    monkeypatch.setattr(apply, "verify_sha256", fake_verify)
    monkeypatch.setattr(apply.aiohttp, "ClientSession", lambda: MockShaSession())

    popen_calls = []
    monkeypatch.setattr(apply.subprocess, "Popen", lambda *a, **kw: popen_calls.append((a, kw)))
    exit_calls = []
    monkeypatch.setattr(apply.os, "_exit", lambda code: exit_calls.append(code))

    release_calls = []
    progress_calls = []

    await apply.run_update(
        info,
        on_progress=lambda d, t: progress_calls.append((d, t)),
        release_singleton_mutex=lambda: release_calls.append(True),
    )

    assert exe_path.read_text() == "new-binary"
    assert (tmp_path / "guling-trader.exe.old").read_text() == "old-binary"
    assert not (tmp_path / "guling-trader.exe.new").exists()
    assert release_calls == [True]
    assert len(popen_calls) == 1
    assert exit_calls == [0]
    assert progress_calls == [(10, 10)]


@pytest.mark.asyncio
async def test_run_update_sha256_mismatch_leaves_exe_untouched(tmp_path, monkeypatch):
    from trader.selfupdate import apply
    from trader.selfupdate.check import UpdateInfo

    exe_path = tmp_path / "guling-trader.exe"
    exe_path.write_text("old-binary")
    monkeypatch.setattr(apply.sys, "executable", str(exe_path))

    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    async def fake_download(url, dest, on_progress=None, chunk_size=65536):
        Path(dest).write_text("new-binary")
        return Path(dest)

    async def fake_verify(file_path, expected):
        return False  # 校验失败

    class MockShaResp:
        status = 200

        async def text(self):
            return "deadbeef  guling-trader.exe\n"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class MockShaSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, *a, **kw):
            return MockShaResp()

    monkeypatch.setattr(apply, "download_with_progress", fake_download)
    monkeypatch.setattr(apply, "verify_sha256", fake_verify)
    monkeypatch.setattr(apply.aiohttp, "ClientSession", lambda: MockShaSession())

    with pytest.raises(apply.SelfUpdateError, match="SHA256"):
        await apply.run_update(
            info, on_progress=lambda d, t: None, release_singleton_mutex=lambda: None
        )

    assert exe_path.read_text() == "old-binary"
    assert not (tmp_path / "guling-trader.exe.new").exists()
    assert not (tmp_path / "guling-trader.exe.old").exists()


@pytest.mark.asyncio
async def test_run_update_wrong_exe_name_aborts_before_download(tmp_path, monkeypatch):
    from trader.selfupdate import apply
    from trader.selfupdate.check import UpdateInfo

    wrong_exe = tmp_path / "python.exe"
    wrong_exe.write_text("dev-env")
    monkeypatch.setattr(apply.sys, "executable", str(wrong_exe))

    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    download_calls = []

    async def fake_download(*a, **kw):
        download_calls.append(True)

    monkeypatch.setattr(apply, "download_with_progress", fake_download)

    with pytest.raises(apply.SelfUpdateError, match="guling-trader.exe"):
        await apply.run_update(
            info, on_progress=lambda d, t: None, release_singleton_mutex=lambda: None
        )

    assert download_calls == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/selfupdate/test_apply.py -v -k run_update`
Expected: FAIL —— `AttributeError: module 'trader.selfupdate.apply' has no attribute 'run_update'`

- [ ] **Step 3: 在 `src/trader/selfupdate/apply.py` 追加 `run_update`**

在文件顶部的 import 区替换/追加：

```python
"""guling-trader 自更新执行：下载新 exe → 校验 SHA256 → Windows 重命名自替换 → 拉起新进程 → 退出。"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import aiohttp

from .check import UpdateInfo
from ..installer.download import download_with_progress, verify_sha256

logger = logging.getLogger(__name__)

EXPECTED_EXE_NAME = "guling-trader.exe"

# Windows-only 常量；非 Windows 平台 subprocess 模块没有这两个属性，getattr 兜底避免 import 时炸
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
```

（`SelfUpdateError`、`_parse_sha256_file`、`_swap_files`、`cleanup_orphan_files` 保持 Task 2 写的不动，只是上面这段 import 区要替换掉 Task 2 那个更简单的版本）

文件末尾追加：

```python
async def run_update(
    info: UpdateInfo,
    on_progress: Callable[[int, int], None],
    release_singleton_mutex: Callable[[], None],
) -> None:
    """执行一次完整的自更新。失败抛 SelfUpdateError，调用方负责捕获并反馈 UI。

    成功路径末尾调用 os._exit(0)——正常情况下这个协程不会返回。
    """
    exe_path = Path(sys.executable).resolve()
    if exe_path.name != EXPECTED_EXE_NAME:
        raise SelfUpdateError(
            f"当前可执行文件名 {exe_path.name!r} 与预期 {EXPECTED_EXE_NAME!r} 不符，"
            "可能是开发环境或打包方式变更，放弃自更新"
        )

    new_path = exe_path.parent / (EXPECTED_EXE_NAME + ".new")
    old_path = exe_path.parent / (EXPECTED_EXE_NAME + ".old")

    try:
        await download_with_progress(info.exe_url, new_path, on_progress=on_progress)

        async with aiohttp.ClientSession() as session:
            async with session.get(
                info.sha256_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise SelfUpdateError(f"下载 sha256 校验文件失败：HTTP {resp.status}")
                sha_text = await resp.text()
        expected_sha256 = _parse_sha256_file(sha_text)

        if not await verify_sha256(new_path, expected_sha256):
            raise SelfUpdateError("下载文件 SHA256 校验不匹配，可能下载不完整或被篡改")

        _swap_files(exe_path, new_path, old_path)

    except SelfUpdateError:
        new_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        new_path.unlink(missing_ok=True)
        raise SelfUpdateError(f"下载或替换过程出错：{e}") from e

    release_singleton_mutex()

    subprocess.Popen(
        [str(exe_path)],
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    os._exit(0)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/selfupdate/test_apply.py -v`
Expected: 全部 10 个测试 PASS（Task 2 的 7 个 + 本任务新增的 3 个）

- [ ] **Step 5: Commit**

```bash
git add src/trader/selfupdate/apply.py tests/selfupdate/test_apply.py
git commit -m "feat(selfupdate): run_update 编排——下载+SHA256校验+重命名自替换+重启"
```

---

## Task 4: `main_window.py` —— SharedState 新字段 + 内联横幅 UI

**Files:**
- Modify: `src/trader/main_window.py`
- Test: `tests/test_main_window_state.py`（新建，只测 `SharedState` 数据面，不涉及 Tk 渲染——见下方说明）

**Interfaces:**
- Consumes: `check.UpdateInfo`（渲染时读取 `.tag`/`.current_version`/`.latest_version` 属性）。
- Produces: `SharedState` 新增字段 `self_update_info: Optional[UpdateInfo]`、`self_update_progress: Optional[tuple[int,int]]`、`self_update_status: str`（`"idle"`/`"downloading"`/`"error"`）；`MainWindow.__init__` 新增可选参数 `on_apply_self_update: Optional[Callable[[], None]]`——供 Task 5（`main.py` 的 `MainWindow(...)` 构造调用）传入。

**重要说明（本任务范围边界）**：本项目的 CI（`test.yml`，ubuntu-latest 无 Xvfb）目前**没有任何测试实例化 `tk.Tk()`**——`MainWindow`/Tk 渲染逻辑全仓零自动化覆盖，这是既有约定，不是本次要修的缺口。本任务的自动化测试只覆盖 `SharedState`（纯 dataclass，无 Tk 依赖）的字段读写；`_build_ui`/`_sync_state` 里新增的 Tk widget 创建与显隐逻辑，跟随 Task 6 的真机人工验证一并确认，不写自动化测试（写了在 CI 也跑不起来）。

- [ ] **Step 1: 写失败测试 `tests/test_main_window_state.py`**

```python
"""SharedState 自更新字段测试（不涉及 Tk 渲染，纯数据面）"""


def test_shared_state_self_update_fields_default():
    from trader.main_window import SharedState

    state = SharedState()
    snap = state.snapshot()

    assert snap["self_update_info"] is None
    assert snap["self_update_progress"] is None
    assert snap["self_update_status"] == "idle"


def test_shared_state_self_update_fields_roundtrip():
    from trader.main_window import SharedState
    from trader.selfupdate.check import UpdateInfo

    state = SharedState()
    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    state.update(self_update_info=info, self_update_progress=(10, 100), self_update_status="downloading")
    snap = state.snapshot()

    assert snap["self_update_info"] is info
    assert snap["self_update_info"].latest_version == "0.6.0"
    assert snap["self_update_progress"] == (10, 100)
    assert snap["self_update_status"] == "downloading"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_main_window_state.py -v`
Expected: FAIL —— `KeyError: 'self_update_info'`（`snapshot()` 里还没有这个 key）

- [ ] **Step 3: 修改 `src/trader/main_window.py` —— `SharedState` 加字段**

在 `src/trader/main_window.py:41`（`install_progress` 字段声明）之后加：

```python
    self_update_info: Optional[UpdateInfo] = None  # 检测到的新版本信息，None=无更新
    self_update_progress: Optional[tuple[int, int]] = None  # (done, total)
    self_update_status: str = "idle"  # idle | downloading | error
```

（`main_window.py` 顶部已有 `from __future__ import annotations`（第 9 行），所有注解自动变成延迟求值的字符串，所以这里写 `UpdateInfo` 不需要真的 `import` 它、也不会在模块加载时报 `NameError`——`SharedState` 这个纯数据类因此不需要对 `selfupdate.check` 产生 import 依赖）

在 `src/trader/main_window.py:66`（`snapshot()` 方法里 `"enable_rpa_suite": self.enable_rpa_suite,` 那一行）之后加：

```python
                "self_update_info": self.self_update_info,
                "self_update_progress": self.self_update_progress,
                "self_update_status": self.self_update_status,
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_main_window_state.py -v`
Expected: 2 个测试全部 PASS

- [ ] **Step 5: 修改 `_build_ui`（`src/trader/main_window.py:180` 起）—— 新增横幅 widget**

在 `main_pane.pack(fill="both", expand=True)`（第 183 行）之后、`left_frame = tk.Frame(main_pane, bg="#f6f8fa", width=310)`（第 188 行）之前插入：

```python
        # 【自更新提示横幅】跨两栏，默认隐藏；_sync_state 检测到新版本时显示。
        # 用 before=self.left_frame 固定插回 pack 顺序最上方——pack_forget() 会清除顺序
        # 记忆，重新 pack 若不指定 before/after 会被追加到当前 slave 列表末尾（即
        # left_frame/right_frame 之后），横幅会被挤到没有剩余空间的位置而实际不可见。
        self.self_update_box = tk.Frame(
            main_pane, bg="#fff8e1", highlightbackground="#e0b656", highlightthickness=1
        )

        self.self_update_label = tk.Label(
            self.self_update_box, text="", bg="#fff8e1", fg="#24292f",
            font=("Helvetica", 9, "bold")
        )
        self.self_update_label.pack(side="left", padx=10, pady=6)

        self.self_update_progress_var = tk.DoubleVar(master=self.root, value=0.0)
        self.self_update_progress_bar = ttk.Progressbar(
            self.self_update_box, variable=self.self_update_progress_var,
            maximum=100.0, length=120,
        )
        # 进度条只在 downloading 状态才 pack 出来，见 _sync_state

        self.self_update_skip_btn = tk.Button(
            self.self_update_box, text="跳过", command=self._on_click_self_update_skip,
            relief="flat", bg="#ffffff", fg="#57606a", font=("Helvetica", 8),
            padx=8, pady=2, cursor="hand2", bd=0,
            highlightbackground="#d0d7de", highlightthickness=1
        )
        self.self_update_skip_btn.pack(side="right", padx=(0, 4), pady=6)

        self.self_update_btn = tk.Button(
            self.self_update_box, text="立即更新", command=self._on_click_self_update,
            relief="flat", bg="#e0b656", fg="#ffffff", font=("Helvetica", 8, "bold"),
            padx=8, pady=2, cursor="hand2", bd=0
        )
        self.self_update_btn.pack(side="right", padx=10, pady=6)
        # self_update_box 本身默认不 pack（不加入 main_pane 的显示），子控件的 pack
        # 状态不受影响——_sync_state 检测到新版本时才把 self_update_box 本身 pack 出来
```

紧接着，修改第 188 行 `left_frame = tk.Frame(main_pane, bg="#f6f8fa", width=310)` 之后、`left_frame.pack(...)` 之前，加一行把 `left_frame` 存成实例属性（供上面横幅 `pack(before=...)` 引用）：

```python
        left_frame = tk.Frame(main_pane, bg="#f6f8fa", width=310)
        self.left_frame = left_frame  # 供自更新横幅 pack(before=...) 稳定定位
        left_frame.pack(side="left", fill="both", padx=(12, 6), pady=(12, 4))
```

- [ ] **Step 6: 修改 `_sync_state`（`src/trader/main_window.py:598` 起）—— 追加横幅渲染分支**

在 `_sync_state` 方法末尾（第 712 行，THS 卡片 if/else 块结束之后，`def _drain_log_queue` 之前）追加：

```python

        # 3. 自更新提示横幅状态自适应
        update_info = snap.get("self_update_info")
        update_status = snap.get("self_update_status", "idle")

        if update_info is None:
            if self.self_update_box.winfo_ismapped():
                self.self_update_box.pack_forget()
        else:
            if not self.self_update_box.winfo_ismapped():
                self.self_update_box.pack(fill="x", padx=12, pady=(12, 0), before=self.left_frame)

            if update_status == "downloading":
                done, total = snap.get("self_update_progress") or (0, 0)
                pct = (done / total * 100) if total > 0 else 0
                self.self_update_label.config(text=f"正在更新到 v{update_info.latest_version}...")
                self.self_update_progress_var.set(pct)
                if not self.self_update_progress_bar.winfo_ismapped():
                    self.self_update_progress_bar.pack(side="left", padx=10, pady=6)
                self.self_update_btn.config(state="disabled")
                if self.self_update_skip_btn.winfo_ismapped():
                    self.self_update_skip_btn.pack_forget()
            elif update_status == "error":
                self.self_update_label.config(text="更新失败，可重试或前往 GitHub Releases 手动下载")
                if self.self_update_progress_bar.winfo_ismapped():
                    self.self_update_progress_bar.pack_forget()
                self.self_update_btn.config(state="normal", text="重试更新")
                if not self.self_update_skip_btn.winfo_ismapped():
                    self.self_update_skip_btn.pack(side="right", padx=(0, 4), pady=6)
            else:
                self.self_update_label.config(
                    text=f"发现新版本 v{update_info.latest_version}（当前 v{update_info.current_version}）"
                )
                if self.self_update_progress_bar.winfo_ismapped():
                    self.self_update_progress_bar.pack_forget()
                self.self_update_btn.config(state="normal", text="立即更新")
                if not self.self_update_skip_btn.winfo_ismapped():
                    self.self_update_skip_btn.pack(side="right", padx=(0, 4), pady=6)
```

- [ ] **Step 7: 修改 `MainWindow.__init__`（`src/trader/main_window.py:114` 起）—— 新增回调参数**

```python
    def __init__(
        self,
        state: SharedState,
        on_open_xiadan: Optional[Callable[[], None]] = None,
        on_reset_pair: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
        on_redetect_xiadan: Optional[Callable[[], None]] = None,
        on_set_xiadan_path: Optional[Callable[[str], None]] = None,
        on_apply_self_update: Optional[Callable[[], None]] = None,
        minimize_to_tray: bool = False,
    ):
        self.state = state
        self.on_open_xiadan = on_open_xiadan
        self.on_reset_pair = on_reset_pair
        self.on_exit_cb = on_exit
        self.on_redetect_xiadan = on_redetect_xiadan
        self.on_set_xiadan_path = on_set_xiadan_path
        self.on_apply_self_update = on_apply_self_update
        self._minimize_to_tray = minimize_to_tray
```

- [ ] **Step 8: 新增按钮回调方法**

在 `_toggle_ths_plugin` 方法（`src/trader/main_window.py:519` 附近）之前加：

```python
    def _on_click_self_update(self) -> None:
        if self.on_apply_self_update:
            self.on_apply_self_update()

    def _on_click_self_update_skip(self) -> None:
        self.state.update(self_update_info=None)

```

- [ ] **Step 9: 运行完整测试套件确认没有回归**

Run: `pytest tests/ -v`
Expected: 全部 PASS（新增 2 个 + 之前 Task 1-3 的 17 个 + 项目原有测试）

- [ ] **Step 10: Commit**

```bash
git add src/trader/main_window.py tests/test_main_window_state.py
git commit -m "feat(main_window): SharedState 自更新字段 + 内联横幅 UI（跳过/立即更新按钮）"
```

---

## Task 5: `main.py` —— 接线（mutex 释放 / 启动检查 / 按钮回调 / 孤儿清理）

**Files:**
- Modify: `src/trader/main.py`

**Interfaces:**
- Consumes: Task 1 的 `selfupdate.check.check_for_update`；Task 3 的 `selfupdate.apply.run_update`、`selfupdate.apply.cleanup_orphan_files`；Task 4 的 `MainWindow(..., on_apply_self_update=...)` 构造参数、`SharedState` 新字段。
- Produces: `release_singleton_mutex()`（模块级函数，供 `run_update` 的 `release_singleton_mutex` 参数传入）。

本任务是纯接线，逻辑已在 Task 1-4 覆盖测试；`main.py` 里这部分历来没有自动化测试（`run()`/`_async_main()` 依赖真实 asyncio loop + Windows API + Tk，全仓没有先例），跟随 Task 6 真机验证一并确认，本任务不新增自动化测试文件。

- [ ] **Step 1: 新增 import**

在 `src/trader/main.py:81-83` 处（现有 import 块）之后加两行：

```python
from . import __version__ as _TRADER_VERSION
from .selfupdate import apply as selfupdate_apply, check as selfupdate_check
```

- [ ] **Step 2: 新增 `release_singleton_mutex()`**

在 `src/trader/main.py:471`（`_enforce_single_instance` 函数的 `except Exception: return True` 那行）之后、`def run() -> None:`（第 474 行）之前插入：

```python

def release_singleton_mutex() -> None:
    """自更新替换 exe 前显式释放单例 mutex。

    正常运行期间 mutex 故意不释放（见上方 _SINGLETON_MUTEX 注释，防单例锁失效）；
    这是唯一例外——必须在拉起新进程前释放，否则新进程的 _enforce_single_instance()
    会撞见旧 mutex 还没关，误判"已有实例在跑"而直接退出。
    """
    global _SINGLETON_MUTEX
    if _SINGLETON_MUTEX is None:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_SINGLETON_MUTEX)
    except Exception as e:
        logger.warning("释放单例 mutex 失败（非致命）：%s", e)
    finally:
        _SINGLETON_MUTEX = None

```

- [ ] **Step 3: `_async_main` 里新增 Step 1.5（孤儿文件清理）**

在 `src/trader/main.py:230`（`logger.exception("ensure_xiadan failed")`）之后、`# Step 2: 升级检查`（第 232 行）之前插入：

```python

    # Step 1.5: 自更新孤儿文件清理（上次更新可能留下 .old/.new）
    if platform.system() == "Windows":
        try:
            selfupdate_apply.cleanup_orphan_files(Path(sys.executable).resolve().parent)
        except Exception as e:
            logger.warning("清理自更新孤儿文件失败（非致命）: %s", e)
```

- [ ] **Step 4: `_async_main` 里新增 Step 2.5（自更新检查）**

在 `src/trader/main.py:236`（`logger.warning("升级检查失败: %s", e)`）之后、`# Step 3: 冲突检测`（第 238 行）之前插入：

```python

    # Step 2.5: guling-trader 自身更新检查（GitHub Releases，仅 Windows）
    if platform.system() == "Windows":
        try:
            update_info = await selfupdate_check.check_for_update(_TRADER_VERSION)
            if update_info:
                state.update(self_update_info=update_info, self_update_status="idle")
                state.log(f"发现 guling-trader 新版本：v{update_info.latest_version}")
        except Exception as e:
            logger.warning("guling-trader 自更新检查失败: %s", e)
```

- [ ] **Step 5: `run()` 里新增「立即更新」按钮回调闭包**

在 `src/trader/main.py` 的 `on_reset_pair` 函数定义结束之后、`on_main_exit` 函数定义（约第 599 行附近）之前插入：

```python
    def on_apply_self_update() -> None:
        """MainWindow「立即更新」按钮回调（主线程触发）：把 apply.run_update 丢给后台 loop 跑。"""
        snap = state.snapshot()
        if snap.get("self_update_status") == "downloading":
            return  # 防重入：已经在下载中，忽略重复点击
        info = snap.get("self_update_info")
        if info is None:
            return

        loop = _ws_client_holder.get("loop")
        if loop is None:
            state.log("⚠ 无法启动更新：后台任务队列未就绪，请稍后重试")
            return

        state.update(self_update_status="downloading", self_update_progress=(0, 0))

        def on_progress(done: int, total: int) -> None:
            state.update(self_update_progress=(done, total))

        async def _run() -> None:
            try:
                await selfupdate_apply.run_update(info, on_progress, release_singleton_mutex)
            except Exception as e:
                logger.warning("guling-trader 自更新失败: %s", e)
                state.log(f"⚠ 自更新失败：{e}")
                state.update(self_update_status="error")

        asyncio.run_coroutine_threadsafe(_run(), loop)

```

- [ ] **Step 6: `MainWindow(` 构造调用处传入新回调**

修改 `src/trader/main.py` 里的 `MainWindow(` 调用（约第 624-632 行）：

```python
    mw = MainWindow(
        state=state,
        on_open_xiadan=on_open_xiadan,
        on_reset_pair=on_reset_pair,
        on_exit=on_main_exit,
        on_redetect_xiadan=on_redetect_xiadan,
        on_set_xiadan_path=on_set_xiadan_path,
        on_apply_self_update=on_apply_self_update,
        minimize_to_tray=(platform.system() == "Windows"),
    )
```

- [ ] **Step 7: 语法/import 自检**

Run: `python -c "import ast; ast.parse(open('src/trader/main.py').read())"`
Expected: 无输出（语法通过）

Run: `python -m py_compile src/trader/main.py src/trader/main_window.py src/trader/selfupdate/check.py src/trader/selfupdate/apply.py`
Expected: 无输出（编译通过，import 链路无循环导入错误——注意这一步在 macOS/Linux 上跑，`pywin32`/`ctypes.windll` 相关代码不会被执行到，只做语法与顶层 import 检查；`ctypes.windll` 本身在非 Windows 上 import `ctypes` 没问题，只有真正执行 `ctypes.windll.xxx` 才会在非 Windows 报错，而这些调用都在 `platform.system() == "Windows"` 分支或 try/except 里）

- [ ] **Step 8: 运行完整测试套件**

Run: `pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add src/trader/main.py
git commit -m "feat(main): 接入自更新——启动检查+孤儿清理+按钮回调+mutex释放"
```

---

## Task 6: 真机人工验证（非自动化，发布前必做）

**Files:** 无代码改动，纯验证清单。

设计文档已明确：自替换的真实正确性（Windows 重命名正在运行的 exe、`_enforce_single_instance` 的 mutex 时序、Tk 横幅实际渲染效果）依赖真机验证，ROI 低的端到端自动化不做。发布前在 Windows 真机上按此清单过一遍：

- [ ] **验证 1：横幅正确显示**
  临时把本地 `trader/__init__.py` 的 `__version__` 改成一个比线上 Release 更旧的版本号（如 `"0.1.0"`），启动 `guling-trader.exe`（或 `python -m trader.main`），确认主窗口顶部出现横幅："发现新版本 v{latest}（当前 v0.1.0）"，横幅在 left_frame/right_frame 上方、不遮挡任何现有卡片。

- [ ] **验证 2：「跳过」按钮**
  点「跳过」，确认横幅消失；重启程序后横幅重新出现（验证"不持久化，下次启动重新检查"）。

- [ ] **验证 3：「立即更新」完整流程**
  点「立即更新」，确认：横幅切换成下载中态（进度条+百分比滚动）、「立即更新」按钮变灰不可再点；下载完成后进程自己退出、新窗口自动弹出；新窗口标题栏/关于信息里的版本号已是更新后的版本；同目录下 `guling-trader.exe.old` 存在（本次启动会在下次运行时自动清理）。

- [ ] **验证 4：`.old` 孤儿清理**
  验证 3 之后再重启一次 `guling-trader.exe`，确认 `guling-trader.exe.old` 已被自动删除。

- [ ] **验证 5：重复点击防重入**
  在下载中间快速多次点击「立即更新」（此时按钮应已置灰不可点——如果仍可点击说明 Task 4 Step 6 的 `state="disabled"` 没生效，需回查），确认不会触发第二次并发下载/替换。

- [ ] **验证 6：网络失败场景**
  临时断网后启动程序，确认启动过程不受影响（无异常弹窗、无卡顿），日志里能看到"检查 guling-trader 更新失败"或类似 warning。

- [ ] **验证 7：新旧版本 xiadan 皮肤 + 自更新互不干扰**（顺带回归）
  确认本次改动没有影响到既有的新版/旧版 xiadan 兼容路径（Step 1/Step 1.5/Step 2/Step 2.5 之间用独立 try/except 隔离，理论上互不影响，真机上过一遍确认没有引入意外耦合）。

---

## Post-Implementation

全部 Task 1-5 完成、测试全绿、Task 6 真机验证通过后：
- 更新 `docs/ths_architecture.md`（如果该文档有维护"启动流程 Step 列表"的章节，把新增的 Step 1.5/2.5 补进去——检查该文档现状，若无此类流程图则跳过，不强行加）
- 正常走下一次 `chore(release): vX.Y.Z` 流程（改 4 处版本号 + tag + push），本功能随下一个版本一起发布，不需要单独出一个版本。
