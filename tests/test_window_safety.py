"""窗口身份与前台保护必须在非 Windows CI 中可验证。"""

from types import SimpleNamespace

import pytest

from trader.ths import win as w
from trader.ths.win import WindowSafetyError, WinThsBackend


def _bound_backend(monkeypatch):
    backend = WinThsBackend()
    backend.hwnd_main = 100
    backend._bound_pid = 7
    backend._bound_executable = r"c:\\ths\\xiadan.exe"
    monkeypatch.setattr(backend, "_bound_window_is_valid", lambda: True)
    monkeypatch.setattr(
        w,
        "_window_process_identity",
        lambda hwnd: (7, r"c:\\ths\\xiadan.exe") if hwnd in {100, 200} else (99, r"c:\\other.exe"),
    )
    return backend


def _mouse_api(events):
    return SimpleNamespace(
        SetCursorPos=lambda pos: events.append(("move", pos)),
        mouse_event=lambda *args: events.append(("mouse", args)),
        PostMessage=lambda *args: events.append(("post", args)),
    )


def _con():
    return SimpleNamespace(MOUSEEVENTF_LEFTDOWN=2, MOUSEEVENTF_LEFTUP=4, BM_CLICK=0xF5)


def test_global_hotkey_is_not_sent_when_bound_window_cannot_take_foreground(monkeypatch):
    backend = _bound_backend(monkeypatch)
    keys = []
    monkeypatch.setattr(backend, "_activate_bound_window", lambda: False)
    monkeypatch.setattr(w, "hot_key", lambda value, before_dispatch=None: keys.append(value))

    with pytest.raises(WindowSafetyError):
        backend._send_hotkey(["enter"], "test_submit")

    assert keys == []


def test_hotkey_rechecks_focus_immediately_before_key_dispatch(monkeypatch):
    backend = _bound_backend(monkeypatch)
    keys = []
    foreground = [100]
    monkeypatch.setattr(w, "_foreground_window", lambda: foreground[0])

    def delayed_hotkey(value, before_dispatch=None):
        foreground[0] = 999
        before_dispatch()
        keys.append(value)

    monkeypatch.setattr(w, "hot_key", delayed_hotkey)

    with pytest.raises(WindowSafetyError):
        backend._send_hotkey(["enter"], "test_submit")

    assert keys == []


def test_captcha_hotkey_requires_the_exact_popup_not_any_bound_window(monkeypatch):
    backend = _bound_backend(monkeypatch)
    keys = []
    monkeypatch.setattr(w, "_foreground_window", lambda: 100)
    monkeypatch.setattr(backend, "_activate_owned_window", lambda hwnd: False)
    monkeypatch.setattr(w, "hot_key", lambda value, before_dispatch=None: keys.append(value))

    with pytest.raises(WindowSafetyError):
        backend._send_hotkey(["enter"], "captcha_confirm", expected_popup=200)

    assert keys == []


def test_generation_change_during_hotkey_delay_aborts_before_dispatch(monkeypatch):
    backend = _bound_backend(monkeypatch)
    keys = []
    monkeypatch.setattr(w, "_foreground_window", lambda: 100)

    def delayed_hotkey(value, before_dispatch=None):
        backend.invalidate_inflight()
        before_dispatch()
        keys.append(value)

    monkeypatch.setattr(w, "hot_key", delayed_hotkey)

    result = backend._run_guarded(
        lambda: backend._send_hotkey(["enter"], "test_submit")
    )

    assert keys == []
    assert result["code"] == "aborted"


def test_click_rechecks_focus_after_pointer_move_before_button_down(monkeypatch):
    backend = _bound_backend(monkeypatch)
    events = []
    monkeypatch.setattr(w, "win32api", _mouse_api(events), raising=False)
    monkeypatch.setattr(w, "win32con", _con(), raising=False)
    monkeypatch.setattr(backend, "_activate_bound_window", lambda: True)
    monkeypatch.setattr(w, "_foreground_window", lambda: 999)

    with pytest.raises(WindowSafetyError):
        backend._click_screen(12, 34, "test_click")

    assert events == [("move", (12, 34))]


def test_direct_button_message_requires_control_from_bound_process(monkeypatch):
    backend = _bound_backend(monkeypatch)
    events = []
    monkeypatch.setattr(w, "win32api", _mouse_api(events), raising=False)
    monkeypatch.setattr(w, "win32con", _con(), raising=False)

    with pytest.raises(WindowSafetyError):
        backend._post_owned_button_click(999, "test_button")

    assert events == []


def test_cancel_stops_before_second_click_when_focus_guard_rejects_it(monkeypatch):
    backend = _bound_backend(monkeypatch)
    clicks = []
    monkeypatch.setattr(backend, "switch_to_normal", lambda **kwargs: None)
    monkeypatch.setattr(backend, "_send_hotkey", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "refresh", lambda **kwargs: None)
    monkeypatch.setattr(backend, "get_right_hwnd", lambda: 10)
    monkeypatch.setattr(backend, "_find_grid", lambda hwnd: 20)
    monkeypatch.setattr(
        backend,
        "read_table_text",
        lambda hwnd: "委托编号\t证券代码\t\r\n42\t600000\t\r\n",
    )
    monkeypatch.setattr(backend, "_require_owned_window_for_input", lambda *args: None)
    monkeypatch.setattr(w, "win32gui", SimpleNamespace(GetWindowRect=lambda hwnd: (0, 0, 100, 100)), raising=False)
    monkeypatch.setattr(w, "sleep_time", 0)

    def click(*args):
        clicks.append(args)
        if len(clicks) == 2:
            raise WindowSafetyError("focus lost before second click")

    monkeypatch.setattr(backend, "_click_screen", click)
    result = backend._do_cancel("42")

    assert len(clicks) == 2
    assert result["status"] == "failed"
    assert result["data"]["submitted"] is False


def test_query_navigation_keeps_its_existing_unblocked_mode(monkeypatch):
    backend = WinThsBackend()
    events = []
    monkeypatch.setattr(backend, "_require_owned_window_for_input", lambda *args: pytest.fail("read was blocked"))
    monkeypatch.setattr(backend, "get_left_bottom_tabs", lambda: 33)
    monkeypatch.setattr(w, "win32api", _mouse_api(events), raising=False)
    monkeypatch.setattr(w, "win32con", _con(), raising=False)
    monkeypatch.setattr(w, "win32gui", SimpleNamespace(GetWindowRect=lambda hwnd: (0, 0, 100, 100)), raising=False)
    monkeypatch.setattr(w, "_activate_window", lambda hwnd: None)
    monkeypatch.setattr(w, "sleep_time", 0)

    backend.switch_to_normal()

    assert [event[0] for event in events] == ["move", "mouse", "mouse"]
