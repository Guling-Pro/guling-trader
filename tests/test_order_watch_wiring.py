"""接线回归：main 引用 order_watch 且任务符号存在。"""
import inspect

import trader.main as main
from trader import order_watch


def test_main_imports_order_watch_and_wires_task():
    assert hasattr(order_watch, "order_watch_task")
    src = inspect.getsource(main._async_main)
    assert "order_watch.order_watch_task" in src      # 已拉起
    assert src.count("order_watch") >= 2              # create_task + finally cancel
