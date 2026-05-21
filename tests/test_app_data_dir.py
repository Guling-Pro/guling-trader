"""便携式数据目录 + OCR 临时目录回归测试。

用户反馈：exe 同目录散落 ocr.png/ocr_proc.png + 配置丢系统目录，无法干净删除/重测。
修复目标：frozen 时所有数据收拢到 exe 同级 guling-trader-data/，OCR 临时图进 tmp/，
且旧系统目录的 config.json 一次性迁移过来（不丢配对）。
"""
import importlib
import json
import sys

import pytest


def _reload_config():
    from trader import config
    return importlib.reload(config)


def test_frozen_uses_exe_sibling_dir(monkeypatch, tmp_path):
    exe = tmp_path / "guling-trader.exe"
    exe.write_text("x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    config = _reload_config()
    base = config.app_data_dir()
    assert base == tmp_path / "guling-trader-data"
    assert base.is_dir()
    assert config.tmp_dir() == base / "tmp"
    assert config.tmp_dir().is_dir()


def test_non_frozen_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config = _reload_config()
    base = config.app_data_dir()
    # 非 frozen：不在 exe 同级，而在系统配置目录
    assert base != tmp_path / "guling-trader-data"
    assert base.is_dir()


def test_legacy_config_migrated_on_frozen(monkeypatch, tmp_path):
    # 旧系统目录有 config.json（带 agent_token），新位置没有 → 应迁移
    appdata = tmp_path / "appdata"
    legacy_dir = appdata / "guling-trader"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "config.json").write_text(
        json.dumps({"device_id": "dev-1", "agent_token": "tok-abc"}), encoding="utf-8"
    )

    exe = tmp_path / "guling-trader.exe"
    exe.write_text("x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(appdata))

    config = _reload_config()
    cfg = config.load()
    assert cfg.device_id == "dev-1"
    assert cfg.agent_token == "tok-abc"   # 配对没丢
    assert (tmp_path / "guling-trader-data" / "config.json").exists()


def test_setup_routes_ocr_temp_to_work_dir(monkeypatch, tmp_path):
    from trader.ths import win
    work = tmp_path / "tmp"
    win.setup("网上股票交易系统5.0", "", str(work))
    assert win.work_dir == str(work)
    assert work.is_dir()


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    # 测试结尾恢复 config 模块到真实状态，避免 reload 污染其他测试
    importlib.reload(importlib.import_module("trader.config"))
