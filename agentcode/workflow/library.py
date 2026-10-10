"""内置流程：作业里要求的三种形态，都在这一个文件里定义。

| 流程 | 形态 | 用到的 Agent |
| --- | --- | --- |
| ``mini_se`` | 需求 → 设计 → 编码 → 汇总（顺序流水线） | plan_and_solve、react、coding、echo |
| ``test_pipeline`` | 生成测试 → 执行 → 判定 → 没过就回到生成（条件 + 循环） | test_gen、echo |
| ``code_review`` | 编码 → 审查 → 判定 → 需修改就修复并复审（条件 + 循环） | coding、react、echo |

节点里的 Agent 全部来自项目1 的注册表（``default_registry``），流程本身只负责
"谁先谁后、结果怎么传下去、不过关怎么办"。

``test_pipeline`` 的第一步会准备一个演示用的被测模块。之所以把模块源码直接写在
这里，是因为代码执行后端也可能是 docker（那时容器里看不到本仓库的 ``examples/``），
写成内联的才能两种后端都跑得起来。
"""

from __future__ import annotations

from typing import Any

from agentcode.core.errors import AgentCodeError
from agentcode.workflow.model import Edge, Node, Workflow

#: 演示用的被测模块：故意留了除零、空列表、负数开方这几个边界。
#: 名字与函数要和项目1 离线脚本模型（``demo_responses_llm("test_gen")``）对得上，
#: 否则 ``--llm mock`` 的演示会跑不通。
_DEMO_MODULE = '''import math


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def divide(a, b):
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b


def average(numbers):
    if not numbers:
        raise ValueError("列表不能为空")
    return sum(numbers) / len(numbers)


def safe_sqrt(value):
    if value < 0:
        return None
    return math.sqrt(value)
'''

#: 准备靶子：把演示模块写进代码工作目录（已经有了就不动它）
_PREPARE_CODE = (
    "from pathlib import Path\n"
    "\n"
    "MODULE = '''" + _DEMO_MODULE + "'''\n"
    "\n"
    "target = Path('calculator.py')\n"
    "if target.exists():\n"
    "    print('calculator.py 已存在，直接复用')\n"
    "else:\n"
    "    target.write_text(MODULE, encoding='utf-8')\n"
    "    print('已准备被测文件 calculator.py')\n"
)

#: 执行测试：复用工作目录里的 pytest
_PYTEST_CODE = (
    "import sys\n"
    "\n"
    "import pytest\n"
    "\n"
    "sys.exit(pytest.main(['-q']))\n"
)


def _mini_se() -> Workflow:
    """需求 → 设计 → 编码 → 汇总。"""
    return Workflow(
        name="mini_se",
        title="Mini SE 流水线",
        description="需求分析 → 方案设计 → 编码实现 → 汇总，三个职责不同的 Agent 接力完成一次小需求。",
        tags=("顺序", "3 个 Agent"),
        nodes=(
            Node(
                name="requirements",
                title="① 需求分析",
                agent="plan_and_solve",
                task=(
                    "你是需求分析智能体。把下面的需求整理成 3-5 条可验收的需求要点，"
                    "每条都要能判断「做到没做到」。\n需求：${request}"
                ),
                output="requirements",
                edges=(Edge("design"),),
            ),
            Node(
                name="design",
                title="② 方案设计",
                agent="react",
                task=(
                    "你是设计智能体。根据下面的需求要点写一份简要设计：模块划分、关键接口、"
                    "数据结构、以及主要风险。\n需求要点：${requirements}"
                ),
                output="design",
                edges=(Edge("implement"),),
            ),
            Node(
                name="implement",
                title="③ 编码实现",
                agent="coding",
                task=(
                    "你是编码智能体。按下面的设计在工作目录里实现代码，写完自己跑一遍验证。\n"
                    "设计：${design}\n需求要点：${requirements}"
                ),
                output="code",
                # 写代码可能要几轮工具调用，失败一次就重试一次
                retry=1,
                edges=(Edge("summary"),),
            ),
            Node(
                name="summary",
                title="④ 汇总",
                agent="echo",
                task="用三句话总结这次流水线的产出（需求、设计、实现各一句）：\n${requirements}\n${design}\n${code}",
                output="summary",
                edges=(Edge("done"),),
            ),
            Node(name="done", kind="end", title="完成"),
        ),
    )


def _test_pipeline() -> Workflow:
    """生成测试 → 执行 → 没过就回到生成（条件 + 循环）。"""
    return Workflow(
        name="test_pipeline",
        title="测试流水线",
        description="准备被测模块 → 生成 pytest 测试 → 执行 → 没过就回到生成（最多两轮）→ 收尾。",
        tags=("条件", "循环", "2 个 Agent + 命令节点"),
        nodes=(
            Node(
                name="prepare",
                title="准备靶子",
                kind="command",
                code=_PREPARE_CODE,
                output="prepare",
                edges=(Edge("generate"),),
            ),
            Node(
                name="generate",
                title="生成测试",
                agent="test_gen",
                task=(
                    "为工作目录里的 calculator.py 生成 pytest 测试，写进 test_calculator.py，"
                    "覆盖正常路径与边界情况，并跑一遍确认。\n"
                    "上一次的测试结果：${result}\n"
                    "如果上一次没通过，请针对失败点补测试或修正测试本身。"
                ),
                output="tests",
                retry=1,
                edges=(Edge("run_tests"),),
            ),
            Node(
                name="run_tests",
                title="执行测试",
                kind="command",
                code=_PYTEST_CODE,
                output="result",
                # 测试没过是预期内的结果：交给判定节点决定"回到生成"还是"收尾"
                on_error="continue",
                edges=(Edge("check"),),
            ),
            Node(
                name="check",
                title="是否全绿",
                kind="judge",
                cond=lambda state: state.get("result_ok") is True,
                output="passed",
                edges=(
                    Edge(
                        "generate",
                        when=lambda state: state.get("passed") is not True,
                        max_times=1,
                        label="没过，回到生成",
                    ),
                    Edge("report", label="收尾"),
                ),
            ),
            Node(
                name="report",
                title="收尾报告",
                agent="echo",
                task="用一两句话说明这次测试流水线的结果：\n${result}",
                output="report",
                edges=(Edge("done"),),
            ),
            Node(name="done", kind="end", title="完成"),
        ),
    )


def _code_review() -> Workflow:
    """编码 → 审查 → 需修改就修复并复审（条件 + 循环）。"""
    return Workflow(
        name="code_review",
        title="代码审查流程",
        description="编码 → 审查 → 判定：需修改就修复并再审一轮（最多两轮），通过就收尾。",
        tags=("条件", "循环", "2 个 Agent + 判定节点"),
        nodes=(
            Node(
                name="implement",
                title="① 编码",
                agent="coding",
                task=(
                    "你是编码智能体。按下面的要求在工作目录里实现代码，写完自己跑一遍。\n"
                    "要求：${request}"
                ),
                output="code",
                retry=1,
                edges=(Edge("review"),),
            ),
            Node(
                name="review",
                title="② 代码审查",
                agent="react",
                task=(
                    "你是代码审查智能体。审查工作目录里的代码：正确性、边界情况、可读性。"
                    "最后一行必须单独写「结论：通过」或「结论：需修改」。\n"
                    "原始要求：${request}"
                ),
                output="review",
                edges=(Edge("verdict"),),
            ),
            Node(
                name="verdict",
                title="③ 审查结论",
                kind="judge",
                cond=lambda state: "需修改" in str(state.get("review") or ""),
                output="needs_fix",
                edges=(
                    Edge(
                        "fix",
                        when=lambda state: state.get("needs_fix") is True,
                        max_times=1,
                        label="需修改",
                    ),
                    Edge("report", label="通过，收尾"),
                ),
            ),
            Node(
                name="fix",
                title="④ 修复",
                agent="coding",
                task="按审查意见修改代码，改完自己再跑一遍：\n${review}",
                output="fix",
                retry=1,
                edges=(Edge("review"),),  # 回边：修完再审一轮
            ),
            Node(
                name="report",
                title="⑤ 收尾报告",
                agent="echo",
                task="用一两句话总结这次代码审查的结果：\n审查意见：${review}",
                output="report",
                edges=(Edge("done"),),
            ),
            Node(name="done", kind="end", title="完成"),
        ),
    )


#: 内置流程登记表（名字 → 流程）
WORKFLOWS: dict[str, Workflow] = {}


def register_workflow(workflow: Workflow, *, overwrite: bool = False) -> Workflow:
    """登记一条流程；重名要显式要求覆盖，避免悄悄顶掉别人的流程。"""
    if workflow.name in WORKFLOWS and not overwrite:
        raise AgentCodeError(f"流程重名：{workflow.name}。")
    WORKFLOWS[workflow.name] = workflow
    return workflow


def get_workflow(name: str) -> Workflow:
    """按名字取流程。"""
    workflow = WORKFLOWS.get(str(name or "").strip())
    if workflow is None:
        raise AgentCodeError(
            f"没有这个流程：{name}。现有流程：{'、'.join(sorted(WORKFLOWS)) or '（无）'}。"
        )
    return workflow


def all_workflows() -> list[Workflow]:
    """全部流程（按名字排序，输出稳定）。"""
    return [WORKFLOWS[name] for name in sorted(WORKFLOWS)]


def list_workflows() -> list[dict[str, Any]]:
    """清单摘要，给 CLI 与网页用。"""
    return [
        {
            "name": workflow.name,
            "title": workflow.title,
            "description": workflow.description,
            "agents": workflow.agent_names(),
            "nodes": len(workflow.nodes),
            "tags": list(workflow.tags),
        }
        for workflow in all_workflows()
    ]


for _workflow in (_mini_se(), _test_pipeline(), _code_review()):
    register_workflow(_workflow)
