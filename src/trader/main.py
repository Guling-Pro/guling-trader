"""启动入口：主窗口（主线程 tk mainloop）+ asyncio 后台线程（bootstrap + ws_client）

为什么主窗口是主线程 + asyncio 后台线程：
- pystray tray icon 在 wine/CrossOver 下不渲染，所以 tray 不能作为唯一 UI 入口
- tkinter mainloop 必须主线程跑（tk 限制）
- asyncio loop 跑在后台 daemon thread，通过 SharedState 跟主线程交换数据
- tray icon 仍然启动作为辅助（真 Windows 下用户喜欢托盘），但不阻塞 main UI 可见性
"""
import argparse
import asyncio
import io
import logging
import os
import platform
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---- 文件日志 + stderr/stdout 重定向 ----
# PyInstaller --windowed 模式 stdout/stderr 被吞掉，wine 下 print 全部消失。
# 启动期就把所有输出写到本地配置目录下的 trader.log。
# 这是 wine/CrossOver 用户的唯一诊断渠道——异常 traceback 也会写进去。
#
# 追加 + 滚动，不再每次启动覆盖：2026-08-04 查 08-03 查询串线时，受控端凌晨重启一次，
# 'w' 模式把当天全部 RPC 日志（含每笔 call 的 id/method，正是定位串线归属的关键证据）
# 抹干净，回溯路径直接灭失。日志无限增长的原顾虑改由体积滚动解决。

LOG_MAX_BYTES = 5 * 1024 * 1024   # 单文件上限，超过则滚动
LOG_KEEP = 5                      # 保留 trader.log.1 .. .5（约覆盖最近数个交易日）


def _rotate_logs(log_file: Path) -> None:
    """启动时按体积滚动：trader.log → .1 → .2 …，最老的丢弃。失败不阻断启动。"""
    try:
        if not log_file.exists() or log_file.stat().st_size < LOG_MAX_BYTES:
            return
        oldest = log_file.with_suffix(log_file.suffix + f".{LOG_KEEP}")
        if oldest.exists():
            oldest.unlink()
        for i in range(LOG_KEEP - 1, 0, -1):
            src = log_file.with_suffix(log_file.suffix + f".{i}")
            if src.exists():
                src.rename(log_file.with_suffix(log_file.suffix + f".{i + 1}"))
        log_file.rename(log_file.with_suffix(log_file.suffix + ".1"))
    except Exception:
        pass  # 滚动失败就继续往原文件追加——丢日志比不启动好


def _setup_file_logging() -> Path:
    from . import config as _config  # 延迟导入：本函数在模块顶层 import 之前就被调用
    log_dir = _config.app_data_dir()  # frozen → exe 同级 guling-trader-data/

    log_file = log_dir / "trader.log"
    _rotate_logs(log_file)
    log_fh = open(log_file, "a", encoding="utf-8", buffering=1)
    log_fh.write(f"\n===== trader 启动 {datetime.now().isoformat(timespec='seconds')} =====\n")

    # 1) stdout / stderr 同时写到日志 + 原 sink（windowed 下原 sink 是 /dev/null，无副作用）
    class _Tee(io.TextIOBase):
        def __init__(self, *streams):
            self.streams = [s for s in streams if s is not None]

        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                    s.flush()
                except Exception:
                    pass
            return len(data)

        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    sys.stdout = _Tee(sys.__stdout__, log_fh)
    sys.stderr = _Tee(sys.__stderr__, log_fh)

    # 2) logging 模块也写到日志 + stderr（已 Tee 到文件）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    # 根 logger 维持 INFO，避免 websockets 等依赖把文件日志淹没；项目自己的
    # DEBUG 是排查 UI、表格和弹窗动作所需的完整诊断信息，应写入 trader.log。
    logging.getLogger("trader").setLevel(logging.DEBUG)

    # 3) 顶层异常都写到日志（uncaught exception hook）
    def _excepthook(exc_type, exc_value, exc_tb):
        print("\n=== UNCAUGHT EXCEPTION ===", file=sys.stderr)
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
        sys.stderr.flush()
        log_fh.flush()

    sys.excepthook = _excepthook

    return log_file


_LOG_FILE = _setup_file_logging()


from . import bootstrap, config as trader_config, order_watch, tray, ui_dialogs, ws_client
from .installer import auto_install
from .main_window import MainWindow, SharedState
from . import __version__ as _TRADER_VERSION
from .selfupdate import apply as selfupdate_apply, check as selfupdate_check

if platform.system() == "Windows":
    try:
        import psutil
    except ImportError:
        psutil = None
else:
    psutil = None

logger = logging.getLogger(__name__)
logger.info("=== guling-trader 启动 ===")
logger.info("log file: %s", _LOG_FILE)
logger.info("platform: %s, python: %s", platform.platform(), sys.version)


def _make_installer_event_handler(state: SharedState):
    """生成 installer event 回调，把事件写到 SharedState"""

    def on_event(event: auto_install.InstallerEvent) -> None:
        kind = event.kind
        payload = event.payload or {}

        if kind == "download_progress":
            done = payload.get("bytes_done", 0)
            total = payload.get("bytes_total", 0)
            state.update(install_progress=(done, total))
        elif kind == "install_started":
            state.update(connection_state="INSTALLING")
            state.log(f"开始安装同花顺：{payload.get('path', '?')}")
        elif kind == "install_done":
            state.update(install_progress=None)
            state.log(f"✓ 同花顺安装完成：{payload.get('path', '?')}")
        elif kind == "detected_existing":
            state.log(f"检测到已有同花顺：{payload.get('path', '?')}")
        elif kind == "error":
            state.log(f"⚠ 安装错误：{payload.get('message', '?')}")
        else:
            state.log(f"installer event: {kind} {payload}")

    return on_event


# 跨线程访问 ws_client 实例 + 它跑的 asyncio loop
_ws_client_holder: dict = {"client": None, "loop": None}


async def _pairing_refresh_watcher(state: SharedState, client: ws_client.WsClient) -> None:
    """检测配对码过期 → ws.close() 触发重连申请新码"""
    while True:
        try:
            await asyncio.sleep(5)
            snap = state.snapshot()
            if snap["connection_state"] != "AWAITING_BIND":
                continue
            exp = snap.get("pairing_expires_at")
            if exp is None:
                continue
            import time as _t
            if _t.time() < exp:
                continue
            # 过期
            code = snap.get("pairing_code", "?")
            state.update(ths_refreshing=True)
            state.log(f"配对码 {code} 已过期，重连获取新码...")
            if client.ws is not None:
                try:
                    await client.ws.close()
                except Exception:
                    pass
            # 重连后 on_pair_pending 会被调用，到时 ths_refreshing 会被清除
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("pairing refresh watcher exception: %s", e)


async def _ths_polling_task(state: SharedState) -> None:
    """THS 4步检测循环：2秒周期，exception-safe"""
    while True:
        try:
            await asyncio.sleep(2)
            snap = state.snapshot()
            # 断开/错误时不检测
            if snap["connection_state"] in ("DISCONNECTED", "FATAL"):
                continue

            # 真正需要的是 xiadan「网上股票交易系统5.0」窗口——交易/查询全靠它。
            # hexin（行情软件）并非必需：用户独立打开 xiadan 也能交易。所以优先检测
            # 窗口，窗口在就判就绪，不再卡在 hexin 那步（新机上独立开 xiadan 时 hexin
            # 可能没跑 → 之前向导永远停在 Step 1）。
            if platform.system() == "Windows":
                win_result = bootstrap._detect_xiadan_window("网上股票交易系统5.0")
            else:
                win_result = None
            if win_result:
                if not snap.get("xiadan_path"):
                    xp = _check_xiadan_running()
                    if xp:
                        state.update(xiadan_path=xp)
                        state.log(f"✓ 检测到 xiadan：{xp}")
                if snap["ths_steps_complete"] < 4:
                    state.update(ths_steps_complete=4, ths_expanded=False)
                    state.log("✓ 自检完成 · xiadan 就绪")
                continue

            # 窗口还没出来 → 按 hexin → xiadan 进程 → 窗口 给引导步骤
            if not _check_hexin_running():
                if snap["ths_steps_complete"] != 0:
                    state.update(ths_steps_complete=0, ths_expanded=True)
                continue
            if not _check_xiadan_running():
                if snap["ths_steps_complete"] != 1:
                    state.update(ths_steps_complete=1, ths_expanded=True)
                continue
            # 进程在、窗口还没出来
            if snap["ths_steps_complete"] != 2:
                state.update(ths_steps_complete=2, ths_expanded=True)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("THS polling exception: %s", e)


async def _async_main(
    bootstrap_result: bootstrap.BootstrapResult,
    state: SharedState,
    tray_manager: Optional[tray.TrayManager],
) -> None:
    """asyncio 后台主循环：xiadan ensure → ws_client 连接"""
    on_event = _make_installer_event_handler(state)

    state.log("启动中...")
    state.log(f"设备 ID: {bootstrap_result.config.device_id}")

    # Step 1: xiadan 检测 / 自动安装
    try:
        xiadan_path = await bootstrap.ensure_xiadan_async(on_event=on_event)
        if xiadan_path:
            state.update(xiadan_path=str(xiadan_path))
            state.log(f"✓ xiadan 就绪: {xiadan_path}")
            bootstrap_result.found_xiadan_path = str(xiadan_path)
        else:
            state.log("⚠ xiadan 未找到，交易功能受限")
    except Exception as e:
        state.log(f"⚠ xiadan 准备失败: {e}")
        logger.exception("ensure_xiadan failed")

    # Step 1.5: 自更新孤儿文件清理（上次更新可能留下 .old/.new）
    if platform.system() == "Windows":
        try:
            selfupdate_apply.cleanup_orphan_files(Path(sys.executable).resolve().parent)
        except Exception as e:
            logger.warning("清理自更新孤儿文件失败（非致命）: %s", e)

    # Step 2: 升级检查
    try:
        await bootstrap.maybe_upgrade_async(on_event=on_event)
    except Exception as e:
        logger.warning("升级检查失败: %s", e)

    # Step 2.5: guling-trader 自身更新检查（GitHub Releases，仅 Windows）
    if platform.system() == "Windows":
        try:
            update_info = await selfupdate_check.check_for_update(_TRADER_VERSION)
            if update_info:
                state.update(self_update_info=update_info, self_update_status="idle")
                state.log(f"发现 guling-trader 新版本：v{update_info.latest_version}")
        except Exception as e:
            logger.warning("guling-trader 自更新检查失败: %s", e)

    # Step 3: 冲突检测
    if platform.system() == "Windows" and bootstrap_result.found_xiadan_path:
        _check_xiadan_conflict(state)

    # Step 3.5: 先接通已有 OCR；缺失时的 winget 安装转为后台任务，绝不能阻塞
    # xiadan 窗口检测或网关连接。None=未装；""=PATH；具体路径=已应用。
    if platform.system() == "Windows":
        try:
            from .installer.tesseract import verify_ocr_runnable
            from .ths.win import setup as ths_setup
            tesseract_cmd = bootstrap_result.found_tesseract_cmd
            ths_setup("网上股票交易系统5.0", tesseract_cmd or "", str(trader_config.tmp_dir()))
            if tesseract_cmd is None:
                state.update(ocr_status="installing", ocr_message="正在后台尝试安装")
                state.log("OCR 未安装，正在后台尝试安装；不影响连接和窗口检测")
            else:
                ocr_ok, ocr_info = verify_ocr_runnable()
                if ocr_ok:
                    state.update(ocr_status="ready", ocr_message=str(ocr_info))
                    state.log(f"✓ OCR 就绪（Tesseract {ocr_info}）")
                else:
                    state.update(ocr_status="unavailable", ocr_message="OCR 自检失败")
                    state.log(f"⚠ OCR 自检失败：{ocr_info}（下单验证码可能无法识别）")
        except Exception as e:
            state.update(ocr_status="unavailable", ocr_message="OCR 初始化失败")
            logger.warning("Tesseract 准备失败: %s", e)

    # Step 4: WS 连接
    def on_ws_state_change(s) -> None:
        s_name = s.name if hasattr(s, "name") else str(s)
        cfg = trader_config.load()
        state.update(
            connection_state=s_name,
            account_name=cfg.account_name or "",
            agent_token=cfg.agent_token or None,
        )
        state.log(f"连接状态: {s_name}")
        if tray_manager is not None:
            try:
                tray_manager.set_state(s)
            except Exception:
                pass

    def on_pair_pending(code, expires_at) -> None:
        """收到 pair_pending：把 code + expires_at 推给主窗口显示，清除刷新标志"""
        # expires_at 可能是 ISO 字符串或数字时间戳，统一转 unix timestamp
        from datetime import datetime, timezone
        import time as _time

        exp_ts = None
        if expires_at:
            try:
                if isinstance(expires_at, (int, float)):
                    exp_ts = float(expires_at)
                else:
                    # ISO 字符串如 "2026-05-19T08:05:00.123456Z"
                    # server 发的是 UTC + 'Z'；'Z' 替换成 '+00:00' 让 fromisoformat
                    # 返回 tz-aware datetime，再 .timestamp() 才是正确的 unix 时间戳
                    s = str(expires_at).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        # 兜底：server 没带 tz 时按 UTC 算（server 一直发 UTC）
                        dt = dt.replace(tzinfo=timezone.utc)
                    exp_ts = dt.timestamp()
            except Exception:
                # 兜底：当前时间 +5min
                exp_ts = _time.time() + 300

        state.update(pairing_code=code, pairing_expires_at=exp_ts, ths_refreshing=False)
        state.log(f"✓ 收到配对码：{code}，5 分钟内有效")

    state.log("连接服务器...")
    client = ws_client.WsClient(
        dev_url=os.environ.get("YU_TRADER_DEV_URL"),
        on_state_change=on_ws_state_change,
        on_pair_pending=on_pair_pending,
        on_rpc_log=state.log,
    )
    # 暴露给主线程的「重新配对」按钮用——thread-safe 关 ws 触发重连
    _ws_client_holder["client"] = client
    _ws_client_holder["loop"] = asyncio.get_event_loop()

    # 同时跑 ws_client + THS polling + pairing refresh watcher
    polling_task = asyncio.create_task(_ths_polling_task(state))
    refresh_task = asyncio.create_task(_pairing_refresh_watcher(state, client))
    order_event_task = asyncio.create_task(order_watch.order_watch_task(state, client))
    from . import watchlist_watch
    watchlist_task = asyncio.create_task(watchlist_watch.watchlist_watch_task(state, client))
    ocr_task = None
    if platform.system() == "Windows" and bootstrap_result.found_tesseract_cmd is None:
        async def _install_ocr_in_background() -> None:
            try:
                from .installer.tesseract import ensure_tesseract, verify_ocr_runnable
                from .ths.win import setup as ths_setup

                installed = await ensure_tesseract(on_log=state.log)
                # 用户在安装期间已选择有效的离线程序时，保留其选择和当前状态。
                if trader_config.load().tesseract_path_manual:
                    return
                if installed is None:
                    state.update(ocr_status="unavailable", ocr_message="自动安装失败，可选择本地 tesseract.exe")
                    return
                ths_setup("网上股票交易系统5.0", installed, str(trader_config.tmp_dir()))
                ok, info = verify_ocr_runnable()
                if ok:
                    bootstrap_result.found_tesseract_cmd = installed
                    state.update(ocr_status="ready", ocr_message=str(info))
                    state.log(f"✓ OCR 就绪（Tesseract {info}）")
                else:
                    state.update(ocr_status="unavailable", ocr_message="安装后自检失败")
                    state.log(f"⚠ Tesseract 安装后自检失败：{info}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                state.update(ocr_status="unavailable", ocr_message="自动安装异常，可选择本地 tesseract.exe")
                state.log(f"⚠ Tesseract 后台安装异常：{e}")
                logger.exception("Tesseract 后台安装失败")

        ocr_task = asyncio.create_task(_install_ocr_in_background())
    try:
        await client.run()
    except asyncio.CancelledError:
        state.log("ws_client 已取消")
    except Exception as e:
        state.log(f"⚠ ws_client 异常: {e}")
        logger.exception("ws_client.run failed")
    finally:
        polling_task.cancel()
        refresh_task.cancel()
        order_event_task.cancel()
        watchlist_task.cancel()
        try:
            tasks = [polling_task, refresh_task, order_event_task, watchlist_task]
            if ocr_task is not None:
                ocr_task.cancel()
                tasks.append(ocr_task)
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass


def _check_hexin_running() -> bool:
    """Step 1: hexin.exe 或 ths.exe 进程存活"""
    if platform.system() != "Windows" or psutil is None:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in {"hexin.exe", "ths.exe"}:
                return True
    except Exception:
        pass
    return False


def _check_xiadan_running() -> Optional[str]:
    """Step 2: xiadan.exe 进程存活，返回 exe 路径"""
    if platform.system() != "Windows" or psutil is None:
        return None
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            name = (proc.info.get("name") or "").lower()
            if name == "xiadan.exe":
                return proc.info.get("exe") or None
    except Exception:
        pass
    return None


def _check_xiadan_conflict(state: SharedState) -> None:
    """检查是否有手动启动的同花顺进程"""
    if platform.system() != "Windows" or psutil is None:
        return
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name in {"xiadan.exe", "hexin.exe", "ths.exe"}:
                    state.log(f"⚠ 检测到运行中的同花顺进程: {name}，建议先关闭")
                    return
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except Exception:
        pass


def _run_async_in_thread(coro_factory, state: SharedState) -> threading.Thread:
    """asyncio loop 跑在后台 daemon thread"""

    def thread_target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro_factory())
        except Exception as e:
            state.log(f"⚠ 后台异常: {e}")
            logger.exception("background async loop crashed")
        finally:
            loop.close()

    t = threading.Thread(target=thread_target, daemon=True)
    t.start()
    return t


async def _diagnose() -> None:
    """诊断模式：纯本地探测，无 WS 连接、无 GUI"""
    print("=== guling-trader 诊断 ===\n")

    result = bootstrap.bootstrap()
    print(f"设备 ID:    {result.config.device_id}")
    print(f"已配对:     {result.config.has_paired()}")

    if result.errors:
        print("\n警告:")
        for err in result.errors:
            print(f"  - {err}")

    print()
    if result.found_xiadan_path:
        print(f"✓ xiadan:    {result.found_xiadan_path}")
    else:
        print("✗ xiadan:    未找到")

    if result.found_tesseract_cmd is not None:
        print(f"✓ Tesseract: {result.found_tesseract_cmd or '(PATH)'}")
    else:
        print("✗ Tesseract: 未找到")

    print()
    if platform.system() == "Windows":
        try:
            from .ths.win import WinThsBackend

            backend = WinThsBackend()
            print("尝试 balance()...")
            balance = await backend.balance()
            print(f"  → {balance}")
        except Exception as e:
            print(f"  ✗ balance() 失败: {e}")
    else:
        print("非 Windows 平台，跳过 Win32 后端测试")

    print("\n=== 诊断完成 ===")


_SINGLETON_MUTEX = None  # 持有命名 mutex 句柄，进程存活期间不释放（防单例锁失效）


def _enforce_single_instance() -> bool:
    """Windows 单例锁：已有实例在跑则返回 False。

    重复打开两个 trader 会各自连服务端 → 互相把对方的 session 踢下线
    （evicted_by_other_session 死循环）→ 隧道不稳 → 用户侧报错。用命名 mutex 防呆。
    """
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        global _SINGLETON_MUTEX
        # session-local 命名空间（无 Global\ 前缀），单桌面会话足够，免提权
        _SINGLETON_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "guling-trader-singleton")
        ERROR_ALREADY_EXISTS = 183
        return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True  # 锁机制本身异常不阻断启动


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


def run() -> None:
    # 单例：已有实例则提示并退出，避免双 trader 互踢导致隧道不稳。
    if not _enforce_single_instance():
        if platform.system() == "Windows":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0, "guling-trader 已在运行，请勿重复打开。", "guling-trader", 0x40
                )
            except Exception:
                pass
        logger.warning("已有 guling-trader 实例在运行，退出本次启动")
        sys.exit(0)

    # Windows 任务栏图标：不设 AppUserModelID 时，任务栏按钮认的是 python 进程图标，
    # 而非窗口/exe 图标。早于任何窗口创建前设置，配合 main_window 的 iconbitmap，
    # 标题栏 + 任务栏才会显示 brand 图标。
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("guling.trader")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="guling-trader — Windows 交易终端")
    parser.add_argument("--diagnose", action="store_true", help="诊断模式")
    args = parser.parse_args()

    if args.diagnose:
        try:
            asyncio.run(_diagnose())
        except Exception as e:
            logger.exception("诊断失败: %s", e)
            sys.exit(1)
        return

    try:
        result = bootstrap.bootstrap()
    except Exception as e:
        logger.exception("bootstrap 失败: %s", e)
        sys.exit(1)

    state = SharedState()
    state.update(
        xiadan_path=result.found_xiadan_path,
        account_name=result.config.account_name or "",
        agent_token=result.config.agent_token or None,
        enable_ths_plugin=result.config.enable_ths_plugin,
        external_cancel_confirmation=result.config.external_cancel_confirmation,
    )

    def on_open_xiadan() -> None:
        # 优先用 state 里的（可能已通过 redetect / set_path 更新），fallback bootstrap result
        snap = state.snapshot()
        xiadan = snap.get("xiadan_path") or result.found_xiadan_path
        if xiadan:
            try:
                os.startfile(xiadan)
                state.log("已启动同花顺")
            except Exception as e:
                state.log(f"⚠ 启动同花顺失败: {e}")
        else:
            state.log("⚠ xiadan 路径未知。请选择 xiadan.exe，或从同花顺官方渠道安装后重新检测。")

    def on_redetect_xiadan() -> None:
        """重新检测 xiadan 路径"""
        try:
            from .installer import detect

            found = detect.find_xiadan()
            if found:
                state.update(xiadan_path=str(found))
                result.found_xiadan_path = str(found)
                state.log(f"✓ 重新检测命中：{found}")
            else:
                state.update(xiadan_path=None)
                state.log("⚠ 重新检测未找到 xiadan。请选择 xiadan.exe，或确认已从同花顺官方渠道安装。")
        except Exception as e:
            state.log(f"⚠ 检测异常: {e}")

    def on_set_xiadan_path(path: str) -> None:
        """用户手动指定 xiadan.exe 路径"""
        try:
            from pathlib import Path as _P

            p = _P(path)
            if not p.exists():
                state.log(f"⚠ 指定路径不存在：{path}")
                return
            if not p.is_file():
                state.log(f"⚠ 指定路径不是文件：{path}")
                return
            # 写入 config
            result.config.xiadan_path_manual = str(p)
            trader_config.save(result.config)
            # 更新 state + bootstrap 缓存
            state.update(xiadan_path=str(p))
            result.found_xiadan_path = str(p)
            state.log(f"✓ 已设置 xiadan 路径：{p}")
        except Exception as e:
            state.log(f"⚠ 设置路径失败: {e}")

    def on_set_tesseract_path(path: str) -> None:
        """接入用户离线提供的 tesseract.exe，并立即验证。"""
        try:
            from pathlib import Path as _P
            from .installer.tesseract import verify_ocr_runnable
            from .ths.win import setup as ths_setup

            p = _P(path)
            if not p.is_file():
                state.log(f"⚠ 指定的 Tesseract 路径不是文件：{path}")
                return
            ths_setup("网上股票交易系统5.0", str(p), str(trader_config.tmp_dir()))
            ok, info = verify_ocr_runnable()
            if not ok:
                state.update(ocr_status="unavailable", ocr_message="所选程序无法运行")
                state.log(f"⚠ 所选 Tesseract 无法运行：{info}")
                return
            result.config.tesseract_path_manual = str(p)
            trader_config.save(result.config)
            bootstrap_result.found_tesseract_cmd = str(p)
            state.update(ocr_status="ready", ocr_message=str(info))
            state.log(f"✓ 已设置本地 Tesseract：{p}")
        except Exception as e:
            state.update(ocr_status="unavailable", ocr_message="设置本地 OCR 失败")
            state.log(f"⚠ 设置 Tesseract 路径失败：{e}")

    def on_reset_pair() -> None:
        try:
            # 1. 清 config 中的 pairing 字段
            result.config.agent_token = None
            result.config.account_name = None
            result.config.paired_at = None
            trader_config.save(result.config)
            state.update(pairing_code=None, account_name="", connection_state="UNPAIRED")
            state.log("已清除配对，正在重连服务器申请新配对码...")

            # 2. thread-safe 关当前 ws → ws_client.run 外层 loop 重连 → 因 config 已清
            #    cfg.has_paired() 返 False → 走 pair_init → 新配对码到来
            client = _ws_client_holder.get("client")
            loop = _ws_client_holder.get("loop")
            if client and loop and client.ws is not None:
                asyncio.run_coroutine_threadsafe(client.ws.close(), loop)
        except Exception as e:
            state.log(f"⚠ 重置失败: {e}")

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

    def on_main_exit() -> None:
        # 主窗口自身关闭时的清理钩子；asyncio thread 是 daemon，进程退它就停。
        pass

    def on_tray_exit() -> None:
        # 托盘「退出」从托盘线程触发：调度到 tk 主线程真正销毁窗口 →
        # mainloop 返回 → run() 末尾 sys.exit(0) → 进程退出。
        # 旧实现只 stop 托盘图标、不关 mainloop，导致进程残留、只能任务管理器查杀。
        try:
            mw.root.after(0, mw.root.destroy)
        except Exception:
            os._exit(0)

    # tray manager（辅助；wine 下可能不可见但不阻塞主流程）
    tray_mgr: Optional[tray.TrayManager] = None
    try:
        tray_config = tray.TrayConfig(
            xiadan_path=result.found_xiadan_path,
            on_exit=on_tray_exit,
        )
        tray_mgr = tray.TrayManager(tray_config)
        tray_mgr.start()
    except Exception as e:
        logger.warning("tray icon 未启动 (wine 下正常): %s", e)
        state.log("tray icon 未启动（wine 限制，可忽略）")

    # 后台 asyncio loop
    _run_async_in_thread(lambda: _async_main(result, state, tray_mgr), state)

    # 主线程：MainWindow.mainloop()
    mw = MainWindow(
        state=state,
        on_open_xiadan=on_open_xiadan,
        on_reset_pair=on_reset_pair,
        on_exit=on_main_exit,
        on_redetect_xiadan=on_redetect_xiadan,
        on_set_xiadan_path=on_set_xiadan_path,
        on_set_tesseract_path=on_set_tesseract_path,
        on_apply_self_update=on_apply_self_update,
        minimize_to_tray=(platform.system() == "Windows"),
    )
    # tray「显示窗口」回调在 mw 创建后才能绑定
    if tray_mgr is not None:
        tray_mgr.config.on_show_window = mw.show_window
    try:
        mw.run()
    except KeyboardInterrupt:
        pass

    sys.exit(0)
