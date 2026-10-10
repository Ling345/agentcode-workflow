"""在线运行（网页实时看进度用）。"""

from __future__ import annotations

import time

from agentcode.workflow import Edge, Node, Workflow
from agentcode.workflow.live import LiveRuns
from tests.workflow_stubs import ScriptedAgent, ScriptedCommand


def _workflow() -> Workflow:
    return Workflow(
        name="live-demo",
        title="在线演示",
        start="first",
        nodes=(
            Node(name="first", agent="planner", task="第一步 ${request}", output="one", edges=(Edge("second"),)),
            Node(name="second", agent="coder", task="第二步 ${one}", output="two"),
        ),
    )


def test_events_are_published_while_the_flow_runs(tmp_path):
    live_runs = LiveRuns()
    live = live_runs.start(
        _workflow(),
        {"request": "你好"},
        settings=None,
        agent_runner=ScriptedAgent({"planner": "要点", "coder": "实现"}),
        command_runner=ScriptedCommand(),
    )

    events = list(live_runs.subscribe(live))

    types = [event["type"] for event in events]
    assert types[0] == "start"
    assert "node" in types
    assert types[-1] == "saved"
    assert live.status == "succeeded"
    node_events = [event for event in events if event["type"] == "node"]
    assert node_events[0]["data"]["name"] == "first"
    assert [item["data"]["status"] for item in node_events] == ["running", "ok", "running", "ok"]


def test_a_finished_run_can_still_be_read_from_memory(tmp_path):
    live_runs = LiveRuns()
    live = live_runs.start(
        _workflow(),
        {},
        settings=None,
        agent_runner=ScriptedAgent(),
        command_runner=ScriptedCommand(),
    )
    list(live_runs.subscribe(live))  # 等它跑完

    again = live_runs.get(live.run_id)
    assert again is not None
    assert again.finished is True
    # 迟到的订阅者能补上全部历史事件
    assert [event["type"] for event in live_runs.subscribe(again)][-1] == "saved"
    assert live_runs.list()[0]["run_id"] == live.run_id


def test_an_unknown_run_id_returns_none():
    assert LiveRuns().get("没有这个运行") is None


def test_the_record_is_saved_to_the_trace_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TRACE_DIR", str(tmp_path / "traces"))
    from agentcode.config import Settings
    from agentcode.workflow.store import list_runs

    settings = Settings.from_env()
    live_runs = LiveRuns()
    live = live_runs.start(
        _workflow(),
        {"request": "落盘"},
        settings=settings,
        agent_runner=ScriptedAgent(),
        command_runner=ScriptedCommand(),
    )
    for _ in range(100):
        if live.finished:
            break
        time.sleep(0.05)

    assert live.record_path
    assert [item["run_id"] for item in list_runs(settings=settings)] == [live.run_id]
