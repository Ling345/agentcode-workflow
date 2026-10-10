"""Workflow 的共享状态：Agent 之间的"消息"就存在这里（离线）。"""

from __future__ import annotations

import json
import threading

from agentcode.workflow import WorkflowState


def test_values_can_be_written_and_read():
    state = WorkflowState({"request": "写一个计算器"})

    state.set("design", "分三层：接口、实现、测试")

    assert state.get("request") == "写一个计算器"
    assert state.get("design") == "分三层：接口、实现、测试"
    assert state.get("没有这个键", "兜底") == "兜底"


def test_snapshot_is_a_copy_not_the_live_state():
    """快照要能安全地交给界面/日志，改它不能反过来改运行中的状态。"""
    state = WorkflowState()
    state.set("a", 1)

    snapshot = state.snapshot()
    snapshot["a"] = 999
    snapshot["b"] = "偷偷写的"

    assert state.get("a") == 1
    assert state.get("b") is None


def test_update_writes_many_keys_at_once():
    state = WorkflowState()

    state.update({"a": 1, "b": 2}, source="node-x")

    assert state.get("a") == 1
    assert state.get("b") == 2
    assert state.history()[-1]["source"] == "node-x"


def test_render_fills_placeholders_from_state():
    state = WorkflowState({"request": "写一个计算器", "language": "Python"})

    rendered = state.render("用 ${language} 完成：${request}")

    assert rendered == "用 Python 完成：写一个计算器"


def test_render_stringifies_objects_readably():
    state = WorkflowState({"files": ["a.py", "b.py"], "ok": True, "rows": {"n": 3}, "none": None})

    assert state.render("${files}") == json.dumps(["a.py", "b.py"], ensure_ascii=False)
    assert state.render("${rows}") == json.dumps({"n": 3}, ensure_ascii=False)
    assert state.render("${ok}") == "true"
    assert state.render("${none}") == ""


def test_render_keeps_unknown_placeholders_visible():
    """变量名写错时要看得见，而不是被替换成空白、让人以为节点没跑。"""
    state = WorkflowState()

    assert state.render("你好 ${查无此变量}") == "你好 ${查无此变量}"


def test_history_records_who_wrote_what():
    state = WorkflowState()
    state.set("requirements", "需求原文", source="requirements")
    state.set("design", "设计稿", source="design")

    history = state.history()

    assert [item["key"] for item in history] == ["requirements", "design"]
    assert [item["source"] for item in history] == ["requirements", "design"]


def test_parallel_writes_do_not_lose_anything():
    """并行分支会同时往状态里写：加锁之前这条会掉数据。"""
    state = WorkflowState()

    def worker(prefix: str) -> None:
        for index in range(50):
            state.set(f"{prefix}-{index}", index)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(state.keys()) == 150
    assert state.get("c-49") == 49
