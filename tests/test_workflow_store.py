"""运行记录落盘：跑完的流程要能回看（离线）。"""

from __future__ import annotations

import json

from agentcode.workflow import Workflow, WorkflowEngine
from agentcode.workflow.store import list_runs, load_run, save_run
from tests.workflow_stubs import ScriptedAgent


def _workflow(name: str = "demo") -> Workflow:
    from agentcode.workflow import Node

    return Workflow(
        name=name,
        title="演示流程",
        start="only",
        nodes=(Node(name="only", agent="echo", task="回显 ${text}", output="answer"),),
    )


def _run(tmp_path, text: str = "你好"):
    engine = WorkflowEngine(_workflow(), agent_runner=ScriptedAgent({"echo": f"收到 {text}"}))
    return engine.run({"text": text})


def test_a_run_is_saved_as_readable_json(tmp_path):
    run = _run(tmp_path)

    path = save_run(run, directory=tmp_path)

    assert path.name == f"{run.run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"] == run.run_id
    assert payload["status"] == "succeeded"
    assert payload["state"]["answer"] == "收到 你好"
    # 中文不转义，方便直接看日志
    assert "收到 你好" in path.read_text(encoding="utf-8")


def test_a_saved_run_can_be_loaded_back(tmp_path):
    run = _run(tmp_path, "再试一次")
    save_run(run, directory=tmp_path)

    loaded = load_run(run.run_id, directory=tmp_path)

    assert loaded is not None
    assert loaded["run_id"] == run.run_id
    assert loaded["nodes"][0]["name"] == "only"
    assert loaded["state"]["text"] == "再试一次"


def test_loading_an_unknown_run_returns_none(tmp_path):
    assert load_run("没有这个记录", directory=tmp_path) is None


def test_runs_are_listed_newest_first(tmp_path):
    first = _run(tmp_path, "第一次")
    save_run(first, directory=tmp_path)
    second = _run(tmp_path, "第二次")
    save_run(second, directory=tmp_path)

    runs = list_runs(directory=tmp_path)

    assert [item["run_id"] for item in runs] == [second.run_id, first.run_id]
    assert runs[0]["workflow"] == "demo"
    assert runs[0]["status"] == "succeeded"
    assert "nodes" not in runs[0]  # 列表只给摘要，不带节点明细


def test_listing_an_empty_directory_is_not_an_error(tmp_path):
    assert list_runs(directory=tmp_path / "还没有") == []


def test_a_label_can_be_used_to_tell_runs_apart(tmp_path):
    """同一个流程跑很多次时，得能看出"这次是给谁跑的"。"""
    run = _run(tmp_path, "带标签的")
    save_run(run, directory=tmp_path, label="给老师演示")

    summary = list_runs(directory=tmp_path)[0]

    assert summary["label"] == "给老师演示"
