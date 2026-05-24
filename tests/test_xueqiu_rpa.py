# -*- coding: utf-8 -*-

"""
Xueqiu RPA Suite Regression Tests (Red-Green Testing)
Uses pytest and unittest.mock to test routing, schema injection, and CDP execution paths.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from trader import config, dispatcher
from trader.rpa.xueqiu import XueqiuRpaBackend


# ==========================================
# 🔴 RED STAGE: 验证未启用 RPA Suite 时的保护机制
# ==========================================

def test_rpa_suite_disabled_by_default():
    """验证默认配置下，RPA Suite 是关闭的，且 tools_schema 中不包含发帖工具"""
    cfg = config.load()
    assert cfg.enable_rpa_suite is False

    schema = dispatcher.load_tools_schema()
    tool_names = [t["name"] for t in schema.get("tools", [])]
    assert "xueqiu_publish_review" not in tool_names


@pytest.mark.asyncio
async def test_rpa_call_rejected_when_disabled():
    """验证当未开启配置开关时，dispatcher 会直接拦截拒绝调用"""
    # 模拟 enable_rpa_suite 为 False
    mock_config = config.TraderConfig(device_id="test", enable_rpa_suite=False)
    
    with patch("trader.config.load", return_value=mock_config):
        frame = {
            "type": "call",
            "id": "1",
            "method": "xueqiu_publish_review",
            "params": {"content": "今天实战交易良好"}
        }
        mock_backend = MagicMock()
        
        reply = await dispatcher.handle_call(frame, mock_backend)
        assert reply["ok"] is False
        assert "RPA 模块未启用" in reply["error"]


# ==========================================
# 🟢 GREEN STAGE: 验证启用 RPA Suite 后的正常加载与流程
# ==========================================

def test_rpa_suite_schema_injection_when_enabled():
    """验证开启 enable_rpa_suite 之后，tools_schema.json 动态成功注入新工具"""
    mock_config = config.TraderConfig(device_id="test", enable_rpa_suite=True)
    
    with patch("trader.config.load", return_value=mock_config):
        schema = dispatcher.load_tools_schema()
        tool_names = [t["name"] for t in schema.get("tools", [])]
        assert "xueqiu_publish_review" in tool_names


@pytest.mark.asyncio
@patch("trader.rpa.xueqiu.CdpConnection")
@patch("trader.rpa.xueqiu.get_or_create_tab")
async def test_rpa_publish_semi_manual_success(mock_get_tab, mock_cdp_conn_class):
    """验证在半人工确认模式下下发发帖指令，能顺利拉起浏览器并成功注入 DOM，返回等待确认状态"""
    # 模拟已启动的 CDP 链接信息
    mock_get_tab.return_value = "ws://localhost:9222/devtools/page/test-tab-id"
    
    # 模拟 CDP 连接实例及其返回
    mock_conn = MagicMock()
    mock_conn.connect = AsyncMock()
    mock_conn.close = AsyncMock()
    
    # execute_js 顺序调用：
    # 1. 检查是否登录 (is_logged_in -> True)
    # 2. 注入文本文案 (fill_ok -> True)
    # 3. 半人工高亮发帖按钮 (highlight_ok -> True)
    mock_conn.execute_js = AsyncMock()
    mock_conn.execute_js.side_effect = [True, True, True]
    
    mock_cdp_conn_class.return_value = mock_conn

    # 执行发帖业务
    backend = XueqiuRpaBackend()
    res = await backend.publish_review(content="A股量化实盘同步交易日志", semi_manual=True)
    
    # 断言连接建立与关闭的完整生命周期
    mock_get_tab.assert_called_once_with("xueqiu.com", "https://xueqiu.com", 9222)
    mock_conn.connect.assert_called_once()
    mock_conn.close.assert_called_once()
    
    # 断言成功状态
    assert res["code"] == 0
    assert res["status"] == "pending_manual_click"
    assert "红色脉冲呼吸灯" in res["msg"]


@pytest.mark.asyncio
@patch("trader.rpa.xueqiu.CdpConnection")
@patch("trader.rpa.xueqiu.get_or_create_tab")
async def test_rpa_publish_full_auto_success(mock_get_tab, mock_cdp_conn_class):
    """验证在全自动模式下发帖，能顺利执行并在后台自动模拟点击完成"""
    mock_get_tab.return_value = "ws://localhost:9222/devtools/page/test-tab-id"
    
    mock_conn = MagicMock()
    mock_conn.connect = AsyncMock()
    mock_conn.close = AsyncMock()
    
    # 1. 检查是否登录 (True)
    # 2. 注入文案 (True)
    # 3. 自动模拟点击 (True)
    mock_conn.execute_js = AsyncMock()
    mock_conn.execute_js.side_effect = [True, True, True]
    
    mock_cdp_conn_class.return_value = mock_conn

    backend = XueqiuRpaBackend()
    res = await backend.publish_review(content="A股量化自动下单复盘记录", semi_manual=False)
    
    assert res["code"] == 0
    assert res["status"] == "success"
    assert "已成功通过浏览器 CDP 自动发布" in res["msg"]


@pytest.mark.asyncio
@patch("trader.rpa.xueqiu.CdpConnection")
@patch("trader.rpa.xueqiu.get_or_create_tab")
async def test_rpa_publish_not_logged_in(mock_get_tab, mock_cdp_conn_class):
    """验证当页面未检测到登录发帖框时，程序能优雅拦截并提示用户先完成手动登录"""
    mock_get_tab.return_value = "ws://localhost:9222/devtools/page/test-tab-id"
    
    mock_conn = MagicMock()
    mock_conn.connect = AsyncMock()
    mock_conn.close = AsyncMock()
    
    # 1. 检查是否登录 (返回 False 表示未检测到登录)
    mock_conn.execute_js = AsyncMock()
    mock_conn.execute_js.return_value = False
    
    mock_cdp_conn_class.return_value = mock_conn

    backend = XueqiuRpaBackend()
    res = await backend.publish_review(content="测试未登录抛出", semi_manual=True)
    
    assert res["code"] == 1
    assert "未检测到雪球登录态" in res["error"]
