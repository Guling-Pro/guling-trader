"""SharedState 自更新字段测试（不涉及 Tk 渲染，纯数据面）"""


def test_shared_state_self_update_fields_default():
    from trader.main_window import SharedState

    state = SharedState()
    snap = state.snapshot()

    assert snap["self_update_info"] is None
    assert snap["self_update_progress"] is None
    assert snap["self_update_status"] == "idle"


def test_shared_state_self_update_fields_roundtrip():
    from trader.main_window import SharedState
    from trader.selfupdate.check import UpdateInfo

    state = SharedState()
    info = UpdateInfo(
        tag="v0.6.0", current_version="0.5.0", latest_version="0.6.0",
        exe_url="https://example.com/guling-trader.exe",
        sha256_url="https://example.com/guling-trader.exe.sha256",
    )

    state.update(self_update_info=info, self_update_progress=(10, 100), self_update_status="downloading")
    snap = state.snapshot()

    assert snap["self_update_info"] is info
    assert snap["self_update_info"].latest_version == "0.6.0"
    assert snap["self_update_progress"] == (10, 100)
    assert snap["self_update_status"] == "downloading"
