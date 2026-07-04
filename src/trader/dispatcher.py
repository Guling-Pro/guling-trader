"""RPC 分派：call frame → backend method → reply frame"""
import json
import logging
from pathlib import Path
from typing import Any, Optional

from .ths.win import WinThsBackend

logger = logging.getLogger(__name__)

# Fallback tools schema in case the external JSON file cannot be found (e.g., in a packaged PyInstaller environment)
FALLBACK_TOOLS_SCHEMA = {
  "tools": [
    {
      "name": "balance",
      "description": "查询资金账户余额（包括资金余额、可用资金、可取资金、股票市值、总资产、当日盈亏等）。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "position",
      "description": "查询当前股票持仓。返回持仓列表，包含证券代码、证券名称、股票余额、可用余额、成本价、市价、盈亏等字段。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "orders_active",
      "description": "查询当日未成交的委托单列表（可用于撤单），包含委托编号(entrust_no)、证券代码、证券名称、委托数量、委托价格、委托方向、委托状态等字段。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "orders_filled",
      "description": "查询当日已成交的委托单历史记录，包含委托编号、成交编号、证券代码、证券名称、成交数量、成交均价、成交金额等字段。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "settlement",
      "description": "查询交割单（历史成交结算记录，含更完整的日期、代码、名称、操作、数量、均价、金额、发生金额、手续费、印花税等）。数据量可能较大，适合偶尔做整体盈亏/交易复盘分析。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "date_range": {
            "type": "string",
            "description": "查询的时间跨度，可选值：近一周、近一月、近三月、近一年；默认近一年",
            "enum": ["近一周", "近一月", "近三月", "近一年"],
            "default": "近一年"
          }
        },
        "additionalProperties": False
      }
    },
    {
      "name": "watchlist",
      "description": "查询自选股列表（证券代码）。返回同花顺自选股当前顶部可见的代码列表；按同花顺习惯，最新加入的自选股出现在顶部。注意：受限于客户端渲染，仅返回第一屏顶部部分（非全量，返回中 partial=true）。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "buy",
      "description": "下买入委托单。**会真实下单**，慎重调用。不传 price=五档即成剩撤市价单(立即成交、剩余自动撤销、无残留挂单)，回执 status/filled_amount/avg_price 为实际成交；传 price=限价挂单，返回 entrust_no，未成交需自行用 orders_active+cancel 管理。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "stock_no": {
            "type": "string",
            "description": "6位数字股票代码，如 600000"
          },
          "amount": {
            "type": "integer",
            "description": "买入股数（必须为 100 股的整数倍）"
          },
          "price": {
            "type": "number",
            "description": "限价买入价格。不传则走同花顺市价委托(五档即成剩撤)立即成交、剩余自动撤销、无残留挂单；传则限价挂单，需自行 orders_active/cancel 管理。"
          },
          "client_order_id": {
            "type": "string",
            "description": "可选的客户端自定义订单 ID"
          }
        },
        "required": ["stock_no", "amount"],
        "additionalProperties": False
      }
    },
    {
      "name": "sell",
      "description": "下卖出委托单。**会真实下单**，慎重调用。不传 price=五档即成剩撤市价单(立即成交、剩余自动撤销、无残留挂单)，回执 status/filled_amount/avg_price 为实际成交；传 price=限价挂单，返回 entrust_no，未成交需自行用 orders_active+cancel 管理。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "stock_no": {
            "type": "string",
            "description": "6位数字股票代码，如 600000"
          },
          "amount": {
            "type": "integer",
            "description": "卖出股数"
          },
          "price": {
            "type": "number",
            "description": "限价卖出价格。不传则走同花顺市价委托(五档即成剩撤)立即成交、剩余自动撤销、无残留挂单；传则限价挂单，需自行 orders_active/cancel 管理。"
          },
          "client_order_id": {
            "type": "string",
            "description": "可选的客户端自定义订单 ID"
          }
        },
        "required": ["stock_no", "amount"],
        "additionalProperties": False
      }
    },
    {
      "name": "cancel",
      "description": "撤销指定委托编号的未成交订单。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "entrust_no": {
            "type": "string",
            "description": "要撤销的委托编号（从 orders_active 中获取）"
          }
        },
        "required": ["entrust_no"],
        "additionalProperties": False
      }
    }
  ]
}

from . import config as _config

def load_tools_schema() -> dict[str, Any]:
    """尝试从 docs/tools_schema.json 加载工具定义，如失败则使用内置 Fallback 保证打包后的 .exe 也能正常运行"""
    cfg = _config.load()
    schema = None
    try:
        # __file__ 是 src/trader/dispatcher.py，项目根目录是其三级父目录
        root = Path(__file__).resolve().parent.parent.parent
        schema_path = root / "docs" / "tools_schema.json"
        if schema_path.exists():
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
    except Exception as e:
        logger.warning("从文件系统加载 tools_schema.json 失败（可能在 PyInstaller 打包环境中运行）：%s", e)
    
    if schema is None:
        schema = json.loads(json.dumps(FALLBACK_TOOLS_SCHEMA))

    if not cfg.enable_ths_plugin:
        trading_names = {"balance", "position", "orders_active", "orders_filled", "settlement", "watchlist", "buy", "sell", "cancel"}
        schema["tools"] = [t for t in schema["tools"] if t.get("name") not in trading_names]

    return schema

METHOD_WHITELIST = {
    "tools/list",
    "balance",
    "position",
    "orders_active",
    "orders_filled",
    "settlement",
    "watchlist",
    "buy",
    "sell",
    "cancel",
}


async def handle_call(
    frame: dict[str, Any],
    backend: WinThsBackend,
) -> dict[str, Any]:
    """处理 RPC call 帧，返回 reply 帧"""
    frame_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params", {})

    reply = {"type": "reply", "id": frame_id}

    if method not in METHOD_WHITELIST:
        reply["ok"] = False
        reply["error"] = f"方法 '{method}' 不支持"
        return reply

    if method == "tools/list":
        logger.info("[RPC] method=tools/list, frame_id=%s", frame_id)
        schema = load_tools_schema()
        reply["ok"] = True
        reply["result"] = {"tools": schema.get("tools", [])}
        return reply

    # 针对插件禁用状态的请求拦截
    cfg = _config.load()
    trading_methods = {
        "balance",
        "position",
        "orders_active",
        "orders_filled",
        "settlement",
        "watchlist",
        "buy",
        "sell",
        "cancel",
    }
    if method in trading_methods and not cfg.enable_ths_plugin:
        reply["ok"] = False
        reply["error"] = "同花顺实盘交易插件已被禁用，请在客户端界面中开启该插件模块！"
        return reply

    # 串行化 THS 单窗口访问：order_watch 轮询与下单/查询共用 backend.win_lock。
    needs_window = method in trading_methods
    if needs_window:
        await backend.win_lock.acquire()
    try:
        if method == "balance":
            logger.info("[RPC] method=balance, frame_id=%s", frame_id)
            result = await backend.balance()
            logger.info("[RPC] balance → code=%s", result.get("code"))
        elif method == "position":
            result = await backend.position()
        elif method == "orders_active":
            result = await backend.orders_active()
        elif method == "orders_filled":
            result = await backend.orders_filled()
        elif method == "settlement":
            result = await backend.settlement(params.get("date_range", "近一年"))
        elif method == "watchlist":
            result = await backend.watchlist()
        elif method == "buy":
            stock_no = params.get("stock_no")
            amount = params.get("amount")
            price = params.get("price")
            client_order_id = params.get("client_order_id")
            result = await backend.buy(stock_no, amount, price, client_order_id)
            _eno = (result or {}).get("entrust_no")
            if _eno:
                backend.agent_entrust_nos.add(str(_eno))
        elif method == "sell":
            stock_no = params.get("stock_no")
            amount = params.get("amount")
            price = params.get("price")
            client_order_id = params.get("client_order_id")
            result = await backend.sell(stock_no, amount, price, client_order_id)
            _eno = (result or {}).get("entrust_no")
            if _eno:
                backend.agent_entrust_nos.add(str(_eno))
        elif method == "cancel":
            entrust_no = params.get("entrust_no")
            result = await backend.cancel(entrust_no)
        else:
            result = {"code": 1, "error": "内部错误"}

        if not isinstance(result, dict):
            reply["ok"] = False
            reply["error"] = "未知错误"
            return reply

        code = result.get("code")
        if code == 0:
            reply["ok"] = True
            reply["result"] = result
        else:
            reply["ok"] = False
            # 透传后端的 code/status/msg，让上层能区分"已提交未确认(code=2)"和真失败，
            # 而不是把一切塌缩成"未知错误"。
            reply["result"] = result
            if code == 2:
                # 委托已提交但未能在委托列表回查确认（验证码/刷新延迟常见）。
                # 这不是下单失败——必须明确告知，避免上层重复下单造成双倍成交。
                reply["error"] = (
                    result.get("msg")
                    or "委托可能已提交但未确认，请勿重复下单，需人工或查询确认状态"
                )
            else:
                reply["error"] = (
                    result.get("error") or result.get("msg") or "未知错误"
                )

    except Exception as e:
        logger.error("处理 RPC '%s' 出错：%s", method, e)
        reply["ok"] = False
        reply["error"] = str(e)
    finally:
        if needs_window:
            backend.win_lock.release()

    return reply
