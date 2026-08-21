"""C7 契约即测试：规范件（PROTOCOL.md / tools_schema.json）与实现不许漂移。

冻结的前提是漂移可检测——否则「很久不改」等于「坏了很久没人知道」。
"""
import json
from pathlib import Path

import pytest

from trader import contract
from trader.dispatcher import CLIENT_ORDER_ID_PATTERN, FALLBACK_TOOLS_SCHEMA, METHOD_WHITELIST
from trader.ths import rows

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = (ROOT / "docs/PROTOCOL.md").read_text("utf-8")
SCHEMA = json.loads((ROOT / "docs/tools_schema.json").read_text("utf-8"))

CODES = [v for k, v in vars(contract).items() if k.startswith("CODE_")]
CLASSES = [v for k, v in vars(contract).items() if k.startswith("CLS_")]


@pytest.mark.parametrize("code", CODES)
def test_every_code_is_documented(code):
    assert f"`{code}`" in PROTOCOL, f"code={code} 未写进 PROTOCOL.md"


@pytest.mark.parametrize("cls", CLASSES)
def test_every_error_class_is_documented(cls):
    assert f"`{cls}`" in PROTOCOL, f"error.class={cls} 未写进 PROTOCOL.md"


@pytest.mark.parametrize("state", rows.ORDER_STATES)
def test_every_order_state_is_documented(state):
    assert state in PROTOCOL, f"委托状态 {state} 未写进 PROTOCOL.md"


def test_contract_version_is_consistent():
    assert SCHEMA["contract_version"] == contract.CONTRACT_VERSION
    assert f'"{contract.CONTRACT_VERSION}"' in PROTOCOL
    # 网关侧同一常量（Go）——三处不同步就打红
    gateway_hub = ROOT.parent.parent.parent / "guling-mcp-gateway/gateway/hub.go"
    if gateway_hub.exists():
        assert f'ContractVersion = "{contract.CONTRACT_VERSION}"' in gateway_hub.read_text("utf-8")


def test_schema_tools_match_method_whitelist():
    schema_names = {t["name"] for t in SCHEMA["tools"]}
    exposed = METHOD_WHITELIST - {"tools/list"}
    assert schema_names == exposed, "tools_schema.json 与 METHOD_WHITELIST 不一致"


def test_fallback_schema_matches_disk_verbatim():
    """打包环境用 FALLBACK，必须与磁盘规范件逐字一致。"""
    assert FALLBACK_TOOLS_SCHEMA == SCHEMA


def test_query_order_is_exposed():
    assert "query_order" in {t["name"] for t in SCHEMA["tools"]}


@pytest.mark.parametrize("name", ["buy", "sell", "cancel"])
def test_order_tools_document_idempotency(name):
    tool = next(t for t in SCHEMA["tools"] if t["name"] == name)
    prop = tool["inputSchema"]["properties"]["client_order_id"]
    desc = prop["description"]
    assert "幂等" in desc and "重发" in desc
    assert "client_order_id" in tool["inputSchema"]["required"]
    assert prop["pattern"] == CLIENT_ORDER_ID_PATTERN
    assert "UUID v7" in desc


def test_protocol_pins_canonical_ids_and_one_shot_auto_query():
    assert "gl-<小写 UUID v7>" in PROTOCOL
    assert "data.auto_query" in PROTOCOL
    assert "绝不自动重发下单" in PROTOCOL


def test_protocol_states_failed_is_not_not_submitted():
    """最容易被误读的一条语义必须白纸黑字在协议里。"""
    assert "不等于**「未提交」" in PROTOCOL or "不等于「未提交」" in PROTOCOL
    assert "禁止改单重下" in PROTOCOL


def test_protocol_pins_empty_table_semantics():
    assert "空表是成功" in PROTOCOL
    assert "绝不返回空数组冒充" in PROTOCOL
