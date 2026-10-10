"""流程引擎：按"波次"推进流程图。

一次波次（wave）是一组可以同时执行的节点。节点跑完看它的出边：

- 普通节点：**第一条条件成立的边**被选中（这就是条件分支）；
- ``parallel=True`` 的节点：所有条件成立的边一起走，这些目标构成下一波（并行执行）；
- 条件不成立的边，以及没被选中的分支，会在运行记录里标成 ``skipped``——看图时
  才知道"这次为什么没走那条路"；
- 出边指回已经跑过的节点就是循环，靠 ``Edge.max_times`` 与 ``Node.max_visits``
  两道上限兜底，再加 ``Workflow.max_steps`` 防整张图转不停。

失败处理：节点失败先按 ``retry`` 重试；仍然失败时 ``on_error="stop"``（默认）
让整条流程失败退出，``"continue"`` 则记下错误继续往下跑，最终状态标成 ``partial``
——失败不静默。
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from agentcode.config import Settings
from agentcode.workflow.model import (
    KIND_AGENT,
    KIND_COMMAND,
    KIND_END,
    KIND_JUDGE,
    ON_ERROR_CONTINUE,
    Edge,
    Node,
    Workflow,
)
from agentcode.workflow.runners import (
    AgentOutput,
    AgentRunner,
    CommandOutput,
    CommandRunner,
    default_agent_runner,
    default_command_runner,
)
from agentcode.workflow.state import WorkflowState

#: 节点状态
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

#: 整条流程的状态
FLOW_SUCCEEDED = "succeeded"
FLOW_PARTIAL = "partial"
FLOW_FAILED = "failed"

Event = dict[str, Any]
OnEvent = Callable[[Event], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class NodeRun:
    """一个节点的一次执行记录（循环里的第二轮就是另一条记录）。"""

    name: str
    title: str = ""
    kind: str = KIND_AGENT
    status: str = STATUS_PENDING
    attempt: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    output_key: str = ""
    output: Any = None
    answer: str = ""
    error: str = ""
    skipped_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        data: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1),
            "output_key": self.output_key,
            "answer": self.answer,
            "error": self.error,
            "usage": dict(self.usage),
            "steps": self.steps,
        }
        if self.skipped_reason:
            data["skipped_reason"] = self.skipped_reason
        return data


@dataclass
class FlowRun:
    """一次流程运行的完整记录。"""

    run_id: str
    workflow: str
    title: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_RUNNING
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    nodes: list[NodeRun] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def node(self, name: str) -> NodeRun | None:
        """取某个节点**最后**一次的执行记录（循环里看最后一轮）。"""
        for item in reversed(self.nodes):
            if item.name == name:
                return item
        return None

    def node_runs(self, name: str) -> list[NodeRun]:
        """取某个节点全部执行记录（循环转了几圈看这里）。"""
        return [item for item in self.nodes if item.name == name]

    def executed_steps(self) -> int:
        """真正执行过的节点次数（不含被跳过的）。"""
        return sum(1 for item in self.nodes if item.status != STATUS_SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "title": self.title,
            "status": self.status,
            "inputs": dict(self.inputs),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1),
            "steps": self.executed_steps(),
            "error": self.error,
            "state": dict(self.state),
            "nodes": [item.to_dict() for item in self.nodes],
        }


class WorkflowEngine:
    """执行一张流程图。"""

    def __init__(
        self,
        workflow: Workflow,
        *,
        agent_runner: AgentRunner | None = None,
        command_runner: CommandRunner | None = None,
        settings: Settings | None = None,
        llm_mode: str = "mock",
        on_event: OnEvent | None = None,
        run_id: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.workflow = workflow
        self.settings = settings
        self.llm_mode = llm_mode
        self._on_event = on_event
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._sleep = sleep
        self._agent_runner = agent_runner or default_agent_runner(
            settings=settings, llm_mode=llm_mode
        )
        self._command_runner = command_runner or default_command_runner(settings=settings)

    # ------------------------------------------------------------------ 主入口

    def run(self, inputs: dict[str, Any] | None = None) -> FlowRun:
        """跑完一整条流程，返回运行记录（同步；界面想要实时进度就传 ``on_event``）。"""
        state = WorkflowState(inputs or {})
        record = FlowRun(
            run_id=self.run_id,
            workflow=self.workflow.name,
            title=self.workflow.title,
            inputs=dict(inputs or {}),
            started_at=_now(),
        )
        self._emit(
            {
                "type": "start",
                "data": {
                    "run_id": record.run_id,
                    "workflow": record.workflow,
                    "title": record.title,
                    "inputs": record.inputs,
                },
            }
        )

        started = time.perf_counter()
        visits: dict[str, int] = {}
        edge_uses: dict[str, int] = {}
        wave: list[str] = [self.workflow.start]
        abort_error = ""
        steps = 0

        while wave:
            if steps + len(wave) > self.workflow.max_steps:
                abort_error = (
                    f"超过最大步数（{self.workflow.max_steps}）：流程里可能有闭环没有出口。"
                )
                break
            runnable: list[Node] = []
            for name in wave:
                node = self.workflow.node(name)
                if node is None:  # 校验时已经拦过，这里只是防御
                    continue
                # max_visits=0 表示"不限次数"，只靠整条流程的 max_steps 兜底
                if node.max_visits and visits.get(name, 0) >= node.max_visits:
                    self._record_skipped(
                        record,
                        node,
                        f"同一个节点最多跑 {node.max_visits} 次，这次跳过",
                    )
                    continue
                visits[name] = visits.get(name, 0) + 1
                runnable.append(node)

            if not runnable:
                break

            results = self._run_wave(runnable, state, record)
            steps += len(runnable)

            next_wave: list[str] = []
            for node, node_run in results:
                if node_run.status == STATUS_FAILED:
                    message = f"节点 {node.name} 失败：{node_run.error}"
                    if node.on_error != ON_ERROR_CONTINUE:
                        abort_error = message
                        break
                self._plan_next(node, state, edge_uses, visits, next_wave, record)
            if abort_error:
                break

            deduped: list[str] = []
            for name in next_wave:
                if name not in deduped:
                    deduped.append(name)
            wave = deduped

        record.state = state.snapshot()
        record.finished_at = _now()
        record.duration_ms = (time.perf_counter() - started) * 1000
        # 最终状态看"每个节点最后一次执行的结果"：循环里失败过、后来补回来了，
        # 就不该再标成"部分完成"——那是对已经修好的事情的误报
        final_status: dict[str, str] = {}
        for item in record.nodes:
            if item.status != STATUS_SKIPPED:
                final_status[item.name] = item.status
        still_failed = [name for name, value in final_status.items() if value == STATUS_FAILED]
        reached_end = any(
            item.kind == KIND_END and item.status == STATUS_OK for item in record.nodes
        )
        declares_end = any(node.kind == KIND_END for node in self.workflow.nodes)
        if abort_error:
            record.status = FLOW_FAILED
            record.error = abort_error
        elif still_failed:
            broken = next(
                item
                for item in record.nodes
                if item.status == STATUS_FAILED and item.name in still_failed
            )
            record.status = FLOW_PARTIAL
            record.error = f"节点 {broken.name} 失败：{broken.error}"
        elif declares_end and not reached_end:
            # 流程图里明明画了终点却没走到：多半是某条分支没有出口，
            # 这种"跑完了但没结果"不能算成功
            record.status = FLOW_FAILED
            record.error = "流程没有走到终点节点：可能有一条分支没有出口。"
        else:
            record.status = FLOW_SUCCEEDED
        self._emit(
            {
                "type": "done",
                "data": {
                    "run_id": record.run_id,
                    "workflow": record.workflow,
                    "status": record.status,
                    "error": record.error,
                    "duration_ms": round(record.duration_ms, 1),
                },
            }
        )
        return record

    # ------------------------------------------------------------------ 波次

    def _run_wave(
        self, nodes: list[Node], state: WorkflowState, record: FlowRun
    ) -> list[tuple[Node, NodeRun]]:
        """跑一波节点；多个节点时按 ``max_parallel`` 并发。"""
        if len(nodes) == 1:
            node_run = self._execute(nodes[0], state)
            record.nodes.append(node_run)
            return [(nodes[0], node_run)]

        with ThreadPoolExecutor(
            max_workers=min(self.workflow.max_parallel, len(nodes)),
            thread_name_prefix="agentcode-wf",
        ) as pool:
            node_runs = list(pool.map(lambda node: self._execute(node, state), nodes))
        # 按声明顺序记录，事件按完成顺序发出，日志更稳
        paired = list(zip(nodes, node_runs))
        record.nodes.extend(node_runs)
        return paired

    def _plan_next(
        self,
        node: Node,
        state: WorkflowState,
        edge_uses: dict[str, int],
        visits: dict[str, int],
        next_wave: list[str],
        record: FlowRun,
    ) -> None:
        """根据出边决定下一波走哪些节点。"""
        matched: list[Edge] = []
        rejected: list[Edge] = []
        for edge in node.edges:
            key = f"{node.name}->{edge.target}"
            used = edge_uses.get(key, 0)
            if edge.max_times and used >= edge.max_times:
                continue  # 回边次数用完：不是"条件不成立"，只是循环结束
            if edge.matches(state):
                matched.append(edge)
            else:
                rejected.append(edge)
        if not node.parallel and len(matched) > 1:
            # 普通节点"第一条成立的边"说了算，其余同时成立的边只是让位，不算走错路
            matched = matched[:1]
        for edge in matched:
            key = f"{node.name}->{edge.target}"
            edge_uses[key] = edge_uses.get(key, 0) + 1
            next_wave.append(edge.target)
        taken = set(next_wave)
        for edge in rejected:
            if edge.target in taken:
                continue
            # 只给"从没跑过的支路"记一笔跳过：回边的目标早跑过了，
            # "这次没回到它"只是循环结束，再记一条只会把日志弄乱
            if visits.get(edge.target, 0) > 0:
                continue
            target = self.workflow.node(edge.target)
            if target is None:
                continue
            reason = edge.label or "条件不满足"
            self._record_skipped(record, target, reason)

    # ------------------------------------------------------------------ 单节点

    def _execute(self, node: Node, state: WorkflowState) -> NodeRun:
        """执行一个节点，含重试与错误策略。"""
        run = NodeRun(name=node.name, title=node.title, kind=node.kind, output_key=node.output)
        run.status = STATUS_RUNNING
        run.started_at = _now()
        self._emit(
            {
                "type": "node",
                "data": {
                    "name": node.name,
                    "title": node.title,
                    "kind": node.kind,
                    "status": STATUS_RUNNING,
                },
            }
        )
        started = time.perf_counter()
        attempt = 0
        while True:
            attempt += 1
            error = ""
            try:
                error = self._run_once(node, state, run)
            except Exception as exc:  # noqa: BLE001 - 节点失败要落到记录里，而不是把流程带崩
                error = f"执行出错：{exc}"
            if not error:
                run.error = ""  # 重试成功之后要把上一次的错误清掉
                break
            run.error = error
            if attempt > node.retry:
                break
            if node.retry_delay > 0:
                self._sleep(node.retry_delay * attempt)

        run.attempt = attempt
        run.finished_at = _now()
        run.duration_ms = (time.perf_counter() - started) * 1000
        run.status = STATUS_FAILED if run.error else STATUS_OK
        self._emit(
            {
                "type": "node",
                "data": {
                    "name": node.name,
                    "title": node.title,
                    "kind": node.kind,
                    "status": run.status,
                    "duration_ms": round(run.duration_ms, 1),
                    "error": run.error,
                    "output_key": run.output_key,
                },
            }
        )
        return run

    def _run_once(self, node: Node, state: WorkflowState, run: NodeRun) -> str:
        """执行一次，返回错误信息（空串表示成功）。"""
        if node.kind == KIND_END:
            return ""
        if node.kind == KIND_JUDGE:
            value = bool(node.cond(state)) if node.cond is not None else True
            state.set(node.output, value, source=node.name)
            run.output = value
            run.answer = "是" if value else "否"
            return ""
        if node.kind == KIND_COMMAND:
            return self._run_command(node, state, run)
        return self._run_agent(node, state, run)

    def _run_agent(self, node: Node, state: WorkflowState, run: NodeRun) -> str:
        task = state.render(node.task)
        output: AgentOutput = self._agent_runner(
            node.agent, task, {"node": node.name, "max_steps": None}
        )
        run.answer = output.answer
        run.usage = dict(output.usage)
        run.steps = output.steps
        if output.answer:
            state.set(node.output, output.answer, source=node.name)
            run.output = output.answer
        if output.success:
            return ""
        return output.error or "任务没能完成。"

    def _run_command(self, node: Node, state: WorkflowState, run: NodeRun) -> str:
        code = state.render(node.code)
        output: CommandOutput = self._command_runner(
            code, {"node": node.name, "timeout": node.timeout}
        )
        text = (output.stdout or "").strip()
        if output.stderr and output.stderr.strip():
            text = f"{text}\n--- stderr ---\n{output.stderr.strip()}".strip()
        if output.timed_out:
            text = f"{text}\n（超时被杀掉）".strip()
        state.set(node.output, text, source=node.name)
        state.set(f"{node.output}_ok", output.ok, source=node.name)
        state.set(f"{node.output}_exit", output.exit_code, source=node.name)
        run.output = text
        run.answer = text
        if output.ok:
            return ""
        if output.timed_out:
            return "命令执行超时。"
        return f"命令退出码 {output.exit_code}"

    # ------------------------------------------------------------------ 杂项

    def _record_skipped(self, record: FlowRun, node: Node, reason: str) -> None:
        node_run = NodeRun(
            name=node.name,
            title=node.title,
            kind=node.kind,
            status=STATUS_SKIPPED,
            skipped_reason=reason,
            output_key=node.output,
        )
        record.nodes.append(node_run)
        self._emit(
            {
                "type": "node",
                "data": {
                    "name": node.name,
                    "title": node.title,
                    "kind": node.kind,
                    "status": STATUS_SKIPPED,
                    "skipped_reason": reason,
                },
            }
        )

    def _emit(self, event: Event) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - 进度回调不能影响流程本身
                pass
