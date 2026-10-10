"""在线运行：把"正在跑的流程"的进度留在内存里，供网页实时看。

CLI 是"跑完再看记录"；网页要的是"边跑边看"。所以这里在后台线程里跑引擎，
把每个事件同时做两件事：写进本次运行的事件列表（迟到的订阅者能补上）、
叫醒正在等的订阅者。

跑完之后仍然把完整记录落盘（:mod:`agentcode.workflow.store`）——内存里的东西
重启就没了，能回看的记录才算数。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from agentcode.config import Settings
from agentcode.workflow.engine import WorkflowEngine
from agentcode.workflow.model import Workflow
from agentcode.workflow.runners import AgentRunner, CommandRunner
from agentcode.workflow.store import save_run

#: 跑完的运行在内存里留多久（秒）
DEFAULT_TTL_SECONDS = 1800.0
#: 内存里最多留多少个运行
DEFAULT_MAX_RUNS = 50


@dataclass
class LiveRun:
    """一次正在跑（或刚跑完）的流程。"""

    run_id: str
    workflow: str
    title: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    error: str = ""
    record_path: str = ""
    finished: bool = False
    started_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def summary(self) -> dict[str, Any]:
        """给列表页看的摘要。"""
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "title": self.title,
            "status": self.status,
            "inputs": dict(self.inputs),
            "error": self.error,
            "record_path": self.record_path,
            "finished": self.finished,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)),
        }


class LiveRuns:
    """内存里的运行登记表。线程安全。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_runs: int = DEFAULT_MAX_RUNS,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_runs = max(1, int(max_runs))
        self._runs: dict[str, LiveRun] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 启动

    def start(
        self,
        workflow: Workflow,
        inputs: dict[str, Any] | None = None,
        *,
        settings: Settings | None = None,
        llm_mode: str = "mock",
        agent_runner: AgentRunner | None = None,
        command_runner: CommandRunner | None = None,
        label: str = "",
        run_id: str | None = None,
        on_finished: Callable[[Any], None] | None = None,
    ) -> LiveRun:
        """后台起一次运行，立刻返回它的登记信息。"""
        live = LiveRun(
            run_id=run_id or uuid.uuid4().hex[:12],
            workflow=workflow.name,
            title=workflow.title,
            inputs=dict(inputs or {}),
        )
        with self._lock:
            self._runs[live.run_id] = live
            self._prune_locked()

        def publish(event: dict[str, Any]) -> None:
            with live.condition:
                live.events.append(event)
                live.condition.notify_all()

        def worker() -> None:
            try:
                engine = WorkflowEngine(
                    workflow,
                    settings=settings,
                    llm_mode=llm_mode,
                    on_event=publish,
                    run_id=live.run_id,
                    agent_runner=agent_runner,
                    command_runner=command_runner,
                )
                record = engine.run(live.inputs)
                try:
                    path = save_run(record, settings=settings, label=label)
                    live.record_path = str(path)
                except Exception as exc:  # noqa: BLE001 - 落盘失败不该让运行本身算失败
                    publish({"type": "warning", "data": {"message": f"运行记录没能落盘：{exc}"}})
                live.status = record.status
                live.error = record.error
                if on_finished is not None:
                    try:
                        on_finished(record)
                    except Exception as exc:  # noqa: BLE001 - 收尾失败不影响这次运行的结果
                        publish(
                            {"type": "warning", "data": {"message": f"收尾处理失败：{exc}"}}
                        )
                publish(
                    {
                        "type": "saved",
                        "data": {
                            "run_id": live.run_id,
                            "status": record.status,
                            "error": record.error,
                            "record_path": live.record_path,
                            "steps": record.executed_steps(),
                        },
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 后台线程不能静默死掉
                live.status = "failed"
                live.error = f"运行失败：{exc}"
                publish(
                    {"type": "done", "data": {"run_id": live.run_id, "status": "failed", "error": live.error}}
                )
                if on_finished is not None:
                    try:
                        on_finished(None)
                    except Exception:  # noqa: BLE001 - 同上
                        pass
            finally:
                with live.condition:
                    live.finished = True
                    live.condition.notify_all()

        thread = threading.Thread(
            target=worker, name=f"agentcode-workflow-{live.run_id}", daemon=True
        )
        thread.start()
        return live

    # ------------------------------------------------------------------ 查询

    def get(self, run_id: str | None) -> LiveRun | None:
        """按 run_id 取运行。"""
        with self._lock:
            return self._runs.get(str(run_id or ""))

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近的运行（新的在前）。"""
        with self._lock:
            runs = list(self._runs.values())
        runs.sort(key=lambda item: item.started_at, reverse=True)
        return [item.summary() for item in runs[: max(1, int(limit))]]

    def subscribe(self, live: LiveRun, from_index: int = 0) -> Iterator[dict[str, Any]]:
        """从 ``from_index`` 开始产出事件，直到这次运行结束。"""
        cursor = max(0, int(from_index))
        while True:
            with live.condition:
                while cursor >= len(live.events) and not live.finished:
                    live.condition.wait(timeout=1.0)
                batch = list(live.events[cursor:])
                cursor += len(batch)
                done = live.finished and cursor >= len(live.events)
            for event in batch:
                yield event
            if done:
                return

    # ------------------------------------------------------------------ 内部

    def _prune_locked(self) -> None:
        """清掉过期的、以及超量的已结束运行（调用方需持锁）。"""
        now = time.time()
        stale = [
            run_id
            for run_id, live in self._runs.items()
            if live.finished and now - live.started_at > self.ttl_seconds
        ]
        for run_id in stale:
            self._runs.pop(run_id, None)
        if len(self._runs) <= self.max_runs:
            return
        finished = sorted(
            (live for live in self._runs.values() if live.finished),
            key=lambda live: live.started_at,
        )
        for live in finished:
            if len(self._runs) <= self.max_runs:
                break
            self._runs.pop(live.run_id, None)
