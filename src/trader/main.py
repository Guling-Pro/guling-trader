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
from pathlib import Path
from typing import Optional


# ---- 文件日志 + stderr/stdout 重定向 ----
# PyInstaller --windowed 模式 stdout/stderr 被吞掉，wine 下 print 全部消失。
# 启动期就把所有输出写到 %APPDATA%\guling-trader\trader.log（每次启动覆盖）。
# 这是 wine/CrossOver 用户的唯一诊断渠道——异常 traceback 也会写进去。

def _setup_file_logging() -> Path:
    if platform.system() == "Windows":
        log_dir = Path(os.environ.get("APPDATA", "")) / "guling-trader"
    else:
        log_dir = Path.home() / ".config" / "guling-trader"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "trader.log"
    # 'w' 每次启动新建——避免日志无限增长
    log_fh = open(log_file, "w", encoding="utf-8", buffering=1)

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

    # 3) 顶层异常都写到日志（uncaught exception hook）
    def _excepthook(exc_type, exc_value, exc_tb):
        print("\n=== UNCAUGHT EXCEPTION ===", file=sys.stderr)
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
        sys.stderr.flush()
        log_fh.flush()

    sys.excepthook = _excepthook

    return log_file


_LOG_FILE = _setup_file_logging()


from . import bootstrap, config as trader_config, tray, ui_dialogs, ws_client
from .installer import auto_install
from .main_window import MainWindow, SharedState

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

            # Step 1: hexin
            step1 = _check_hexin_running()
            if not step1:
                if snap["ths_steps_complete"] != 0:
                    state.update(ths_steps_complete=0, ths_expanded=True)
                continue

            # Step 2: xiadan
            xiadan_path = _check_xiadan_running()
            if not xiadan_path:
                if snap["ths_steps_complete"] != 1:
                    state.update(ths_steps_complete=1, ths_expanded=True)
                continue

            # Step 3: 旧版窗口
            if platform.system() == "Windows":
                win_result = bootstrap._detect_xiadan_window("网上股票交易系统5.0")
            else:
                win_result = None
            if not win_result:
                if snap["ths_steps_complete"] != 2:
                    state.update(ths_steps_complete=2, ths_expanded=True)
                continue

            # Step 4: 路径有效
            if snap["ths_steps_complete"] < 3:
                state.update(ths_steps_complete=3, ths_expanded=True)
            # 更新 xiadan_path 如果还没有
            if not snap.get("xiadan_path"):
                state.update(xiadan_path=xiadan_path)
                state.log(f"✓ 检测到 xiadan：{xiadan_path}")

            if snap["ths_steps_complete"] < 4:
                state.update(ths_steps_complete=4, ths_expanded=False)
                state.log("✓ 自检完成 · xiadan 就绪")

            # 已折叠后继续轮询监听进程断连（loop 继续）

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

    # Step 2: 升级检查
    try:
        await bootstrap.maybe_upgrade_async(on_event=on_event)
    except Exception as e:
        logger.warning("升级检查失败: %s", e)

    # Step 3: 冲突检测
    if platform.system() == "Windows" and bootstrap_result.found_xiadan_path:
        _check_xiadan_conflict(state)

    # Step 4: WS 连接
    def on_ws_state_change(s) -> None:
        s_name = s.name if hasattr(s, "name") else str(s)
        state.update(connection_state=s_name)
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

    state.log("连接 guling.pro...")
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
        try:
            await asyncio.gather(polling_task, refresh_task, return_exceptions=True)
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


def run() -> None:
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
    state.update(xiadan_path=result.found_xiadan_path)

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
            state.log("⚠ xiadan 路径未知。先点「指定路径...」选 xiadan.exe，或「下载同花顺」装一份")

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
                state.log("⚠ 重新检测未找到 xiadan。点「下载同花顺」或「指定路径...」")
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

    def on_main_exit() -> None:
        # asyncio thread 是 daemon，主线程退出它就会停
        pass

    # tray manager（辅助；wine 下可能不可见但不阻塞主流程）
    tray_mgr: Optional[tray.TrayManager] = None
    try:
        tray_config = tray.TrayConfig(
            xiadan_path=result.found_xiadan_path,
            on_exit=on_main_exit,
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
