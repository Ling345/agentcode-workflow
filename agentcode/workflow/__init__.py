"""Workflow：让多个智能体按一张"流程图"协作。

这个包只做编排，不重新实现智能体——节点直接复用项目1 里注册在
:data:`agentcode.core.registry.default_registry` 的智能体，以及项目1 的代码执行后端。

四个概念：

- :class:`WorkflowState`：**共享状态**，Agent 之间的消息就放在这里，下一个节点的
  任务模板用 ``${变量}`` 取用；
- :class:`Node` / :class:`Edge`：**节点与连线**，连线上的条件决定走哪条分支，
  回边就是循环，``parallel=True`` 的节点后面是并行分支；
- :class:`WorkflowEngine`：**执行器**，按波次（wave）推进流程，负责重试、错误策略、
  状态写入与事件回调；
- :class:`FlowRun`：**运行记录**，每个节点跑了多久、什么状态、留下什么，都记在里面。
"""

from agentcode.workflow.engine import FlowRun, NodeRun, WorkflowEngine
from agentcode.workflow.model import Edge, Node, Workflow
from agentcode.workflow.runners import AgentOutput, CommandOutput
from agentcode.workflow.state import WorkflowState

__all__ = [
    "AgentOutput",
    "CommandOutput",
    "Edge",
    "FlowRun",
    "Node",
    "NodeRun",
    "Workflow",
    "WorkflowEngine",
    "WorkflowState",
]
