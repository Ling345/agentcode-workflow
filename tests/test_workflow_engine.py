"""Workflow 引擎：顺序、分支、循环、并行、重试与错误恢复（离线）。

引擎不直接调模型——真正的模型调用由 ``agent_runner`` 负责。测试里换成脚本
执行器，就能把"编排逻辑"单独钉死，不受模型输出漂移影响。
"""

from __future__ import annotations

import json

import pytest

from agentcode.core.errors import AgentCodeError
from agentcode.workflow import (
    AgentOutput,
    CommandOutput,
    Edge,
    Node,
    Workflow,
    WorkflowEngine,
)
from tests.workflow_stubs import ScriptedAgent, ScriptedCommand


def _engine(workflow: Workflow, agents=None, commands=None, events=None) -> WorkflowEngine:
    return WorkflowEngine(
        workflow,
        agent_runner=agents or ScriptedAgent(),
        command_runner=commands or ScriptedCommand(),
        on_event=(events.append if events is not None else None),
    )


# ---------------------------------------------------------------- 顺序与状态


def test_a_sequential_workflow_passes_state_between_two_agents():
    """这是作业的硬要求：≥2 个 Agent 协作，前一个的产出要能喂给后一个。"""
    workflow = Workflow(
        name="demo",
        title="顺序演示",
        start="requirements",
        nodes=(
            Node(
                name="requirements",
                agent="planner",
                task="把需求整理成要点：${request}",
                output="points",
                edges=(Edge("design"),),
            ),
            Node(
                name="design",
                agent="designer",
                task="按要点做设计：${points}",
                output="design",
                edges=(Edge("done"),),
            ),
            Node(name="done", kind="end"),
        ),
    )
    agents = ScriptedAgent({"planner": "1. 加法 2. 减法", "designer": "分两层：接口 + 实现"})

    run = _engine(workflow, agents=agents).run({"request": "写一个计算器"})

    assert run.status == "succeeded"
    assert [call["task"] for call in agents.calls] == [
        "把需求整理成要点：写一个计算器",
        "按要点做设计：1. 加法 2. 减法",
    ]
    assert run.state["points"] == "1. 加法 2. 减法"
    assert run.state["design"] == "分两层：接口 + 实现"
    assert [item.name for item in run.nodes] == ["requirements", "design", "done"]
    assert run.node("design").status == "ok"
    assert run.node("design").answer == "分两层：接口 + 实现"
    assert run.node("design").usage["total_tokens"] == 7


def test_the_run_record_is_json_serializable():
    workflow = Workflow(
        name="demo",
        start="only",
        nodes=(Node(name="only", agent="echo", task="回显 ${text}", output="answer"),),
    )

    run = _engine(workflow).run({"text": "你好"})

    payload = json.loads(json.dumps(run.to_dict(), ensure_ascii=False))
    assert payload["workflow"] == "demo"
    assert payload["status"] == "succeeded"
    assert payload["nodes"][0]["name"] == "only"
    assert payload["state"]["answer"]


# ---------------------------------------------------------------- 条件分支


def _branch_workflow() -> Workflow:
    """审查 → 通过就收尾，不通过就去修。"""
    return Workflow(
        name="branch",
        start="review",
        nodes=(
            Node(
                name="review",
                kind="judge",
                title="审查结论",
                cond=lambda state: state.get("review") == "通过",
                output="passed",
                edges=(
                    Edge("report", when=lambda state: state.get("passed") is True, label="通过"),
                    Edge("fix", when=lambda state: state.get("passed") is not True, label="需修改"),
                ),
            ),
            Node(name="fix", agent="coder", task="按意见修改", output="fix", edges=(Edge("report"),)),
            Node(name="report", kind="end"),
        ),
    )


def test_a_matching_branch_runs_and_the_other_is_skipped():
    workflow = _branch_workflow()
    agents = ScriptedAgent({"coder": "已修好"})

    run = _engine(workflow, agents=agents).run({"review": "需修改"})

    assert run.status == "succeeded"
    assert [call["agent"] for call in agents.calls] == ["coder"]
    assert run.state["passed"] is False
    assert run.node("fix").status == "ok"
    assert run.node("report").status == "ok"


def test_the_untaken_branch_is_recorded_as_skipped():
    """没走的那条支路要留痕：看图时才看得出"这次为什么没修"。"""
    workflow = _branch_workflow()

    run = _engine(workflow).run({"review": "通过"})

    assert run.state["passed"] is True
    skipped = run.node("fix")
    assert skipped.status == "skipped"
    assert "需修改" in skipped.skipped_reason


# ---------------------------------------------------------------- 循环


def _loop_workflow() -> Workflow:
    """测试没过就回到生成，最多回两次。"""
    return Workflow(
        name="loop",
        start="generate",
        nodes=(
            Node(name="generate", agent="tester", task="生成测试", output="tests", edges=(Edge("run"),)),
            Node(
                name="run",
                kind="command",
                title="执行测试",
                code="pytest -q",
                output="result",
                # 测试没过是**预期内**的结果，交给后面的判断节点处理，不该让流程挂掉
                on_error="continue",
                edges=(Edge("check"),),
            ),
            Node(
                name="check",
                kind="judge",
                title="是否全绿",
                cond=lambda state: state.get("result_ok") is True,
                output="passed",
                edges=(
                    Edge(
                        "generate",
                        when=lambda state: state.get("passed") is not True,
                        max_times=2,
                        label="没过，回到生成",
                    ),
                    Edge("report", label="收尾"),
                ),
            ),
            Node(name="report", kind="end"),
        ),
    )


def test_a_loop_edge_runs_the_node_again_until_it_passes():
    workflow = _loop_workflow()
    agents = ScriptedAgent({"tester": "测试已更新"})
    commands = ScriptedCommand([(1, "1 failed"), (0, "3 passed")])

    run = _engine(workflow, agents=agents, commands=commands).run({})

    assert run.status == "succeeded"
    assert len(run.node_runs("generate")) == 2
    assert [item.attempt for item in run.node_runs("generate")] == [1, 1]
    assert run.state["passed"] is True


def test_a_loop_stops_when_the_retry_edge_hits_its_limit():
    """回边次数用完就往下走，不能无限转（循环没有出口是最常见的死循环）。"""
    workflow = _loop_workflow()
    commands = ScriptedCommand([(1, "failed"), (1, "failed"), (1, "failed")])

    run = _engine(workflow, commands=commands).run({})

    # 生成 → 执行 → 判定 走了一趟，又回了两趟，共三次执行
    assert len(run.node_runs("generate")) == 3
    assert len(run.node_runs("run")) == 3
    assert run.state["passed"] is False
    assert run.node("report").status == "ok"


def test_a_runaway_workflow_is_stopped_by_the_step_limit():
    workflow = Workflow(
        name="runaway",
        start="a",
        max_steps=5,
        nodes=(
            Node(
                name="a",
                agent="echo",
                task="转圈",
                edges=(Edge("a"),),
                max_visits=0,  # 不限次数，专门让整条流程的步数上限来拦
            ),
        ),
    )

    run = _engine(workflow).run({})

    assert run.status == "failed"
    assert "步数" in run.error
    assert len(run.nodes) == 5


def test_a_flow_that_never_reaches_its_end_node_is_a_failure():
    """画了终点却没走到，说明有分支没出口——这种"跑完了但没结果"不能算成功。"""
    workflow = Workflow(
        name="dead-end",
        start="check",
        nodes=(
            Node(
                name="check",
                kind="judge",
                cond=lambda state: False,
                edges=(Edge("report", when=lambda state: False, label="永远不成立"),),
            ),
            Node(name="report", kind="end"),
        ),
    )

    run = _engine(workflow).run({})

    assert run.status == "failed"
    assert "没有走到终点" in run.error


# ---------------------------------------------------------------- 并行


def test_parallel_branches_both_run_and_then_join():
    workflow = Workflow(
        name="parallel",
        start="split",
        nodes=(
            Node(
                name="split",
                kind="end",
                title="分叉",
                parallel=True,
                edges=(Edge("left"), Edge("right")),
            ),
            Node(name="left", agent="a", task="左", output="left", edges=(Edge("join"),)),
            Node(name="right", agent="b", task="右", output="right", edges=(Edge("join"),)),
            Node(name="join", agent="c", task="合并 ${left} + ${right}", output="merged"),
        ),
    )
    agents = ScriptedAgent({"a": "左边结果", "b": "右边结果", "c": "合并完成"})

    run = _engine(workflow, agents=agents).run({})

    # 两条分支是并行跑的，谁先谁后不确定；这里只关心"都跑了"
    assert sorted(call["node"] for call in agents.calls) == ["join", "left", "right"]
    assert run.node("left").status == "ok"
    assert run.node("right").status == "ok"
    # 两条分支都汇到 join，只应该跑一次
    assert len(run.node_runs("join")) == 1
    assert run.state["merged"] == "合并完成"


# ---------------------------------------------------------------- 失败处理


def test_a_failing_node_is_retried_and_can_succeed():
    workflow = Workflow(
        name="retry",
        start="flaky",
        nodes=(
            Node(
                name="flaky",
                agent="flaky",
                task="做点容易失败的事",
                retry=2,
                edges=(Edge("done"),),
            ),
            Node(name="done", kind="end"),
        ),
    )

    def flaky(_attempt: int) -> AgentOutput:
        return AgentOutput(answer="", success=False, error="模型超时")

    class Once:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, agent: str, task: str, options: dict) -> AgentOutput:
            self.calls += 1
            if self.calls < 3:
                return AgentOutput(answer="", success=False, error="模型超时")
            return AgentOutput(answer="终于好了")

    runner = Once()
    run = _engine(workflow, agents=runner).run({})

    assert runner.calls == 3
    assert run.node("flaky").status == "ok"
    assert run.node("flaky").attempt == 3
    assert run.node("flaky").answer == "终于好了"


def test_a_node_that_keeps_failing_stops_the_flow_by_default():
    workflow = Workflow(
        name="stop",
        start="bad",
        nodes=(
            Node(name="bad", agent="bad", task="必失败", retry=1, edges=(Edge("after"),)),
            Node(name="after", agent="echo", task="不该跑到这里"),
        ),
    )
    agents = ScriptedAgent({"bad": AgentOutput(answer="", success=False, error="炸了")})

    run = _engine(workflow, agents=agents).run({})

    assert run.status == "failed"
    assert run.node("bad").status == "failed"
    assert run.node("bad").attempt == 2
    assert "炸了" in run.error
    assert run.node("after") is None
    assert [call["node"] for call in agents.calls] == ["bad", "bad"]


def test_on_error_continue_lets_the_flow_finish_and_reports_partial():
    """失败不静默：允许继续，但结果要标成"部分完成"并留下原因。"""
    workflow = Workflow(
        name="continue",
        start="bad",
        nodes=(
            Node(
                name="bad",
                agent="bad",
                task="必失败",
                on_error="continue",
                edges=(Edge("after"),),
            ),
            Node(name="after", agent="echo", task="照样往下走", output="after"),
        ),
    )
    agents = ScriptedAgent(
        {"bad": AgentOutput(answer="", success=False, error="工具不可用"), "echo": "我跑完了"}
    )

    run = _engine(workflow, agents=agents).run({})

    assert run.status == "partial"
    assert run.node("bad").status == "failed"
    assert run.node("after").status == "ok"
    assert run.state["after"] == "我跑完了"
    assert "工具不可用" in run.error


def test_a_command_node_records_exit_code_and_output():
    workflow = Workflow(
        name="command",
        start="run",
        nodes=(
            Node(
                name="run",
                kind="command",
                code="pytest -q ${target}",
                output="tests",
                edges=(Edge("done"),),
            ),
            Node(name="done", kind="end"),
        ),
    )
    commands = ScriptedCommand([(0, "5 passed")])

    run = _engine(workflow, commands=commands).run({"target": "test_calc.py"})

    assert commands.calls == ["pytest -q test_calc.py"]
    assert run.node("run").status == "ok"
    assert run.state["tests"] == "5 passed"
    assert run.state["tests_ok"] is True
    assert run.state["tests_exit"] == 0


def test_a_failing_command_marks_the_node_failed():
    workflow = Workflow(
        name="command",
        start="run",
        nodes=(Node(name="run", kind="command", code="pytest -q", output="tests"),),
    )
    commands = ScriptedCommand([(1, "1 failed")])

    run = _engine(workflow, commands=commands).run({})

    assert run.status == "failed"
    assert run.state["tests_ok"] is False
    assert run.state["tests_exit"] == 1
    assert "退出码 1" in run.node("run").error


# ---------------------------------------------------------------- 事件与校验


def test_events_report_node_lifecycle_for_the_ui():
    workflow = Workflow(
        name="events",
        start="one",
        nodes=(Node(name="one", agent="echo", task="你好", output="answer"),),
    )
    events: list[dict] = []

    _engine(workflow, events=events).run({})

    node_events = [item for item in events if item["type"] == "node"]
    assert [item["data"]["status"] for item in node_events] == ["running", "ok"]
    assert node_events[0]["data"]["name"] == "one"
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["status"] == "succeeded"


def test_a_workflow_with_a_dangling_edge_is_rejected():
    with pytest.raises(AgentCodeError, match="不存在的节点"):
        Workflow(
            name="bad",
            start="a",
            nodes=(Node(name="a", agent="echo", task="x", edges=(Edge("没有这个节点"),)),),
        )


def test_duplicate_node_names_are_rejected():
    with pytest.raises(AgentCodeError, match="重名"):
        Workflow(
            name="bad",
            nodes=(
                Node(name="a", agent="echo", task="x"),
                Node(name="a", agent="echo", task="y"),
            ),
        )


def test_an_unknown_start_node_is_rejected():
    with pytest.raises(AgentCodeError, match="起始节点"):
        Workflow(name="bad", start="没有这个", nodes=(Node(name="a", agent="echo"),))
