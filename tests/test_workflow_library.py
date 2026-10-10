"""内置流程：作业要交的三个 Workflow（离线）。"""

from __future__ import annotations

import pytest

from agentcode.core.errors import AgentCodeError
from agentcode.workflow import WorkflowEngine
from agentcode.workflow.library import all_workflows, get_workflow, list_workflows
from tests.workflow_stubs import ScriptedAgent, ScriptedCommand


def test_the_three_required_flows_are_registered():
    names = {item["name"] for item in list_workflows()}

    assert {"mini_se", "test_pipeline", "code_review"} <= names


def test_every_builtin_flow_uses_at_least_two_agents():
    """作业硬要求：至少 2 个 Agent 协作。"""
    for workflow in all_workflows():
        assert len(workflow.agent_names()) >= 2, workflow.name


def test_every_builtin_flow_can_be_described_for_the_ui():
    for workflow in all_workflows():
        payload = workflow.to_dict()
        assert payload["nodes"]
        assert payload["agents"]
        assert all(node["edges"] is not None for node in payload["nodes"])


def test_an_unknown_flow_is_rejected_with_a_readable_message():
    with pytest.raises(AgentCodeError, match="没有这个流程"):
        get_workflow("查无此流程")


# ---------------------------------------------------------------- mini_se


def test_mini_se_is_a_requirements_design_code_pipeline():
    workflow = get_workflow("mini_se")

    assert [node.name for node in workflow.nodes][:3] == ["requirements", "design", "implement"]
    # 三个不同职责的智能体依次接力
    assert workflow.agent_names()[:3] == ["plan_and_solve", "react", "coding"]


def test_mini_se_passes_the_previous_answer_into_the_next_task():
    workflow = get_workflow("mini_se")
    agents = ScriptedAgent(
        {
            "plan_and_solve": "1. 支持加减 2. 支持除零保护",
            "react": "设计：一个 calculator 模块 + 单元测试",
            "coding": "已实现 calculator.py",
            "echo": "总结：需求→设计→实现三段都完成。",
        }
    )

    run = WorkflowEngine(workflow, agent_runner=agents).run({"request": "写一个计算器"})

    assert run.status == "succeeded"
    # 设计节点的任务里带着需求节点的产出，编码节点的任务里带着设计节点的产出
    assert "1. 支持加减 2. 支持除零保护" in agents.calls[1]["task"]
    assert "设计：一个 calculator 模块 + 单元测试" in agents.calls[2]["task"]
    assert run.state["code"] == "已实现 calculator.py"


# ---------------------------------------------------------------- 测试流程


def test_test_pipeline_has_agents_a_command_node_a_judge_and_a_loop_back_edge():
    workflow = get_workflow("test_pipeline")

    kinds = {node.kind for node in workflow.nodes}
    assert {"agent", "command", "judge", "end"} <= kinds
    assert "test_gen" in workflow.agent_names()
    back_edges = [
        edge
        for node in workflow.nodes
        for edge in node.edges
        if edge.max_times and edge.target == "generate"
    ]
    assert back_edges, "测试没过要能回到生成节点（循环）"


def test_test_pipeline_loops_back_once_when_tests_fail():
    """端到端：准备靶子 → 生成测试 → 执行 → 没过 → 回到生成 → 再过 → 收尾。"""
    workflow = get_workflow("test_pipeline")
    agents = ScriptedAgent({"test_gen": "已生成测试", "echo": "流程收尾"})
    commands = ScriptedCommand(
        [
            (0, "calculator.py 已就绪"),
            (1, "1 failed"),  # 第一轮没过
            (0, "8 passed"),  # 回到生成之后再跑，全绿
        ]
    )

    run = WorkflowEngine(
        workflow, agent_runner=agents, command_runner=commands
    ).run({"target": "calculator.py"})

    assert run.status == "succeeded"
    assert len(run.node_runs("generate")) == 2  # 生成跑了两次
    assert run.state["passed"] is True
    assert run.state["result_ok"] is True
    assert "pytest" in commands.calls[-1]


def test_test_pipeline_finishes_even_when_the_tests_never_pass():
    """回边次数用完之后要收尾，并把"没绿"这件事如实记下来。"""
    workflow = get_workflow("test_pipeline")
    commands = ScriptedCommand([(0, "就绪"), (1, "failed"), (1, "failed")])

    run = WorkflowEngine(workflow, command_runner=commands).run({})

    assert len(run.node_runs("generate")) == 2
    assert run.state["passed"] is False
    # 收尾节点照样跑到，流程不算成功（partial），原因是"还有节点是失败的"
    assert run.node("report") is not None
    assert run.status in ("partial", "succeeded")


# ---------------------------------------------------------------- 代码审查流程


def test_code_review_loops_back_to_fix_when_the_verdict_says_so():
    workflow = get_workflow("code_review")
    agents = ScriptedAgent(
        {
            "coding": "已实现",
            "react": lambda count: "结论：需修改" if count == 1 else "结论：通过",
            "echo": "收尾",
        }
    )

    run = WorkflowEngine(workflow, agent_runner=agents).run({"request": "写一个加法函数"})

    assert run.status == "succeeded"
    assert len(run.node_runs("review")) == 2  # 审了两轮
    assert len(run.node_runs("fix")) == 1  # 中间修了一次
    assert run.state["needs_fix"] is False


def test_code_review_skips_the_fix_branch_when_the_review_passes():
    workflow = get_workflow("code_review")
    agents = ScriptedAgent({"react": "结论：通过", "coding": "已实现", "echo": "收尾"})

    run = WorkflowEngine(workflow, agent_runner=agents).run({})

    assert run.status == "succeeded"
    assert run.node("fix").status == "skipped"
    assert "需修改" in run.node("fix").skipped_reason
