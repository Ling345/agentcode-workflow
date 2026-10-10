"""节点怎么"真的干活"：调智能体、跑命令。

两件事被抽成可替换的执行器（**引擎只负责编排，不关心谁来执行**）：

- :func:`default_agent_runner`：复用项目1 的事件流 :func:`agentcode.web.runner.run_stream`
  （同一套模型、中间件、token 预算与工具装配），把事件流收敛成一次结果；
- :func:`default_command_runner`：复用项目1 的代码执行后端
  （``local`` 本机受限直跑 / ``docker`` 一次性容器），在代码工作目录里跑一段 Python。

测试与演示可以传入自己的执行器，这样"编排逻辑"不必依赖真实模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agentcode.config import Settings


@dataclass
class AgentOutput:
    """一次智能体调用的产出。"""

    answer: str = ""
    success: bool = True
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandOutput:
    """一次命令/代码执行的产出。"""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """退出码为 0 且没超时。"""
        return self.exit_code == 0 and not self.timed_out


#: ``(agent 名, 任务文本, 选项) -> 产出``
AgentRunner = Callable[[str, str, dict[str, Any]], AgentOutput]
#: ``(要执行的代码, 选项) -> 产出``
CommandRunner = Callable[[str, dict[str, Any]], CommandOutput]


def default_agent_runner(
    *,
    settings: Settings | None = None,
    llm_mode: str = "mock",
) -> AgentRunner:
    """把项目1 的智能体包装成节点执行器。"""

    def run(agent: str, task: str, options: dict[str, Any]) -> AgentOutput:
        from agentcode.web.runner import run_stream

        active = settings or Settings.from_env()
        answer = ""
        error = ""
        usage: dict[str, Any] = {}
        steps = 0
        for event in run_stream(
            agent,
            task,
            llm_mode=llm_mode,
            max_steps=options.get("max_steps"),
            settings=active,
        ):
            kind = event.get("type")
            data = event.get("data") or {}
            if kind == "answer":
                answer = str(data.get("answer") or "")
                usage = dict(data.get("usage") or {})
                steps = len(data.get("steps") or [])
                if not data.get("success", True):
                    error = str(data.get("error") or "任务没能完成。")
            elif kind == "error":
                error = str(data.get("message") or "运行失败。")
        return AgentOutput(
            answer=answer,
            success=not error,
            error=error,
            usage=usage,
            steps=steps,
        )

    return run


def default_command_runner(*, settings: Settings | None = None) -> CommandRunner:
    """在工作目录里跑代码，复用项目1 的执行后端（含超时与输出限制）。"""

    def run(code: str, options: dict[str, Any]) -> CommandOutput:
        from agentcode.tools.sandbox import ExecutionError, make_backend

        active = settings or Settings.from_env()
        root = Path(active.code_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        backend = make_backend(
            active.execution_backend,
            root,
            docker_image=active.docker_image,
            docker_binary=active.docker_binary,
            docker_memory=active.docker_memory,
            docker_cpus=active.docker_cpus,
            docker_pids_limit=active.docker_pids_limit,
            docker_user=active.docker_user,
        )
        timeout = float(options.get("timeout") or active.code_timeout or 60.0)
        try:
            result = backend.run(str(code), max(0.5, min(timeout, 60.0)))
        except ExecutionError as exc:
            return CommandOutput(exit_code=1, stderr=str(exc))
        return CommandOutput(
            exit_code=int(result.returncode),
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            timed_out=bool(result.timed_out),
        )

    return run
