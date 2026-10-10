"""Workflow 测试用的假执行器：把"编排逻辑"和"模型输出"分开测。"""

from __future__ import annotations

from agentcode.workflow import AgentOutput, CommandOutput


class ScriptedAgent:
    """按 agent 名返回预设答复，并记下每次调用（谁、什么任务、哪个节点）。

    ``replies`` 的值可以是字符串、:class:`AgentOutput`、异常，或者
    ``lambda 第几次调用: 答复`` 这样的函数——用来复现"同一个 agent 前两次
    给的结果不一样"（例如审查第一次不通过、第二次通过）。
    """

    def __init__(self, replies: dict | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[dict] = []

    def __call__(self, agent: str, task: str, options: dict) -> AgentOutput:
        self.calls.append({"agent": agent, "task": task, "node": options.get("node", "")})
        count = sum(1 for call in self.calls if call["agent"] == agent)
        reply = self.replies.get(agent, f"{agent} 的产出")
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(count)
        if isinstance(reply, AgentOutput):
            return reply
        return AgentOutput(answer=str(reply), success=True, usage={"total_tokens": 7})

    def nodes(self) -> list[str]:
        """调用过的节点名（按顺序）。"""
        return [call["node"] for call in self.calls]


class ScriptedCommand:
    """按调用顺序返回预设的 ``(退出码, 输出)``；用完就一直成功。"""

    def __init__(self, results: list[tuple[int, str]] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[str] = []

    def __call__(self, code: str, options: dict) -> CommandOutput:
        self.calls.append(code)
        exit_code, output = self.results.pop(0) if self.results else (0, "ok")
        return CommandOutput(exit_code=exit_code, stdout=output)
