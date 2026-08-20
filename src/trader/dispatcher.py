"""RPC 分派：call frame → backend method → reply frame

契约 v2：backend 返回的已经是统一信封（见 contract.py），dispatcher 只负责
①幂等台账（C5a）②查单（C5b）③busy/超时这两种「还没进 backend」的信封 ④装进 reply 帧。
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from . import contract
from .order_ledger import LedgerUnavailable
from .ths.win import WinThsBackend

logger = logging.getLogger(__name__)

# 受控端单笔调用总预算：必须低于网关侧 30s 超时，保证网关永远等得到带
# unknown 语义的 reply，而不是自造裸错误（-32003）。
CALL_TIMEOUT_SECS = 25.0
# win_lock 排队上限：持锁方被拖住时，排队方回 busy 而非无限饿死。
LOCK_TIMEOUT_SECS = 5.0
# 会真实改变账户状态的方法：超时/busy 回执必须带「可能已提交，先核单」语义。
ORDER_METHODS = {"buy", "sell", "cancel"}
# 走 client_order_id 幂等台账的方法（C5a）。
IDEMPOTENT_METHODS = {"buy", "sell", "cancel"}
# busy 是背压信号：告诉调用方等多久再来，别让它自己猜（G3）。
BUSY_BACKOFF_HINT_SECS = 3


def _resolve_client_order_id(method: str, params: dict[str, Any], frame_id: Any) -> Optional[str]:
    """返回调用方指定的幂等键，或由稳定 RPC id 派生一个。

    MCP 调用方通常不会管理交易幂等键。网关会把原始 RPC ``id`` 原样转发，
    因而以它派生的键能让同一 RPC 帧的重发命中同一条本地台账记录。没有
    有效 RPC id 时绝不能随机生成，否则丢回执后的重发仍可能变成第二笔订单。
    """
    explicit = params.get("client_order_id")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    if isinstance(frame_id, bool) or frame_id is None:
        return None
    rpc_id = str(frame_id).strip()
    return f"rpc:{method}:{rpc_id}" if rpc_id else None

# Fallback tools schema in case the external JSON file cannot be found (e.g., in a packaged PyInstaller environment)
FALLBACK_TOOLS_SCHEMA = {
  "$schema": "https://json-schema.org/draft/2020-12",
  "version": "1.0.0",
  "tools": [
    {
      "name": "balance",
      "description": "查询资金账户余额。data 为 number 字段：资金余额/冻结金额/可用金额/可取金额/股票市值/总资产/持仓盈亏/当日盈亏（单位元），当日盈亏比_pct（百分比数值）。缺值为 null（不是 0）。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "position",
      "description": "查询当前股票持仓。每行：证券代码, 证券名称, 股票余额, 可用余额, 冻结数量(股), 参考成本价, 市价(元), market_value, 浮动盈亏(元), 盈亏比例_pct。缺值为 null。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "orders_active",
      "description": "查询**在飞**委托单（未报/已报/部成）。已成/已撤/废单不出现在本表（契约 v2 C3）；状态识别不出的行按在飞保守返回。每行：client_order_id, entrust_no, 证券代码, 证券名称, 方向, 委托价, 委托数量, 已成数量, 成交均价, 状态, 柜台备注。数值为 number，缺值为 null（不是 0）。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "orders_filled",
      "description": "查询当日成交明细。每行：client_order_id, entrust_no, 成交编号, 成交时间(ISO8601，日期与时区来自受控端本机时钟，非柜台时间), 证券代码, 证券名称, 方向, 成交数量, 成交均价, 成交金额。数值为 number，缺值为 null。",
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
            "enum": [
              "近一周",
              "近一月",
              "近三月",
              "近一年"
            ],
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
            "description": "客户端订单 ID，**幂等键**。调用方无需填写：未传时受控端会由本次 RPC id 自动分配；同一 RPC id 重发只会下单一次。调用方也可显式传入，用于跨请求重试。该 id 不写入柜台，仅存于受控端台账，并在回执中回显。"
          }
        },
        "required": [
          "stock_no",
          "amount"
        ],
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
            "description": "客户端订单 ID，**幂等键**。调用方无需填写：未传时受控端会由本次 RPC id 自动分配；同一 RPC id 重发只会下单一次。调用方也可显式传入，用于跨请求重试。该 id 不写入柜台，仅存于受控端台账，并在回执中回显。"
          }
        },
        "required": [
          "stock_no",
          "amount"
        ],
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
          },
          "client_order_id": {
            "type": "string",
            "description": "客户端订单 ID，**幂等键**。调用方无需填写：未传时受控端会由本次 RPC id 自动分配；同一 RPC id 重发只会下单一次。调用方也可显式传入，用于跨请求重试。该 id 不写入柜台，仅存于受控端台账，并在回执中回显。"
          }
        },
        "required": [
          "entrust_no"
        ],
        "additionalProperties": False
      }
    },
    {
      "name": "switch_account",
      "description": "切换同花顺客户端当前活跃的资金账户（向 xiadan 窗口发送 Alt+N，N=账户在客户端账户下拉列表中的槽位序号）。仅在 xiadan 登录了多个账户时有意义。**盲切**：本工具不核验切换是否成功，受控端对账户身份无感知；切换后所有工具(查询/下单)都作用于新的当前账户。调用方必须紧接着用 balance/position 做指纹核对、确认账户无误后再继续操作。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "slot": {
            "type": "integer",
            "description": "账户槽位序号（1-9），对应快捷键 Alt+N，与客户端账户下拉列表顺序一致",
            "minimum": 1,
            "maximum": 9
          }
        },
        "required": [
          "slot"
        ],
        "additionalProperties": False
      }
    },
    {
      "name": "query_order",
      "description": "按 client_order_id 查单（契约 v2 C5b）。返回 state（未报/已报/部成/已成/已撤/废单/未知）＋首次回执快照＋分辨率 resolution：by_entrust_no=按合同编号精确命中；heuristic=台账无合同编号时按代码/数量匹配，存在同参重复单歧义；unresolved=实表中无法唯一定位，state=未知需人工。与 buy/sell/cancel 的幂等（同 id 重发不重复下单）配对使用。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "client_order_id": {
            "type": "string",
            "description": "下单时传入的 client_order_id"
          }
        },
        "required": [
          "client_order_id"
        ],
        "additionalProperties": False
      }
    }
  ],
  "contract_version": "2"
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
        trading_names = {"balance", "position", "orders_active", "orders_filled", "settlement", "watchlist", "buy", "sell", "cancel", "switch_account"}
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
    "switch_account",
    "query_order",
}


def _ledger_or_none(backend):
    return getattr(backend, "ledger", None)


def _release_reservation(backend, coid: str) -> None:
    led = _ledger_or_none(backend)
    if led is not None:
        try:
            led.release(coid)
        except Exception:
            logger.warning("台账撤销登记失败 coid=%s", coid, exc_info=True)


def _record_brief(record: Optional[dict]) -> dict:
    """台账条目的对外摘要（不回吐内部字段）。"""
    r = record or {}
    return {"state": r.get("state"), "entrust_no": r.get("entrust_no"),
            "created_at": r.get("created_at")}


def _replay_receipt(coid: str, record: Optional[dict]) -> dict:
    """同 id 重发：返回首次回执；首次尚未落定则回 unknown_outcome。

    无论哪条分支，**都不会产生第二次提交**——这就是 C5a 的全部承诺。
    「首次结果本身就是未知」是合法态：最危险那一刻台账自己也不知道结果，
    契约不撒谎（需求方 v2 已把「unknown 从此不存在」改为「收窄至回查确认前」）。
    """
    record = record or {}
    receipt = record.get("receipt")
    if record.get("state") == "done" and isinstance(receipt, dict):
        replayed = json.loads(json.dumps(receipt, ensure_ascii=False))
        if isinstance(replayed.get("data"), dict):
            replayed["data"]["idempotent_replay"] = True
        return replayed
    return contract.submitted_unconfirmed(
        f"client_order_id={coid} 的上一笔提交尚未落定回执，本次未产生第二次提交。"
        "请调 query_order 核实，或稍后用同一 id 再次重发",
        data={"submitted": True, "client_order_id": coid, "idempotent_replay": True,
              "first_record": _record_brief(record)})


async def _query_order(backend, client_order_id: Any) -> dict:
    """C5b 按 client_order_id 查单：台账定位 + 实时委托/成交表核实。

    分辨率分三档，回执里明说是哪一档——消费侧据此决定信不信：
    ``by_entrust_no``（台账有 entrust_no，实表精确命中）、
    ``heuristic``（entrust_no 未知，按代码/方向/数量/价格唯一匹配）、
    ``unresolved``（零命中或多命中 → 未知，需人工）。
    """
    if not client_order_id:
        return contract.fail(contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                             "query_order 缺少 client_order_id")
    coid = str(client_order_id)
    led = _ledger_or_none(backend)
    if led is None:
        return contract.fail(contract.CODE_LEDGER_UNAVAILABLE, contract.CLS_LEDGER_UNAVAILABLE,
                             "下单台账不可用，无法查单")
    try:
        record = await asyncio.to_thread(led.get, coid)
    except LedgerUnavailable as e:
        return contract.fail(contract.CODE_LEDGER_UNAVAILABLE, contract.CLS_LEDGER_UNAVAILABLE,
                             f"台账读取失败：{e}")
    if record is None:
        return contract.fail(
            contract.CODE_NOT_FOUND, contract.CLS_NOT_FOUND,
            f"台账中没有 client_order_id={coid}："
            "本受控端未提交过该 id，或已超出台账保留窗口")

    active = await backend.orders_active()
    filled = await backend.orders_filled()
    active_rows = (active.get("data") or []) if contract.is_succeed(active) else []
    filled_rows = (filled.get("data") or []) if contract.is_succeed(filled) else []
    tables_ok = contract.is_succeed(active) and contract.is_succeed(filled)

    entrust_no = record.get("entrust_no")
    resolution, state, matched = "unresolved", "未知", []
    if entrust_no:
        matched = [r for r in active_rows if r.get("entrust_no") == entrust_no]
        if matched:
            resolution, state = "by_entrust_no", matched[0].get("状态") or "未知"
        else:
            matched = [r for r in filled_rows if r.get("entrust_no") == entrust_no]
            if matched:
                resolution, state = "by_entrust_no", "已成"
    else:
        # entrust_no 未知（提交超时那批）：按首次请求指纹启发式匹配。
        try:
            fp = json.loads(record.get("fingerprint") or "{}")
        except (TypeError, ValueError):
            fp = {}
        stock_no, amount = str(fp.get("stock_no") or ""), fp.get("amount")

        def _hit(rows, qty_key):
            return [r for r in rows
                    if (r.get("证券代码") or "") == stock_no
                    and (amount is None or r.get(qty_key) == amount)]

        cand = _hit(active_rows, "委托数量")
        if len(cand) == 1:
            resolution, state, matched = "heuristic", cand[0].get("状态") or "未知", cand
        elif not cand:
            cand = _hit(filled_rows, "成交数量")
            if len(cand) == 1:
                resolution, state, matched = "heuristic", "已成", cand

    return contract.ok({
        "client_order_id": coid,
        "state": state,                       # 未报/已报/部成/已成/已撤/废单/未知
        "resolution": resolution,
        "entrust_no": entrust_no,
        "ledger_state": record.get("state"),
        "first_receipt": record.get("receipt"),
        "matched_rows": matched,
        "tables_readable": tables_ok,         # False ⇒ state 的可信度仅限台账
        "note": ("state=未知 表示实表中无法唯一定位该单，需人工核实；"
                 "resolution=heuristic 表示按代码/数量匹配而非 id 关联，存在同参重复单歧义"),
    })


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
        msg = f"方法 '{method}' 不支持"
        reply["ok"] = False
        reply["result"] = contract.fail(contract.CODE_UNSUPPORTED_METHOD,
                                        contract.CLS_INVALID_PARAMS, msg)
        reply["error"] = msg
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
        "switch_account",
        "query_order",
    }
    if method in trading_methods and not cfg.enable_ths_plugin:
        msg = "同花顺实盘交易插件已被禁用，请在客户端界面中开启该插件模块！"
        reply["ok"] = False
        reply["result"] = contract.fail(contract.CODE_PLUGIN_DISABLED,
                                        contract.CLS_PLUGIN_DISABLED, msg)
        reply["error"] = msg
        return reply

    # --- C5a 幂等：在**拿锁之前**查台账。重发直接返回首次回执，连排队都不用排，
    # 更不会走到点提交那一步。台账不可用一律拒单（需求方拍板：禁静默降级）。
    reserved_coid: Optional[str] = None
    if method in IDEMPOTENT_METHODS:
        coid = _resolve_client_order_id(method, params, frame_id)
        if coid is None:
            msg = "订单请求缺少有效 RPC id，无法安全分配幂等键，已拒绝执行"
            reply["ok"] = False
            reply["result"] = contract.fail(contract.CODE_INVALID_PARAMS,
                                            contract.CLS_INVALID_PARAMS, msg,
                                            data={"submitted": False})
            reply["error"] = msg
            return reply
        # 将自动分配的键写入这一请求的本地副本，确保台账、后端与回执使用同一值。
        params = {**params, "client_order_id": coid}
        led = _ledger_or_none(backend)
        if led is None:
            msg = ("下单台账不可用，已拒绝下单——无台账即无法保证 client_order_id 幂等，"
                   "重发会造成重复下单。请检查受控端数据目录后重试")
            reply["ok"] = False
            reply["result"] = contract.fail(contract.CODE_LEDGER_UNAVAILABLE,
                                            contract.CLS_LEDGER_UNAVAILABLE, msg)
            reply["error"] = msg
            return reply
        try:
            verdict, record = await asyncio.to_thread(led.reserve, coid, method, params)
        except LedgerUnavailable as e:
            msg = f"下单台账不可用，已拒绝下单（禁降级为无幂等下单）：{e}"
            reply["ok"] = False
            reply["result"] = contract.fail(contract.CODE_LEDGER_UNAVAILABLE,
                                            contract.CLS_LEDGER_UNAVAILABLE, msg)
            reply["error"] = msg
            return reply
        if verdict == "conflict":
            msg = (f"client_order_id={coid} 已用于参数不同的委托，拒绝执行。"
                   "同 id 必须对应同一笔委托——请换新 id，或用 query_order 查原单")
            reply["ok"] = False
            reply["result"] = contract.fail(contract.CODE_INVALID_PARAMS,
                                            contract.CLS_INVALID_PARAMS, msg,
                                            data={"submitted": False,
                                                  "first_record": _record_brief(record)})
            reply["error"] = msg
            return reply
        if verdict == "duplicate":
            result = _replay_receipt(coid, record)
            reply["ok"] = contract.is_succeed(result)
            reply["result"] = result
            if not reply["ok"]:
                reply["error"] = (result.get("error") or {}).get("message") or "重复提交"
            logger.info("[RPC] 幂等命中 coid=%s state=%s，未产生第二次提交",
                        coid, (record or {}).get("state"))
            return reply
        reserved_coid = coid

    # 串行化 THS 单窗口访问：order_watch 轮询与下单/查询共用 backend.win_lock。
    # 拿锁带超时：持锁方若被弹窗/慢操作拖住，排队方不能无限饿死——回 busy
    # 让调用方稍后重试，并提醒先核实前序委托。
    needs_window = method in trading_methods
    if needs_window:
        try:
            await asyncio.wait_for(backend.win_lock.acquire(), LOCK_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            msg = ("受控端正忙或被弹窗阻塞，本笔指令未执行。"
                   f"建议退避 {BUSY_BACKOFF_HINT_SECS}s 后重试；"
                   "下单类请先调 orders_active/orders_filled 或 query_order 核实前序委托")
            result = contract.busy(msg)
            result["data"] = {"submitted": False,
                              "retry_after_secs": BUSY_BACKOFF_HINT_SECS}
            if reserved_coid:
                _release_reservation(backend, reserved_coid)
            reply["ok"] = False
            reply["result"] = result
            reply["error"] = msg
            return reply
    try:
        # 上一笔调用超时（疑似弹窗阻塞）后进入 degraded：先清残留弹窗再干活。
        # 清扫失败不阻断本次调用。
        if getattr(backend, "degraded", False):
            try:
                await asyncio.to_thread(backend.dialog_cleanup)
            except Exception:
                logger.exception("degraded dialog_cleanup 失败")
            backend.degraded = False

        async def _invoke() -> Any:
            if method == "balance":
                logger.info("[RPC] method=balance, frame_id=%s", frame_id)
                r = await backend.balance()
                logger.info("[RPC] balance → status=%s code=%s",
                            (r or {}).get("status"), (r or {}).get("code"))
                return r
            if method == "position":
                return await backend.position()
            if method == "orders_active":
                return await backend.orders_active()
            if method == "orders_filled":
                return await backend.orders_filled()
            if method == "settlement":
                return await backend.settlement(params.get("date_range", "近一年"))
            if method == "watchlist":
                return await backend.watchlist()
            if method in ("buy", "sell"):
                stock_no = params.get("stock_no")
                amount = params.get("amount")
                price = params.get("price")
                client_order_id = params.get("client_order_id")
                fn = backend.buy if method == "buy" else backend.sell
                r = await fn(stock_no, amount, price, client_order_id)
                _eno = ((r or {}).get("data") or {}).get("entrust_no")
                if _eno:
                    backend.agent_entrust_nos.add(str(_eno))
                return r
            if method == "cancel":
                return await backend.cancel(params.get("entrust_no"))
            if method == "switch_account":
                return await backend.switch_account(params.get("slot"))
            if method == "query_order":
                return await _query_order(backend, params.get("client_order_id"))
            return contract.fail(contract.CODE_INTERNAL_ERROR,
                                 contract.CLS_INTERNAL_ERROR, f"未实现的方法 {method}")

        try:
            # 受控端总超时（低于网关 30s）：无论内部卡在哪，25s 内必有明确回执。
            # 弹窗/无响应导致的超时绝不能表现为裸报错——委托可能已提交，
            # 必须回 unknown + 核单指引（2026-07-13「报错但静默成交」事故）。
            result = await asyncio.wait_for(_invoke(), CALL_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            backend.degraded = True
            # wait_for 只取消了等待协程——to_thread 起的工作线程取消不掉，它还在
            # 发全局按键，而下面 finally 马上要放 win_lock 让下一笔进场。作废代次，
            # 让那个线程在下一个检查点（翻页/抓表/弹窗/提交）自己停手，
            # 否则两个线程同击一个 xiadan 窗口 → 抓错表、抢弹窗。
            invalidate = getattr(backend, "invalidate_inflight", None)
            if invalidate:
                invalidate(f"{method} 超过 {CALL_TIMEOUT_SECS}s 未完成")
            logger.error("[RPC] %s 超过 %ss 未完成，标记 degraded，回 unknown",
                         method, CALL_TIMEOUT_SECS)
            if method in ORDER_METHODS:
                result = contract.submitted_unconfirmed(
                    "受控端处理超时（疑似弹窗或客户端无响应），委托可能已提交。"
                    "安全动作=用同一 client_order_id 原样重发（幂等，不会重复下单），"
                    "或调 query_order/orders_active 核实；勿改单重下",
                    data={"submitted": True})
            else:
                result = contract.fail(
                    contract.CODE_CALL_TIMEOUT, contract.CLS_CALL_TIMEOUT,
                    "受控端查询超时（疑似弹窗或客户端无响应），请稍后重试")

        if not isinstance(result, dict) or "status" not in result:
            result = contract.fail(contract.CODE_INTERNAL_ERROR,
                                   contract.CLS_INTERNAL_ERROR,
                                   f"受控端返回了非契约形态：{type(result).__name__}")

        # 下单类：回填 client_order_id 并把首次回执落台账（幂等重发就靠它）。
        if reserved_coid:
            if isinstance(result.get("data"), dict):
                result["data"]["client_order_id"] = reserved_coid
            elif result.get("data") is None:
                result["data"] = {"client_order_id": reserved_coid}
            entrust_no = (result.get("data") or {}).get("entrust_no")
            try:
                led = _ledger_or_none(backend)
                if led is not None:
                    await asyncio.to_thread(led.complete, reserved_coid, result,
                                            str(entrust_no) if entrust_no else None)
                reserved_coid = None   # 已落定，finally 不再回滚
            except LedgerUnavailable:
                # 单已经下出去了，台账却写不进——绝不静默：明确降级为「结果不可知」，
                # 逼调用方去核单，而不是让它以为下单成功。
                logger.exception("台账回写失败 coid=%s，回执降级为 unknown_outcome", reserved_coid)
                result = contract.submitted_unconfirmed(
                    "委托已提交，但台账回写失败——本次结果无法保证可幂等重放，"
                    "请立即用 orders_active/orders_filled 人工核单",
                    data={"submitted": True, "client_order_id": reserved_coid})
                reserved_coid = None

        reply["result"] = result
        reply["ok"] = contract.is_succeed(result)
        if not reply["ok"]:
            reply["error"] = ((result.get("error") or {}).get("message")
                              or f"{result.get('status')}/{result.get('code')}")

    except Exception as e:
        logger.error("处理 RPC '%s' 出错：%s", method, e)
        reply["ok"] = False
        reply["result"] = contract.fail(contract.CODE_INTERNAL_ERROR,
                                        contract.CLS_INTERNAL_ERROR, str(e))
        reply["error"] = str(e)
    finally:
        if needs_window:
            backend.win_lock.release()
        # 走到这里还留着预留说明本笔没能落定回执（异常/未知路径）：
        # 保留登记而不是删除——宁可让重发命中「上一笔结果未知」，也不能让它变成新单。
        if reserved_coid:
            logger.warning("coid=%s 未落定回执，台账保留为 submitting（重发将回 unknown）",
                           reserved_coid)

    return reply
