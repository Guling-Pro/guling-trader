"""RPC 分派：call frame → backend method → reply frame

契约 v2：backend 返回的已经是统一信封（见 contract.py），dispatcher 只负责
①幂等台账（C5a）②查单（C5b）③busy/超时这两种「还没进 backend」的信封 ④装进 reply 帧。
"""
import asyncio
import json
import logging
import math
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import contract
from .order_ledger import LedgerUnavailable
from .ths.rows import ST_CANCELED, ST_FILLED, ST_PARTIAL, ST_PENDING, ST_PLACED, ST_REJECTED
from .ths.win import WinThsBackend

logger = logging.getLogger(__name__)

# 受控端单笔调用总预算：必须低于网关侧 30s 超时，保证网关永远等得到带
# unknown 语义的 reply，而不是自造裸错误（-32003）。
CALL_TIMEOUT_SECS = 25.0
# ``submitted_unconfirmed`` 后的自动核验必须留在网关 30s 总预算内。买卖只读
# orders_active/orders_filled；撤单只读内部全量委托表。超时后保留原始未知结果，绝不重发。
AUTO_QUERY_TIMEOUT_SECS = 3.0
# win_lock 排队上限：持锁方被拖住时，排队方回 busy 而非无限饿死。
LOCK_TIMEOUT_SECS = 5.0
# 会真实改变账户状态的方法：超时/busy 回执必须带「可能已提交，先核单」语义。
CANCEL_METHODS = {"cancel", "confirm_external_cancel"}
ORDER_METHODS = {"buy", "sell", *CANCEL_METHODS}
# 走 client_order_id 幂等台账的方法（C5a）。
IDEMPOTENT_METHODS = {"buy", "sell", *CANCEL_METHODS}
# 买卖按成交/在飞表核单；撤单按目标 entrust_no 的全量委托表核验终态。
AUTO_QUERY_METHODS = {"buy", "sell", *CANCEL_METHODS}
# busy 是背压信号：告诉调用方等多久再来，别让它自己猜（G3）。
BUSY_BACKOFF_HINT_SECS = 3
# 买卖的显式业务语义。入口不再根据 price 是否存在猜测订单类型。
ORDER_TYPES = ("LIMIT", "FIVE_LEVEL_IOC")
_ORDER_TYPE_SET = frozenset(ORDER_TYPES)
# UUID v7 带毫秒时间成分与随机位，便于按时间审计且碰撞概率可忽略。格式校验只
# 约束协议，不在受控端自行生成或重写 ID——同一业务订单必须由上游复用原 ID。
CLIENT_ORDER_ID_PATTERN = (
    r"^gl-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CLIENT_ORDER_ID_RE = re.compile(CLIENT_ORDER_ID_PATTERN)

# 未登记订单的确认令牌只留在进程内。60 秒足以让调用方展示订单摘要并发起确认，
# 又不会把一次旧读取长期变成可执行授权。
EXTERNAL_CANCEL_CONFIRMATION_TTL_SECS = 60.0
_external_cancel_confirmations: dict[str, dict[str, Any]] = {}
_external_cancel_confirmations_lock = threading.Lock()


def clear_external_cancel_confirmations() -> None:
    """清空进程内的未登记订单撤单确认令牌。

    令牌绝不落盘；连接代次切换或进程重启后，先前读取到的订单快照不能继续
    作为点击撤单的授权。``ws_client`` 在重连时调用此函数。
    """
    with _external_cancel_confirmations_lock:
        _external_cancel_confirmations.clear()


def _clean_external_cancel_confirmations_locked(now: float) -> None:
    """在持锁状态下删除过期令牌。"""
    expired = [
        token for token, record in _external_cancel_confirmations.items()
        if float(record.get("expires_at", 0.0)) <= now
    ]
    for token in expired:
        _external_cancel_confirmations.pop(token, None)


def _copy_confirmation_record(record: dict[str, Any]) -> dict[str, Any]:
    """返回可安全供调用方读取的令牌记录副本。"""
    copied = dict(record)
    binding = copied.get("binding")
    if isinstance(binding, dict):
        copied["binding"] = dict(binding)
    summary = copied.get("summary")
    if isinstance(summary, dict):
        copied["summary"] = dict(summary)
    return copied


def _issue_external_cancel_confirmation(
    source_client_order_id: str,
    binding: dict[str, Any],
    summary: dict[str, Any],
) -> tuple[str, float]:
    """生成短时确认令牌；同一提示请求只保留最新令牌。"""
    now = time.monotonic()
    expires_at = now + EXTERNAL_CANCEL_CONFIRMATION_TTL_SECS
    with _external_cancel_confirmations_lock:
        _clean_external_cancel_confirmations_locked(now)
        # 同一个 cancel 的重放只是在重新展示订单、换发授权，绝不能让旧令牌
        # 与新令牌同时有效。这样连接恢复或用户停留过久后仍可用同一请求安全刷新。
        prior_tokens = [
            token for token, record in _external_cancel_confirmations.items()
            if record.get("source_client_order_id") == source_client_order_id
        ]
        for prior_token in prior_tokens:
            _external_cancel_confirmations.pop(prior_token, None)
        token = secrets.token_urlsafe(32)
        while token in _external_cancel_confirmations:
            token = secrets.token_urlsafe(32)
        _external_cancel_confirmations[token] = {
            "source_client_order_id": source_client_order_id,
            "entrust_no": binding["entrust_no"],
            "binding": dict(binding),
            "summary": dict(summary),
            "expires_at": expires_at,
            "used": False,
        }
    return token, expires_at


def _peek_external_cancel_confirmation(
    token: Any,
) -> tuple[str, Optional[dict[str, Any]]]:
    """读取令牌，不消费它。

    返回值第一项为 ``ok``、``used``、``expired``、``missing`` 或 ``invalid``。
    ``used`` 仍会返回记录，用于同一确认请求的幂等回放；它永远不能再执行撤单。
    """
    if not isinstance(token, str) or not token.strip():
        return "invalid", None
    value = token.strip()
    now = time.monotonic()
    with _external_cancel_confirmations_lock:
        record = _external_cancel_confirmations.get(value)
        if record is None:
            return "missing", None
        if float(record.get("expires_at", 0.0)) <= now:
            _external_cancel_confirmations.pop(value, None)
            return "expired", None
        if record.get("used"):
            return "used", _copy_confirmation_record(record)
        return "ok", _copy_confirmation_record(record)


def _consume_external_cancel_confirmation(
    token: Any,
) -> tuple[str, Optional[dict[str, Any]]]:
    """原子消费确认令牌，保证并发确认至多有一个可进入撤单前核验。"""
    if not isinstance(token, str) or not token.strip():
        return "invalid", None
    value = token.strip()
    now = time.monotonic()
    with _external_cancel_confirmations_lock:
        record = _external_cancel_confirmations.get(value)
        if record is None:
            return "missing", None
        if float(record.get("expires_at", 0.0)) <= now:
            _external_cancel_confirmations.pop(value, None)
            return "expired", None
        if record.get("used"):
            return "used", _copy_confirmation_record(record)
        record["used"] = True
        return "ok", _copy_confirmation_record(record)


def _invalidate_external_cancel_confirmation(token: Any) -> None:
    """撤销未发出的确认授权（例如台账无法安全保存提示回执）。"""
    if not isinstance(token, str) or not token:
        return
    with _external_cancel_confirmations_lock:
        _external_cancel_confirmations.pop(token, None)


def _is_unsubmitted_external_cancel_prompt(record: Optional[dict]) -> bool:
    """确认台账记录是否只是一次尚未执行的人工订单提示。

    这种回执没有发送真实撤单。允许相同的 cancel 幂等键再次读取订单并换发令牌，
    既解决进程/连接切换后的旧令牌问题，也不会把重试变成第二次真实操作。
    """
    receipt = (record or {}).get("receipt")
    if not isinstance(receipt, dict):
        return False
    if receipt.get("code") != contract.CODE_CONFIRMATION_REQUIRED:
        return False
    data = receipt.get("data")
    return isinstance(data, dict) and data.get("submitted") is False


def _receipt_for_ledger(result: dict) -> dict:
    """生成可持久化回执副本，绝不把确认令牌写进 SQLite。"""
    if result.get("code") != contract.CODE_CONFIRMATION_REQUIRED:
        return result
    data = result.get("data")
    if not isinstance(data, dict) or "confirmation_token" not in data:
        return result
    stored = json.loads(json.dumps(result, ensure_ascii=False))
    stored_data = stored.get("data")
    if isinstance(stored_data, dict):
        stored_data.pop("confirmation_token", None)
    return stored

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
      "name": "list_accounts",
      "description": "只读列出同花顺账户下拉框中的可切换账户。仅点击当前控件快照确认的账户 ComboBox（ID 0x0912）打开下拉框，等待 0.3 秒后读取展开的 ComboLBox（ID 0x03E8）原始列表项文本；过滤“编辑账户”，其余账户按显示顺序对应 Alt+1..Alt+9。不发送 Alt+N、不选择账户，账户名称原样保留（包括 *）；返回 slot、shortcut 和 text。",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
      }
    },
    {
      "name": "buy",
      "description": "下买入委托单。**会真实下单**，慎重调用。必须显式指定 order_type：LIMIT 为限价挂单，FIVE_LEVEL_IOC 为五档即成剩撤（立即成交、剩余自动撤销、无残留挂单）。LIMIT 必须传正数 price；FIVE_LEVEL_IOC 禁止传 price。",
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
          "order_type": {
            "type": "string",
            "enum": [
              "LIMIT",
              "FIVE_LEVEL_IOC"
            ],
            "description": "订单类型。LIMIT=限价挂单（必须传正数 price）；FIVE_LEVEL_IOC=五档即成剩撤（禁止传 price）。"
          },
          "price": {
            "type": "number",
            "description": "LIMIT 必填的正数限价。order_type=FIVE_LEVEL_IOC 时禁止传入。"
          },
          "client_order_id": {
            "type": "string",
            "pattern": "^gl-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            "description": "客户端订单 ID，**必填幂等键**。格式必须为 gl-<小写 UUID v7>，如 gl-0198f6a1-0001-7000-8000-000000000001；UUID v7 含毫秒时间戳和随机位。调用方必须在创建买卖请求时生成并持久保存该 ID：新买卖用新 ID；网络重发或 query_order 查询同一买卖单必须原样复用。交易端只验证，不生成或改写。同一 ID 重复提交只会执行一次。buy/sell 返回 submitted_unconfirmed 时，交易端会自动执行一次只读 query_order，结果位于 data.auto_query，绝不自动重发。"
          }
        },
        "required": [
          "stock_no",
          "amount",
          "order_type",
          "client_order_id"
        ],
        "additionalProperties": False
      }
    },
    {
      "name": "sell",
      "description": "下卖出委托单。**会真实下单**，慎重调用。必须显式指定 order_type：LIMIT 为限价挂单，FIVE_LEVEL_IOC 为五档即成剩撤（立即成交、剩余自动撤销、无残留挂单）。LIMIT 必须传正数 price；FIVE_LEVEL_IOC 禁止传 price。",
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
          "order_type": {
            "type": "string",
            "enum": [
              "LIMIT",
              "FIVE_LEVEL_IOC"
            ],
            "description": "订单类型。LIMIT=限价挂单（必须传正数 price）；FIVE_LEVEL_IOC=五档即成剩撤（禁止传 price）。"
          },
          "price": {
            "type": "number",
            "description": "LIMIT 必填的正数限价。order_type=FIVE_LEVEL_IOC 时禁止传入。"
          },
          "client_order_id": {
            "type": "string",
            "pattern": "^gl-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            "description": "客户端订单 ID，**必填幂等键**。格式必须为 gl-<小写 UUID v7>，如 gl-0198f6a1-0001-7000-8000-000000000001；UUID v7 含毫秒时间戳和随机位。调用方必须在创建买卖请求时生成并持久保存该 ID：新买卖用新 ID；网络重发或 query_order 查询同一买卖单必须原样复用。交易端只验证，不生成或改写。同一 ID 重复提交只会执行一次。buy/sell 返回 submitted_unconfirmed 时，交易端会自动执行一次只读 query_order，结果位于 data.auto_query，绝不自动重发。"
          }
        },
        "required": [
          "stock_no",
          "amount",
          "order_type",
          "client_order_id"
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
            "pattern": "^gl-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            "description": "撤单请求 ID，**必填幂等键**。格式必须为 gl-<小写 UUID v7>，如 gl-0198f6a1-0001-7000-8000-000000000001；UUID v7 含毫秒时间戳和随机位。调用方必须在创建撤单请求时生成并持久保存该 ID：每个新撤单动作使用新 ID；网络重发同一撤单请求必须原样复用。交易端只验证，不生成或改写。同一 ID 重复提交只会执行一次。cancel 返回 submitted_unconfirmed 时，交易端会按目标 entrust_no 自动读取一次含终态的全量委托表，结果位于 data.auto_query；其中 cancel_state 为已撤/部成后已撤/已成/仍在飞/废单/未知。仅一次只读核验，绝不自动重发。"
          }
        },
        "required": [
          "entrust_no",
          "client_order_id"
        ],
        "additionalProperties": False
      }
    },
    {
      "name": "confirm_external_cancel",
      "description": "确认撤销未登记订单。仅接受此前 cancel 回执给出的短时一次性 confirmation_token；确认前会再次核验合同号及证券代码、方向、委托价、委托数量仍一致且订单仍可撤。必须使用新的 client_order_id，令牌过期、已使用或订单变化时绝不执行撤单。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "confirmation_token": {
            "type": "string",
            "description": "此前 cancel 对未登记订单返回的短时一次性确认令牌"
          },
          "client_order_id": {
            "type": "string",
            "pattern": "^gl-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            "description": "本次确认动作的新客户端订单 ID，必填幂等键，格式必须为 gl-<小写 UUID v7>；网络重发同一确认时必须原样复用，不能复用原 cancel 的 ID。"
          }
        },
        "required": [
          "confirmation_token",
          "client_order_id"
        ],
        "additionalProperties": False
      }
    },
    {
      "name": "switch_account",
      "description": "切换同花顺客户端当前活跃的资金账户（向 xiadan 窗口发送 Alt+N，N=账户在客户端账户下拉列表中的槽位序号）。仅在 xiadan 登录了多个账户时有意义。切换后按账户控件 ID 0x094C 的 text 核验账户变化，并立即读取一次资金信息；成功时返回 account_text 和 balance，失败时禁止后续买卖和撤单。每笔 buy/sell/cancel/confirm_external_cancel 前都会重新核对该账户文本。",
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
      "description": "按 client_order_id 查单（契约 v2 C5b）。买卖单返回 state（未报/已报/部成/已成/已撤/废单/未知）＋首次回执快照＋分辨率 resolution：by_entrust_no=按合同编号精确命中；heuristic=台账无合同编号时按代码、方向、数量匹配，限价活单还须委托价一致，仍可能有同参重复单歧义；unresolved=实表中无法唯一定位，state=未知需人工。对 cancel 请求 ID，按其目标 entrust_no 精确读取含终态的全量委托表，并额外返回 cancel_state（已撤/部成后已撤/已成/仍在飞/废单/未知）。与 buy/sell/cancel 的幂等（同 id 重发不重复下单）配对使用。",
      "inputSchema": {
        "type": "object",
        "properties": {
          "client_order_id": {
            "type": "string",
            "description": "买卖或撤单时传入的 client_order_id"
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
        trading_names = {
            "balance", "position", "orders_active", "orders_filled", "settlement",
            "watchlist", "list_accounts", "buy", "sell", "cancel", "confirm_external_cancel",
            "switch_account", "query_order",
        }
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
    "list_accounts",
    "buy",
    "sell",
    "cancel",
    "confirm_external_cancel",
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


def _ledger_fingerprint(record: dict) -> dict:
    """读取台账请求指纹；损坏的历史值按空指纹处理。"""
    try:
        value = json.loads(record.get("fingerprint") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _cancel_target_entrust_no(record: dict) -> Optional[str]:
    """撤单要核验的原委托编号，以首次请求参数为准。"""
    fingerprint = _ledger_fingerprint(record)
    value = fingerprint.get("entrust_no") or record.get("entrust_no")
    if value is None:
        return None
    entrust_no = str(value).strip()
    return entrust_no or None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_buy_sell_params(method: str, params: dict[str, Any]) -> Optional[str]:
    """校验买卖的显式订单语义，返回错误原因或 ``None``。

    该函数只检查请求参数，不访问 backend 或台账；调用方必须在幂等预留和
    ``win_lock`` 之前调用它。这样漏字段、错类型和非法价格都不会留下台账
    记录，也不会有机会触碰交易窗口。
    """
    order_type = params.get("order_type")
    if order_type not in _ORDER_TYPE_SET:
        return (
            f"{method} 必须明确指定 order_type，取值只能是 LIMIT 或 FIVE_LEVEL_IOC；"
            "已拒绝执行"
        )

    has_price = "price" in params
    price = params.get("price")
    if order_type == "LIMIT":
        if not has_price or isinstance(price, bool) or not isinstance(price, (int, float)):
            return "order_type=LIMIT 必须传入正数 price，已拒绝执行"
        if not math.isfinite(float(price)) or float(price) <= 0:
            return "order_type=LIMIT 的 price 必须是有限且大于 0 的数值，已拒绝执行"
    elif has_price:
        return "order_type=FIVE_LEVEL_IOC 禁止传入 price，已拒绝执行"
    return None


def _cancel_state_from_order_row(row: dict) -> str:
    """把原委托全量行转换为撤单动作的可审计结论。"""
    state = row.get("状态") or "未知"
    order_qty = _as_int(row.get("委托数量"))
    filled_qty = _as_int(row.get("已成数量"))

    if order_qty is not None and order_qty > 0 and filled_qty is not None and filled_qty >= order_qty:
        return "已成"
    if state == ST_CANCELED:
        if filled_qty is not None and filled_qty > 0:
            return "部成后已撤"
        return "已撤"
    if state == ST_FILLED:
        return "已成"
    if state in (ST_PENDING, ST_PLACED, ST_PARTIAL):
        return "仍在飞"
    if state == ST_REJECTED:
        return "废单"
    return "未知"


_EXTERNAL_CANCEL_SNAPSHOT_FIELDS = (
    "entrust_no",
    "证券代码",
    "方向",
    "委托价",
    "委托数量",
    "已成数量",
    "状态",
)


def _external_cancel_snapshot(row: dict) -> Optional[dict[str, Any]]:
    """提取可用于二次确认的严格订单摘要。

    这里不接受缺失、无法规范化或状态未知的行。对人工订单来说，宁可要求用户
    重新读取后确认，也不能把一张不完整的表当作可执行撤单授权。
    """
    if not isinstance(row, dict):
        return None
    entrust_no = str(row.get("entrust_no") or "").strip()
    stock_no = str(row.get("证券代码") or "").strip()
    direction = str(row.get("方向") or "").strip()
    order_qty = _as_int(row.get("委托数量"))
    filled_qty = _as_int(row.get("已成数量"))
    state = str(row.get("状态") or "").strip()
    price = row.get("委托价")
    if price is None:
        normalized_price = None
    elif isinstance(price, bool):
        return None
    else:
        try:
            normalized_price = round(float(price), 3)
        except (TypeError, ValueError):
            return None

    if (
        not entrust_no
        or not stock_no
        or direction not in {"买入", "卖出"}
        or order_qty is None
        or order_qty <= 0
        or filled_qty is None
        or filled_qty < 0
        or state not in {ST_PENDING, ST_PLACED, ST_PARTIAL}
    ):
        return None
    return {
        "entrust_no": entrust_no,
        "证券代码": stock_no,
        "方向": direction,
        "委托价": normalized_price,
        "委托数量": order_qty,
        "已成数量": filled_qty,
        "状态": state,
    }


async def _read_external_cancel_target(
    backend,
    entrust_no: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """全量重读并严格确认目标订单仍可撤。

    ``orders_active`` 会过滤终态，无法区分「目标不存在」和「刚刚已成/已撤」；
    二次确认必须读取内部全量表，再按合同编号唯一匹配。
    """
    read_all = getattr(backend, "orders_active_all", None)
    if not callable(read_all):
        return None, contract.fail(
            contract.CODE_INTERNAL_ERROR,
            contract.CLS_INTERNAL_ERROR,
            "受控端不支持未登记订单的全量委托复核，已停止撤单",
            data={"submitted": False},
        )
    result = await read_all()
    if not contract.is_succeed(result):
        detail = ((result.get("error") or {}).get("message")) or "委托表读取失败"
        return None, contract.fail(
            result.get("code") or contract.CODE_READ_FAILED,
            ((result.get("error") or {}).get("class")) or contract.CLS_READ_FAILED,
            f"撤单前无法读取全量委托表进行复核（{detail}），已停止撤单",
            data={"submitted": False, "entrust_no": entrust_no},
        )
    rows = result.get("data")
    if not isinstance(rows, list):
        return None, contract.fail(
            contract.CODE_READ_FAILED,
            contract.CLS_READ_FAILED,
            "撤单前全量委托表不是行列表，已停止撤单",
            data={"submitted": False, "entrust_no": entrust_no},
        )
    matched = [
        row for row in rows
        if isinstance(row, dict) and str(row.get("entrust_no") or "").strip() == entrust_no
    ]
    if len(matched) != 1:
        code = contract.CODE_NOT_FOUND if not matched else contract.CODE_TABLE_MISMATCH
        cls = contract.CLS_NOT_FOUND if not matched else contract.CLS_TABLE_MISMATCH
        reason = "未找到该合同编号" if not matched else "同一合同编号出现多行"
        return None, contract.fail(
            code,
            cls,
            f"撤单前复核失败：{reason}，已停止撤单",
            data={"submitted": False, "entrust_no": entrust_no, "matched_rows": matched},
        )
    snapshot = _external_cancel_snapshot(matched[0])
    if snapshot is None:
        return None, contract.fail(
            contract.CODE_NOT_FOUND,
            contract.CLS_NOT_FOUND,
            "撤单前复核发现订单已不可撤、状态未知或关键字段不完整，已停止撤单",
            data={"submitted": False, "entrust_no": entrust_no, "order": matched[0]},
        )
    if snapshot["已成数量"] >= snapshot["委托数量"]:
        return None, contract.fail(
            contract.CODE_NOT_FOUND,
            contract.CLS_NOT_FOUND,
            "撤单前复核发现订单已全部成交，已停止撤单",
            data={"submitted": False, "entrust_no": entrust_no, "order": snapshot},
        )
    return snapshot, None


def _confirmation_required(message: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """返回未登记订单的显式确认回执；这条路径尚未点击撤单。"""
    payload: dict[str, Any] = {"submitted": False}
    if data:
        payload.update(data)
    return contract.fail(
        contract.CODE_CONFIRMATION_REQUIRED,
        contract.CLS_CONFIRMATION_REQUIRED,
        message,
        data=payload,
    )


def _external_cancel_confirmation_error(state: str) -> dict[str, Any]:
    """令牌不再可用时，要求调用方重新从 cancel 开始，不触发 GUI。"""
    messages = {
        "invalid": "confirmation_token 无效，未执行撤单；请重新发起 cancel 获取确认信息",
        "missing": "confirmation_token 不存在、已失效或连接已切换，未执行撤单；请重新发起 cancel",
        "expired": "confirmation_token 已过期，未执行撤单；请重新发起 cancel",
        "used": "confirmation_token 已使用，未执行撤单；请先用原确认请求 ID 查单或重新发起 cancel",
    }
    return _confirmation_required(
        messages.get(state, "confirmation_token 不可用，未执行撤单；请重新发起 cancel"),
        {"confirmation_state": state},
    )


def _external_cancel_binding_matches(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    """确认前必须仍是同一笔、同一状态的可撤订单。"""
    return all(expected.get(field) == actual.get(field) for field in _EXTERNAL_CANCEL_SNAPSHOT_FIELDS)


async def _query_cancel(backend, client_order_id: str, record: dict) -> dict:
    """撤单动作的查单：按目标合同编号读内部全量委托表，绝不二次撤单。"""
    entrust_no = _cancel_target_entrust_no(record)
    if not entrust_no:
        logger.warning("[CANCEL_VERIFY] coid=%s lacks target entrust_no", client_order_id)
        return contract.ok({
            "client_order_id": client_order_id,
            "state": "未知",
            "cancel_state": "未知",
            "resolution": "unresolved",
            "entrust_no": None,
            "ledger_state": record.get("state"),
            "first_receipt": record.get("receipt"),
            "matched_rows": [],
            "tables_readable": False,
            "note": "撤单请求未保存目标 entrust_no，无法核验是否已撤；需人工核实。",
        })

    read_all = getattr(backend, "orders_active_all", None)
    if not callable(read_all):
        return contract.fail(
            contract.CODE_INTERNAL_ERROR, contract.CLS_INTERNAL_ERROR,
            "受控端不支持撤单全量委托核验，无法确认是否已撤")

    all_orders = await read_all()
    tables_readable = contract.is_succeed(all_orders)
    rows = (all_orders.get("data") or []) if tables_readable else []
    matched = [row for row in rows if str(row.get("entrust_no") or "") == entrust_no]
    resolution, state, cancel_state = "unresolved", "未知", "未知"
    if len(matched) == 1:
        resolution = "by_entrust_no"
        state = matched[0].get("状态") or "未知"
        cancel_state = _cancel_state_from_order_row(matched[0])

    logger.info(
        "[CANCEL_VERIFY] coid=%s entrust_no=%s readable=%s resolution=%s state=%s "
        "cancel_state=%s matches=%d",
        client_order_id, entrust_no, tables_readable, resolution, state, cancel_state, len(matched),
    )
    return contract.ok({
        "client_order_id": client_order_id,
        "state": state,
        "cancel_state": cancel_state,
        "resolution": resolution,
        "entrust_no": entrust_no,
        "ledger_state": record.get("state"),
        "first_receipt": record.get("receipt"),
        "matched_rows": matched,
        "tables_readable": tables_readable,
        "note": (
            "cancel_state=已撤 或 部成后已撤 才表示柜台已确认撤单；"
            "已成/仍在飞/废单/未知均不表示撤单成功。"
        ),
    })


async def _query_order(backend, client_order_id: Any) -> dict:
    """C5b 按 client_order_id 查单：台账定位 + 实时委托/成交表核实。

    分辨率分三档，回执里明说是哪一档——消费侧据此决定信不信：
    ``by_entrust_no``（台账有 entrust_no，实表精确命中）、
    ``heuristic``（entrust_no 未知，按代码/方向/数量匹配；限价活单再比委托价）、
    ``unresolved``（零命中或多命中 → 未知，需人工）。

    对撤单请求，`client_order_id` 标识撤单动作而非原买卖单；改按其请求中保存的
    `entrust_no` 精确读取含终态的全量委托表，才能确认已撤或部成后已撤。
    `query_order` 保留读取历史台账中旧格式 ID 的能力；UUID v7 格式仅对新建的
    buy/sell/cancel 生效，避免升级后历史未知订单反而无法核查。
    """
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        return contract.fail(contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS,
                             "query_order 缺少 client_order_id")
    coid = client_order_id.strip()
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

    if record.get("method") in CANCEL_METHODS:
        return await _query_cancel(backend, coid, record)

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
        fp = _ledger_fingerprint(record)
        stock_no, amount = str(fp.get("stock_no") or ""), fp.get("amount")
        direction = {"buy": "买入", "sell": "卖出"}.get(
            record.get("method") or fp.get("method"))
        requested_price = fp.get("price")

        def _price_matches(value: Any) -> bool:
            if requested_price is None:
                return True
            try:
                return float(value) == float(requested_price)
            except (TypeError, ValueError):
                return False

        def _hit(rows, qty_key, *, price_key: Optional[str] = None):
            return [r for r in rows
                    if (r.get("证券代码") or "") == stock_no
                    and (direction is None or r.get("方向") == direction)
                    and (amount is None or r.get(qty_key) == amount)
                    and (price_key is None or _price_matches(r.get(price_key)))]

        cand = _hit(active_rows, "委托数量", price_key="委托价")
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
                 "resolution=heuristic 表示按代码/方向/数量（限价活单另含委托价）匹配而非 id 关联，"
                 "存在同参重复单歧义"),
    })


def _attach_auto_query(result: dict, query_result: dict) -> None:
    """把只读自动核单结果嵌入原未知回执，绝不改变原回执的未知语义。"""
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    data["auto_query"] = query_result


async def _auto_query_after_unconfirmed(
    backend,
    method: str,
    client_order_id: str,
    result: dict,
    *,
    lock_held: bool,
) -> None:
    """对买卖/撤单未知结果做一次受限的只读核验，不重发、不改变顶层结果。

    首次下单路径已持有 ``win_lock``；同 ID 再次调用命中台账时尚未持锁，需在这里
    按正常窗口访问规则排队。无论锁忙、核单超时还是内部异常，均只附带核单失败信息，
    绝不让它覆盖 ``submitted_unconfirmed`` 或触发第二次下单/撤单。
    """
    if method not in AUTO_QUERY_METHODS or result.get("code") != contract.CODE_SUBMITTED_UNCONFIRMED:
        return

    logger.info("[ORDER] submitted_unconfirmed 后开始一次只读核验 coid=%s method=%s",
                client_order_id, method)
    acquired_here = False
    lock = getattr(backend, "win_lock", None)
    if not lock_held:
        if lock is None:
            query_result = contract.fail(
                contract.CODE_INTERNAL_ERROR, contract.CLS_INTERNAL_ERROR,
                "自动核单未执行：受控端缺少窗口锁")
            _attach_auto_query(result, query_result)
            return
        try:
            await asyncio.wait_for(lock.acquire(), LOCK_TIMEOUT_SECS)
            acquired_here = True
        except asyncio.TimeoutError:
            query_result = contract.busy(
                "自动核验未执行：受控端正忙；原交易动作未重发，请稍后用同一 client_order_id 查单")
            query_result["data"] = {"submitted": True,
                                    "retry_after_secs": BUSY_BACKOFF_HINT_SECS}
            _attach_auto_query(result, query_result)
            logger.warning("[ORDER] 自动核单排队超时 coid=%s method=%s", client_order_id, method)
            return

    try:
        try:
            query_result = await asyncio.wait_for(
                _query_order(backend, client_order_id), AUTO_QUERY_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            # 与主调用超时同理：查询线程可能仍在操作窗口，作废代次后再放锁。
            backend.degraded = True
            invalidate = getattr(backend, "invalidate_inflight", None)
            if invalidate:
                invalidate(f"自动核单超过 {AUTO_QUERY_TIMEOUT_SECS}s 未完成")
            query_result = contract.fail(
                contract.CODE_CALL_TIMEOUT, contract.CLS_CALL_TIMEOUT,
                "自动核验超时，原交易动作结果仍未知；未自动重发，请稍后用同一 client_order_id 查单")
            logger.warning("[ORDER] 自动核单超时 coid=%s method=%s", client_order_id, method)
        except Exception:
            logger.exception("[ORDER] 自动核单异常 coid=%s method=%s", client_order_id, method)
            query_result = contract.fail(
                contract.CODE_INTERNAL_ERROR, contract.CLS_INTERNAL_ERROR,
                "自动核验异常，原交易动作结果仍未知；未自动重发，请稍后用同一 client_order_id 查单")
    finally:
        if acquired_here:
            lock.release()

    _attach_auto_query(result, query_result)
    query_data = query_result.get("data") if isinstance(query_result, dict) else None
    logger.info("[ORDER] 自动核验 coid=%s method=%s result=%s/%s state=%s resolution=%s",
                client_order_id, method, query_result.get("status"), query_result.get("code"),
                query_data.get("state") if isinstance(query_data, dict) else None,
                query_data.get("resolution") if isinstance(query_data, dict) else None)


async def handle_call(
    frame: dict[str, Any],
    backend: WinThsBackend,
) -> dict[str, Any]:
    """处理 RPC call 帧，返回 reply 帧"""
    frame_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params", {})

    reply = {"type": "reply", "id": frame_id}

    if not isinstance(params, dict):
        msg = "params 必须是对象"
        reply["ok"] = False
        reply["result"] = contract.fail(
            contract.CODE_INVALID_PARAMS, contract.CLS_INVALID_PARAMS, msg,
            data={"submitted": False},
        )
        reply["error"] = msg
        return reply

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
        "list_accounts",
        "buy",
        "sell",
        "cancel",
        "confirm_external_cancel",
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

    if method == "cancel" and (
        not isinstance(params.get("entrust_no"), str)
        or not params["entrust_no"].strip()
    ):
        msg = "cancel 缺少有效 entrust_no，未执行撤单"
        reply["ok"] = False
        reply["result"] = contract.fail(
            contract.CODE_INVALID_PARAMS,
            contract.CLS_INVALID_PARAMS,
            msg,
            data={"submitted": False},
        )
        reply["error"] = msg
        return reply

    # 买卖契约校验必须早于台账 reserve、win_lock 和 backend 调用；尤其不能
    # 让缺失 order_type 的请求沿用旧的 price 推断路径。
    if method in ("buy", "sell"):
        validation_error = _validate_buy_sell_params(method, params)
        if validation_error is not None:
            reply["ok"] = False
            reply["result"] = contract.fail(
                contract.CODE_INVALID_PARAMS,
                contract.CLS_INVALID_PARAMS,
                validation_error,
                data={"submitted": False},
            )
            reply["error"] = validation_error
            return reply

    # --- C5a 幂等：下单/撤单必须带业务级 ID，在**拿锁之前**查台账。重发直接
    # 返回首次回执，连排队都不用排，更不会走到点提交那一步。
    reserved_coid: Optional[str] = None
    # 当首次 cancel 只返回人工确认提示时，同 coid 重放可以安全刷新令牌。它不是
    # 新的台账预留，锁忙时不得删除原记录。
    refreshing_confirmation_prompt = False
    confirmation_prompt_token: Optional[str] = None
    confirmed_target_entrust_no: Optional[str] = None
    real_submission_started = False
    if method in IDEMPOTENT_METHODS:
        coid = params.get("client_order_id")
        if not isinstance(coid, str) or _CLIENT_ORDER_ID_RE.fullmatch(coid) is None:
            logger.warning("[ORDER] 拒绝无效 client_order_id method=%s frame_id=%s coid=%r",
                           method, frame_id, coid)
            msg = (f"{method} 的 client_order_id 格式无效，必须为 "
                   "gl-<小写 UUID v7>（如 gl-0198f6a1-0001-7000-8000-000000000001）；"
                   "已拒绝执行")
            reply["ok"] = False
            reply["result"] = contract.fail(contract.CODE_INVALID_PARAMS,
                                            contract.CLS_INVALID_PARAMS, msg,
                                            data={"submitted": False})
            reply["error"] = msg
            return reply
        if method == "confirm_external_cancel" and (
            not isinstance(params.get("confirmation_token"), str)
            or not params["confirmation_token"].strip()
        ):
            msg = "confirm_external_cancel 缺少 confirmation_token，未执行撤单"
            reply["ok"] = False
            reply["result"] = contract.fail(
                contract.CODE_INVALID_PARAMS,
                contract.CLS_INVALID_PARAMS,
                msg,
                data={"submitted": False},
            )
            reply["error"] = msg
            return reply
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
            if (
                method == "cancel"
                and cfg.external_cancel_confirmation
                == _config.EXTERNAL_CANCEL_CONFIRMATION_TWO_STEP
                and _is_unsubmitted_external_cancel_prompt(record)
            ):
                # confirmation_required 意味着尚未发送撤单；重新读表、换发令牌
                # 仍然是同一笔未提交动作的安全重放，不会点击 GUI。
                reserved_coid = coid
                refreshing_confirmation_prompt = True
                logger.info("[RPC] 刷新未登记订单撤单确认 coid=%s", coid)
            else:
                result = _replay_receipt(coid, record)
                await _auto_query_after_unconfirmed(backend, method, coid, result, lock_held=False)
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
            if reserved_coid and not refreshing_confirmation_prompt:
                _release_reservation(backend, reserved_coid)
            reply["ok"] = False
            reply["result"] = result
            reply["error"] = msg
            return reply
    try:
        # 账户文本是同花顺当前交易账户的唯一已确认身份信号。每个真实交易路径
        # 均在持有 win_lock 后、读取订单表/消费人工撤单令牌/发送任何 UI 输入前
        # 重新核验；首次成功读取建立本进程基线，之后文本变化一律阻断。
        if method in ORDER_METHODS:
            account_preflight = await backend.verify_account_for_trade()
            if not contract.is_succeed(account_preflight):
                if reserved_coid and not refreshing_confirmation_prompt:
                    _release_reservation(backend, reserved_coid)
                    reserved_coid = None
                reply["ok"] = False
                reply["result"] = account_preflight
                reply["error"] = ((account_preflight.get("error") or {}).get("message")
                                  or "交易前账户核验失败")
                return reply

        # 上一笔调用超时（疑似弹窗阻塞）后进入 degraded：先清残留弹窗再干活。
        # 清扫失败不阻断本次调用。
        if getattr(backend, "degraded", False):
            try:
                await asyncio.to_thread(backend.dialog_cleanup)
            except Exception:
                logger.exception("degraded dialog_cleanup 失败")
            backend.degraded = False

        async def _invoke() -> Any:
            nonlocal confirmation_prompt_token, confirmed_target_entrust_no, real_submission_started
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
                real_submission_started = True
                r = await fn(stock_no, amount, price, client_order_id)
                _eno = ((r or {}).get("data") or {}).get("entrust_no")
                if _eno:
                    backend.agent_entrust_nos.add(str(_eno))
                return r
            if method == "cancel":
                entrust_no = params["entrust_no"].strip()
                if (
                    cfg.external_cancel_confirmation
                    == _config.EXTERNAL_CANCEL_CONFIRMATION_DIRECT
                ):
                    real_submission_started = True
                    return await backend.cancel(entrust_no)

                try:
                    is_registered = await asyncio.to_thread(led.has_entrust_no, entrust_no)
                except LedgerUnavailable as e:
                    return contract.fail(
                        contract.CODE_LEDGER_UNAVAILABLE,
                        contract.CLS_LEDGER_UNAVAILABLE,
                        f"读取本机下单台账失败，已停止撤单：{e}",
                        data={"submitted": False, "entrust_no": entrust_no},
                    )
                if is_registered:
                    real_submission_started = True
                    return await backend.cancel(entrust_no)

                snapshot, read_failure = await _read_external_cancel_target(
                    backend, entrust_no
                )
                if read_failure is not None:
                    return read_failure
                assert snapshot is not None
                confirmation_prompt_token, _ = _issue_external_cancel_confirmation(
                    params["client_order_id"], snapshot, snapshot
                )
                return _confirmation_required(
                    "该委托未由本系统登记。已读取到订单摘要，但尚未执行撤单；"
                    "请向用户展示摘要并调用 confirm_external_cancel 明确确认。",
                    {
                        "entrust_no": entrust_no,
                        "order": snapshot,
                        "confirmation_token": confirmation_prompt_token,
                        "confirmation_expires_in_secs": int(
                            EXTERNAL_CANCEL_CONFIRMATION_TTL_SECS
                        ),
                    },
                )
            if method == "confirm_external_cancel":
                token_state, confirmation = _consume_external_cancel_confirmation(
                    params["confirmation_token"]
                )
                if token_state != "ok" or confirmation is None:
                    return _external_cancel_confirmation_error(token_state)

                binding = confirmation.get("binding")
                if not isinstance(binding, dict):
                    return _external_cancel_confirmation_error("missing")
                entrust_no = str(binding.get("entrust_no") or "").strip()
                if not entrust_no:
                    return _external_cancel_confirmation_error("missing")
                confirmed_target_entrust_no = entrust_no

                current, read_failure = await _read_external_cancel_target(
                    backend, entrust_no
                )
                if read_failure is not None:
                    return read_failure
                assert current is not None
                if not _external_cancel_binding_matches(binding, current):
                    return _confirmation_required(
                        "确认前复核发现订单已变化，未执行撤单；请重新发起 cancel 并展示最新摘要。",
                        {
                            "entrust_no": entrust_no,
                            "expected_order": binding,
                            "current_order": current,
                        },
                    )

                real_submission_started = True
                r = await backend.cancel(entrust_no)
                if isinstance(r, dict):
                    data = r.get("data")
                    if isinstance(data, dict):
                        data.setdefault("entrust_no", entrust_no)
                    elif data is None:
                        r["data"] = {"entrust_no": entrust_no}
                return r
            if method == "switch_account":
                return await backend.switch_account(params.get("slot"))
            if method == "list_accounts":
                return await backend.list_accounts()
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
            if confirmation_prompt_token:
                # 令牌尚未安全写入/返回给调用方，不能留下不可审计的可执行授权。
                _invalidate_external_cancel_confirmation(confirmation_prompt_token)
            # wait_for 只取消了等待协程——to_thread 起的工作线程取消不掉，它还在
            # 发全局按键，而下面 finally 马上要放 win_lock 让下一笔进场。作废代次，
            # 让那个线程在下一个检查点（翻页/抓表/弹窗/提交）自己停手，
            # 否则两个线程同击一个 xiadan 窗口 → 抓错表、抢弹窗。
            invalidate = getattr(backend, "invalidate_inflight", None)
            if invalidate:
                invalidate(f"{method} 超过 {CALL_TIMEOUT_SECS}s 未完成")
            logger.error("[RPC] %s 超过 %ss 未完成，标记 degraded，回 unknown",
                         method, CALL_TIMEOUT_SECS)
            if method in ORDER_METHODS and real_submission_started:
                if method in CANCEL_METHODS:
                    message = (
                        "受控端处理超时（疑似弹窗或客户端无响应），撤单动作可能已提交。"
                        "安全动作=用同一 client_order_id 原样重发（幂等，不会第二次撤单），"
                        "或查看本次 data.auto_query / 调 query_order 核验目标委托；勿换新 id 重撤")
                else:
                    message = (
                        "受控端处理超时（疑似弹窗或客户端无响应），委托可能已提交。"
                        "安全动作=用同一 client_order_id 原样重发（幂等，不会重复下单），"
                        "或调 query_order/orders_active 核实；勿改单重下")
                result = contract.submitted_unconfirmed(
                    message,
                    data={"submitted": True})
            else:
                result = contract.fail(
                    contract.CODE_CALL_TIMEOUT, contract.CLS_CALL_TIMEOUT,
                    "受控端处理超时，但尚未开始真实交易动作；请稍后重新发起请求",
                    data={"submitted": False},
                )

        if not isinstance(result, dict) or "status" not in result:
            result = contract.fail(contract.CODE_INTERNAL_ERROR,
                                   contract.CLS_INTERNAL_ERROR,
                                   f"受控端返回了非契约形态：{type(result).__name__}")

        # 下单类：回填 client_order_id 并把首次回执落台账（幂等重发就靠它）。
        if reserved_coid:
            settled_coid = reserved_coid
            if isinstance(result.get("data"), dict):
                result["data"]["client_order_id"] = settled_coid
            elif result.get("data") is None:
                result["data"] = {"client_order_id": settled_coid}
            entrust_no = (result.get("data") or {}).get("entrust_no")
            if method in CANCEL_METHODS:
                target_entrust_no = (
                    str(params.get("entrust_no") or "").strip()
                    if method == "cancel"
                    else confirmed_target_entrust_no
                )
                if target_entrust_no and isinstance(result.get("data"), dict):
                    result["data"].setdefault("entrust_no", target_entrust_no)
                # cancel 的请求指纹本身保存了目标编号；confirm 的参数只含令牌，
                # 必须把经过复核的目标编号写入台账，供 query_order 精确核验。
                entrust_no = None if method == "cancel" else target_entrust_no
            try:
                led = _ledger_or_none(backend)
                should_refresh_receipt = (
                    result.get("code") == contract.CODE_CONFIRMATION_REQUIRED
                    and isinstance(result.get("data"), dict)
                    and "confirmation_token" in result["data"]
                )
                if refreshing_confirmation_prompt and not should_refresh_receipt:
                    # 本次刷新没能生成新令牌（例如读表失败）。保留原提示记录，
                    # 以便同一个 cancel ID 之后安全再试。
                    reserved_coid = None
                elif led is not None:
                    await asyncio.to_thread(led.complete, settled_coid,
                                            _receipt_for_ledger(result),
                                            str(entrust_no) if entrust_no else None)
                reserved_coid = None   # 已落定，finally 不再回滚
            except LedgerUnavailable:
                if real_submission_started:
                    # 单已经下出去了，台账却写不进——绝不静默：明确降级为「结果不可知」，
                    # 逼调用方去核单，而不是让它以为下单成功。
                    logger.exception(
                        "台账回写失败 coid=%s，回执降级为 unknown_outcome", reserved_coid
                    )
                    result = contract.submitted_unconfirmed(
                        "委托已提交，但台账回写失败——本次结果无法保证可幂等重放，"
                        "请立即用 orders_active/orders_filled 人工核单",
                        data={"submitted": True, "client_order_id": settled_coid},
                    )
                else:
                    if confirmation_prompt_token:
                        _invalidate_external_cancel_confirmation(confirmation_prompt_token)
                    logger.exception(
                        "台账回写失败 coid=%s，未开始真实交易，撤销确认授权", reserved_coid
                    )
                    result = contract.fail(
                        contract.CODE_LEDGER_UNAVAILABLE,
                        contract.CLS_LEDGER_UNAVAILABLE,
                        "台账无法保存本次确认回执，未执行撤单；请检查后重新发起请求",
                        data={"submitted": False, "client_order_id": settled_coid},
                    )
                reserved_coid = None

            # 未知时只做一次本地只读核验；不调用 buy/sell/cancel，因此不会自动重发。
            await _auto_query_after_unconfirmed(backend, method, settled_coid, result,
                                                lock_held=True)

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
