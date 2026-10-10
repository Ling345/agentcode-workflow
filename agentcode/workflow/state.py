"""Workflow 的共享状态：Agent 之间靠它传递消息。

为什么需要一层状态而不是把上一个 Agent 的输出直接拼进下一个的提示词：流程里
可能有分支、循环和并行分支，谁写了什么、什么时候写的，必须能查。所以这里是
一个带**加锁**与**写入历史**的键值表：

- 值可以是任意 Python 对象，写进提示词时按可读形式转成文本；
- 每个键都带来源（哪个节点写的），出问题时能一眼看出是谁污染的；
- ``snapshot()`` 给的是副本，界面与日志随便改，不会影响运行中的流程。
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Mapping

#: 任务模板里的占位符：``${变量名}``
PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def stringify(value: Any) -> str:
    """把状态里的值转成能写进提示词的文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


class WorkflowState:
    """一次流程运行里的共享状态。线程安全（并行分支会同时写）。"""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._values: dict[str, Any] = dict(initial or {})
        self._history: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 读写

    def get(self, key: str, default: Any = None) -> Any:
        """取一个值；没有就返回 ``default``。"""
        with self._lock:
            return self._values.get(str(key), default)

    def set(self, key: str, value: Any, *, source: str = "") -> None:
        """写一个值，并记下是谁写的。"""
        name = str(key)
        with self._lock:
            self._values[name] = value
            self._history.append(
                {
                    "key": name,
                    "source": str(source),
                    "at": time.strftime("%H:%M:%S", time.localtime()),
                    "preview": stringify(value)[:120],
                }
            )

    def update(self, values: Mapping[str, Any], *, source: str = "") -> None:
        """批量写。"""
        for key, value in values.items():
            self.set(key, value, source=source)

    def keys(self) -> list[str]:
        """当前全部键（按写入顺序）。"""
        with self._lock:
            return list(self._values)

    def snapshot(self) -> dict[str, Any]:
        """当前状态的副本。"""
        with self._lock:
            return dict(self._values)

    def history(self) -> list[dict[str, Any]]:
        """写入历史（谁、什么时候、写了什么）。"""
        with self._lock:
            return [dict(item) for item in self._history]

    # ------------------------------------------------------------------ 模板

    def render(self, template: str) -> str:
        """把 ``${变量}`` 替换成状态里的值。

        变量不存在时**原样保留**占位符：写错变量名时要看得见，而不是悄悄变成
        空白，让人以为是节点没跑。
        """
        text = str(template or "")
        with self._lock:
            values = dict(self._values)

        def replace(match: re.Match) -> str:
            key = match.group(1).strip()
            if key in values:
                return stringify(values[key])
            return match.group(0)

        return PLACEHOLDER.sub(replace, text)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._values

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"WorkflowState({sorted(self._values)})"
