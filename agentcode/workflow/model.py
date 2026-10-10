"""流程定义的数据结构：节点、连线和整张图。

用 Python 对象而不是 YAML/JSON 来定义流程，是因为连线上要挂**条件函数**
（``when=lambda state: ...``）。条件用代码写出来最直白，也最好测；
需要可视化或导出时用 :meth:`Workflow.to_dict` 拿展示用的结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agentcode.core.errors import AgentCodeError
from agentcode.workflow.state import WorkflowState

#: 节点类型
KIND_AGENT = "agent"  # 调一个注册在册的智能体
KIND_COMMAND = "command"  # 在工作目录里跑一段 Python（例如 pytest）
KIND_JUDGE = "judge"  # 只做判断，把布尔值写进状态
KIND_END = "end"  # 终点标记（不干活，只让图完整）
KINDS = frozenset({KIND_AGENT, KIND_COMMAND, KIND_JUDGE, KIND_END})

#: 节点失败后怎么办
ON_ERROR_STOP = "stop"  # 整条流程失败退出（默认）
ON_ERROR_CONTINUE = "continue"  # 记下错误继续往下走，最终状态为 partial
ON_ERRORS = frozenset({ON_ERROR_STOP, ON_ERROR_CONTINUE})

#: 默认最多执行多少个节点（防"闭环没有出口"）
DEFAULT_MAX_STEPS = 40
#: 默认同一个节点最多被跑到几次（循环的保护）
DEFAULT_MAX_VISITS = 3

Condition = Callable[[WorkflowState], Any]


@dataclass
class Edge:
    """一条连线。``when`` 为空表示"总是走"。"""

    target: str
    when: Condition | None = None
    label: str = ""
    #: 这条边最多被走几次；0 = 不限。循环的回边就靠它兜底
    max_times: int = 0

    def matches(self, state: WorkflowState) -> bool:
        """条件是否成立。"""
        if self.when is None:
            return True
        return bool(self.when(state))


@dataclass
class Node:
    """一个节点。

    ``kind`` 决定它怎么执行：

    - ``agent``：把 ``task``（支持 ``${变量}``）交给 ``agent`` 指定的智能体；
    - ``command``：在工作目录里执行 ``code``（一段 Python，退出码 0 算成功）；
    - ``judge``：用 ``cond`` 判断，把布尔值写进状态；
    - ``end``：什么都不做，只是给流程一个明确的终点。

    结果写到状态的哪个键由 ``output`` 决定（默认是节点名）。命令节点还会额外写
    ``<output>_ok``（是否成功）与 ``<output>_exit``（退出码），方便后面的判断节点用。
    """

    name: str
    kind: str = KIND_AGENT
    title: str = ""
    agent: str = ""
    task: str = ""
    code: str = ""
    output: str = ""
    cond: Condition | None = None
    retry: int = 0
    retry_delay: float = 0.0
    on_error: str = ON_ERROR_STOP
    #: 本节点之后是并行分支（它的多条出边会**同时**走）
    parallel: bool = False
    #: 同一个节点在一次运行里最多执行几次（循环的保护）；0 = 不限
    max_visits: int = DEFAULT_MAX_VISITS
    timeout: float = 60.0
    edges: tuple[Edge, ...] = ()

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        if not self.name:
            raise AgentCodeError("节点名字不能为空。")
        if self.kind not in KINDS:
            raise AgentCodeError(f"节点 {self.name} 的类型不认识：{self.kind}。")
        if self.on_error not in ON_ERRORS:
            raise AgentCodeError(f"节点 {self.name} 的 on_error 只能是 stop 或 continue。")
        if self.retry < 0:
            raise AgentCodeError(f"节点 {self.name} 的 retry 不能是负数。")
        if self.kind == KIND_AGENT and not self.agent:
            raise AgentCodeError(f"节点 {self.name} 是 agent 节点，但没有写 agent 名字。")
        if self.kind == KIND_COMMAND and not str(self.code or "").strip():
            raise AgentCodeError(f"节点 {self.name} 是 command 节点，但没有写要执行的代码。")
        if self.kind == KIND_JUDGE and self.cond is None:
            raise AgentCodeError(f"节点 {self.name} 是 judge 节点，但没有写判断条件。")
        self.edges = tuple(self.edges or ())
        if not self.title:
            self.title = self.name
        if not self.output and self.kind != KIND_END:
            self.output = self.name

    def to_dict(self) -> dict[str, Any]:
        """展示用的结构（给 CLI / 网页画流程图）。"""
        return {
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "agent": self.agent,
            "output": self.output,
            "retry": self.retry,
            "on_error": self.on_error,
            "parallel": self.parallel,
            "edges": [
                {"target": edge.target, "label": edge.label or edge.target, "max_times": edge.max_times}
                for edge in self.edges
            ],
        }


@dataclass
class Workflow:
    """一张流程图：一个起点、若干节点、节点之间用连线串起来。"""

    name: str
    nodes: tuple[Node, ...] = ()
    start: str = ""
    title: str = ""
    description: str = ""
    max_steps: int = DEFAULT_MAX_STEPS
    max_parallel: int = 4
    tags: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        if not self.name:
            raise AgentCodeError("流程名字不能为空。")
        self.nodes = tuple(self.nodes or ())
        if not self.nodes:
            raise AgentCodeError(f"流程 {self.name} 里一个节点都没有。")
        seen = set()
        for node in self.nodes:
            if node.name in seen:
                raise AgentCodeError(f"节点重名：{node.name}。")
            seen.add(node.name)
        if not self.start:
            self.start = self.nodes[0].name
        if self.start not in seen:
            raise AgentCodeError(f"起始节点不存在：{self.start}。")
        for node in self.nodes:
            for edge in node.edges:
                if edge.target not in seen:
                    raise AgentCodeError(
                        f"节点 {node.name} 的连线指向不存在的节点：{edge.target}。"
                    )
        if self.max_steps <= 0:
            raise AgentCodeError(f"流程 {self.name} 的 max_steps 必须大于 0。")
        if self.max_parallel <= 0:
            raise AgentCodeError(f"流程 {self.name} 的 max_parallel 必须大于 0。")
        if not self.title:
            self.title = self.name

    # ------------------------------------------------------------------ 查询

    def node(self, name: str) -> Node | None:
        """按名字取节点。"""
        for item in self.nodes:
            if item.name == name:
                return item
        return None

    def agent_names(self) -> list[str]:
        """这条流程用到的智能体（去重，按出现顺序）。"""
        names: list[str] = []
        for node in self.nodes:
            if node.kind == KIND_AGENT and node.agent not in names:
                names.append(node.agent)
        return names

    def to_dict(self) -> dict[str, Any]:
        """展示用的结构。"""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "start": self.start,
            "max_steps": self.max_steps,
            "agents": self.agent_names(),
            "tags": list(self.tags),
            "nodes": [node.to_dict() for node in self.nodes],
        }
