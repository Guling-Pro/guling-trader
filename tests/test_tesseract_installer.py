"""installer.tesseract 自动安装回归测试。

锁定 PH-061 迁移丢失的 Tesseract 接通逻辑（旧 server.py 调 thsauto_setup 喂
tesseract_cmd；迁移后丢了 → 验证码识别静默失效），以及新增的 winget 自动安装：
- 已装 → 不调 winget，直接返回路径/""
- 未装 + 无 winget → 优雅返回 None
- 未装 + winget 安装后检测到 → 返回路径

用 monkeypatch 假冒 platform/shutil.which/asyncio 子进程，纯逻辑、不碰真实系统。
"""
import asyncio

import pytest

from trader.installer import tesseract as tess


class _FakeProc:
    def __init__(self, out=b"", rc=0):
        self._out = out
        self.returncode = rc

    async def communicate(self):
        return self._out, b""


def _force_windows(monkeypatch):
    monkeypatch.setattr(tess.platform, "system", lambda: "Windows")


def test_already_on_path_skips_winget(monkeypatch):
    _force_windows(monkeypatch)
    monkeypatch.setattr(tess.shutil, "which", lambda name: "C:/x/tesseract.exe" if name == "tesseract" else None)

    called = {"winget": False}

    async def _no_subprocess(*a, **k):
        called["winget"] = True
        return _FakeProc()

    monkeypatch.setattr(tess.asyncio, "create_subprocess_exec", _no_subprocess)
    result = asyncio.run(tess.ensure_tesseract())
    assert result == ""              # 在 PATH 上
    assert called["winget"] is False  # 不该调 winget


def test_found_in_common_path_skips_winget(monkeypatch, tmp_path):
    _force_windows(monkeypatch)
    monkeypatch.setattr(tess.shutil, "which", lambda name: None)
    fake_exe = tmp_path / "tesseract.exe"
    fake_exe.write_text("x")
    monkeypatch.setattr(tess, "TESSERACT_COMMON_PATHS", [str(fake_exe)])
    result = asyncio.run(tess.ensure_tesseract())
    assert result == str(fake_exe)


def test_missing_and_no_winget_returns_none(monkeypatch):
    _force_windows(monkeypatch)
    # tesseract 未装、winget 也不存在
    monkeypatch.setattr(tess.shutil, "which", lambda name: None)
    monkeypatch.setattr(tess, "TESSERACT_COMMON_PATHS", [])
    logs = []
    result = asyncio.run(tess.ensure_tesseract(on_log=logs.append))
    assert result is None
    assert any("winget" in m for m in logs)


def test_winget_installs_then_detected(monkeypatch, tmp_path):
    _force_windows(monkeypatch)
    fake_exe = tmp_path / "tesseract.exe"
    monkeypatch.setattr(tess, "TESSERACT_COMMON_PATHS", [str(fake_exe)])

    state = {"installed": False}

    def which(name):
        if name == "winget":
            return "C:/winget.exe"
        return None  # tesseract 始终不在 PATH

    monkeypatch.setattr(tess.shutil, "which", which)

    async def fake_subprocess(*args, **kwargs):
        # 模拟 winget 把文件装到 common path
        fake_exe.write_text("installed")
        state["installed"] = True
        return _FakeProc(out=b"Successfully installed", rc=0)

    monkeypatch.setattr(tess.asyncio, "create_subprocess_exec", fake_subprocess)
    logs = []
    result = asyncio.run(tess.ensure_tesseract(on_log=logs.append))
    assert state["installed"] is True
    assert result == str(fake_exe)
    assert any("完成" in m for m in logs)


def test_winget_runs_but_still_missing_returns_none(monkeypatch):
    _force_windows(monkeypatch)
    monkeypatch.setattr(tess.shutil, "which", lambda name: "C:/winget.exe" if name == "winget" else None)
    monkeypatch.setattr(tess, "TESSERACT_COMMON_PATHS", [])

    async def fake_subprocess(*args, **kwargs):
        return _FakeProc(out=b"failed", rc=1)

    monkeypatch.setattr(tess.asyncio, "create_subprocess_exec", fake_subprocess)
    result = asyncio.run(tess.ensure_tesseract())
    assert result is None


def test_non_windows_returns_none(monkeypatch):
    monkeypatch.setattr(tess.platform, "system", lambda: "Darwin")
    result = asyncio.run(tess.ensure_tesseract())
    assert result is None


def test_verify_ocr_runnable_ok(monkeypatch):
    import pytesseract
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.3.3")
    ok, info = tess.verify_ocr_runnable()
    assert ok is True
    assert "5.3.3" in info


def test_verify_ocr_runnable_failure(monkeypatch):
    import pytesseract

    def _boom():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", _boom)
    ok, info = tess.verify_ocr_runnable()
    assert ok is False
    assert info  # 有错误原因
