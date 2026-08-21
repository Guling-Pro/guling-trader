"""工具描述：市价/限价两条路径语义 + FALLBACK_TOOLS_SCHEMA 与 tools_schema.json 同步。"""
import json
from pathlib import Path

from trader.dispatcher import FALLBACK_TOOLS_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def _tool(tools, name):
    return next(t for t in tools if t["name"] == name)


def test_buy_sell_desc_mentions_market_and_limit():
    for name in ("buy", "sell"):
        t = _tool(FALLBACK_TOOLS_SCHEMA["tools"], name)
        price_desc = t["inputSchema"]["properties"]["price"]["description"]
        assert "五档即成剩撤" in price_desc
        assert "限价" in price_desc
        # 旧文案不得残留
        assert "对手价市价单" not in price_desc


def test_fallback_matches_tools_schema_json():
    disk = json.loads((ROOT / "docs/tools_schema.json").read_text("utf-8"))
    for name in ("buy", "sell", "cancel"):
        code_schema = _tool(FALLBACK_TOOLS_SCHEMA["tools"], name)["inputSchema"]
        disk_schema = _tool(disk["tools"], name)["inputSchema"]
        assert code_schema == disk_schema
