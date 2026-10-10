"""命令行上的 Workflow：list / show / run / runs / log（离线）。"""

from __future__ import annotations

import json
from pathlib import Path

from agentcode.cli import main
from agentcode.config import Settings
from agentcode.workflow.store import list_runs, load_run


def _env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agentcode.db"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_TRACE_DIR", str(tmp_path / "traces"))


def _settings() -> Settings:
    """和 CLI 读的是同一份环境（AGENT_TRACE_DIR 等）。"""
    return Settings.from_env()


def test_workflow_list_shows_the_builtin_flows(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["workflow", "list"]) == 0

    output = capsys.readouterr().out
    for name in ("mini_se", "test_pipeline", "code_review"):
        assert name in output
    assert "plan_and_solve" in output  # 用到了哪些 Agent 也要看得见


def test_workflow_show_prints_nodes_and_edges(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["workflow", "show", "test_pipeline"]) == 0

    output = capsys.readouterr().out
    assert "生成测试" in output
    assert "没过，回到生成" in output  # 回边（循环）要看得到
    assert "test_gen" in output


def test_workflow_show_rejects_an_unknown_flow(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["workflow", "show", "查无此流程"]) == 2

    assert "没有这个流程" in capsys.readouterr().out


def test_workflow_run_runs_a_flow_and_saves_the_record(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    capsys.readouterr()

    code = main(
        [
            "workflow",
            "run",
            "mini_se",
            "--llm",
            "mock",
            "--input",
            "request=写一个计算器",
            "--label",
            "第一次演示",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "成功" in output
    assert "① 需求分析" in output
    runs = list_runs(settings=_settings())
    assert len(runs) == 1
    record = load_run(runs[0]["run_id"], settings=_settings())
    assert record["status"] == "succeeded"
    assert record["inputs"] == {"request": "写一个计算器"}
    assert record["label"] == "第一次演示"
    assert [item["name"] for item in record["nodes"]][:2] == ["requirements", "design"]


def test_workflow_run_can_print_json(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    capsys.readouterr()

    assert (
        main(["workflow", "run", "mini_se", "--llm", "mock", "--input", "request=你好", "--json"])
        == 0
    )

    raw = capsys.readouterr().out
    payload = json.loads(raw[raw.index("{") :])
    assert payload["workflow"] == "mini_se"
    assert payload["status"] == "succeeded"
    assert payload["state"]["request"] == "你好"


def test_workflow_runs_and_log_show_saved_records(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    main(["workflow", "run", "mini_se", "--llm", "mock"])
    capsys.readouterr()
    run_id = list_runs(settings=_settings())[0]["run_id"]

    assert main(["workflow", "runs"]) == 0
    listed = capsys.readouterr().out
    assert run_id in listed
    assert "mini_se" in listed

    assert main(["workflow", "log", run_id]) == 0
    detail = capsys.readouterr().out
    assert "requirements" in detail
    assert "共享状态" in detail


def test_workflow_log_rejects_an_unknown_run(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["workflow", "log", "没有这个记录"]) == 2

    assert "没有这个运行记录" in capsys.readouterr().out


def test_test_pipeline_runs_end_to_end_offline(tmp_path, monkeypatch, capsys):
    """真机演练：离线模式也要能"生成测试 → 真的跑 pytest → 全绿"。"""
    _env(tmp_path, monkeypatch)
    capsys.readouterr()

    code = main(["workflow", "run", "test_pipeline", "--llm", "mock"])

    output = capsys.readouterr().out
    assert code == 0, output
    assert (tmp_path / "sandbox" / "test_calculator.py").is_file()
    record = load_run(list_runs(settings=_settings())[0]["run_id"], settings=_settings())
    assert record["status"] == "succeeded"
    assert record["state"]["passed"] is True
    assert record["state"]["result_ok"] is True
