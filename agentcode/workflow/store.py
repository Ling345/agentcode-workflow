"""运行记录落盘：跑完的流程能回看、能对比。

每次运行写一个 ``<run_id>.json`` 到 ``<trace_dir>/workflows/``（默认
``traces/workflows/``，和项目1 的其它运行产物一样不进仓库）。

写入用"先写临时文件再原子替换"：进程正好在写的时候被杀掉，也不会留下一个
半截的 JSON 让列表页解析崩掉。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agentcode.config import Settings

#: 运行记录目录名（挂在 trace_dir 下面）
WORKFLOW_DIRNAME = "workflows"


def default_run_dir(settings: Settings | None = None) -> Path:
    """默认的运行记录目录。"""
    active = settings
    trace_dir = Path(getattr(active, "trace_dir", "traces") or "traces")
    return trace_dir / WORKFLOW_DIRNAME


def _resolve_dir(
    directory: str | Path | None, settings: Settings | None
) -> Path:
    if directory is not None:
        return Path(directory)
    return default_run_dir(settings)


def save_run(
    run: Any,
    *,
    directory: str | Path | None = None,
    settings: Settings | None = None,
    label: str = "",
) -> Path:
    """把一次运行写成 JSON，返回文件路径。"""
    target_dir = _resolve_dir(directory, settings)
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = run.to_dict()
    payload["saved_at"] = time.time()
    if label:
        payload["label"] = str(label)
    path = target_dir / f"{run.run_id}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def load_run(
    run_id: str,
    *,
    directory: str | Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """按 run_id 读取一次运行的完整记录；没有就返回 ``None``。"""
    path = _resolve_dir(directory, settings) / f"{str(run_id or '').strip()}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(
    *,
    directory: str | Path | None = None,
    settings: Settings | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """列出最近的运行（摘要，不带节点明细），新的在前。"""
    target_dir = _resolve_dir(directory, settings)
    if not target_dir.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for path in target_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append(
            {
                "run_id": payload.get("run_id") or path.stem,
                "workflow": payload.get("workflow", ""),
                "title": payload.get("title", ""),
                "status": payload.get("status", ""),
                "started_at": payload.get("started_at", ""),
                "duration_ms": payload.get("duration_ms", 0),
                "steps": payload.get("steps", 0),
                "error": payload.get("error", ""),
                "label": payload.get("label", ""),
                "saved_at": payload.get("saved_at", 0.0),
            }
        )
    summaries.sort(key=lambda item: item["saved_at"] or 0.0, reverse=True)
    return summaries[: max(1, int(limit))]
