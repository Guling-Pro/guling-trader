"""工具描述：市价/限价两条路径语义 + FALLBACK_TOOLS_SCHEMA 与 tools_schema.json 同步。"""
import json
from pathlib import Path

from trader.dispatcher import FALLBACK_TOOLS_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def _tool(tools, name):
    return next(t for t in tools if t["name"] == name)


def test_buy_sell_schema_requires_explicit_order_type():
    for name in ("buy", "sell"):
        t = _tool(FALLBACK_TOOLS_SCHEMA["tools"], name)
        schema = t["inputSchema"]
        assert schema["properties"]["order_type"]["enum"] == [
            "LIMIT", "FIVE_LEVEL_IOC"
        ]
        assert "order_type" in schema["required"]
        assert "order_type" in t["description"]
        price_desc = schema["properties"]["price"]["description"]
        assert "LIMIT" in price_desc
        assert "禁止传入" in price_desc
        assert "对手价市价单" not in price_desc


def test_fallback_matches_tools_schema_json():
    disk = json.loads((ROOT / "docs/tools_schema.json").read_text("utf-8"))
    for name in ("buy", "sell", "cancel", "confirm_external_cancel"):
        code_schema = _tool(FALLBACK_TOOLS_SCHEMA["tools"], name)["inputSchema"]
        disk_schema = _tool(disk["tools"], name)["inputSchema"]
        assert code_schema == disk_schema
