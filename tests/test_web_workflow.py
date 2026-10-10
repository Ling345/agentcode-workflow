"""网页上的 Workflow 可视化：列表、流程图、在线运行、运行记录（离线）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from agentcode.web.server import create_server


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    server = create_server(
        host="127.0.0.1", port=0, llm_mode="mock", quiet=True, require_auth=False
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")


def _post(url: str, payload: dict) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read().decode("utf-8")


def test_the_workflow_page_is_served(web):
    status, content_type, html = _get(f"{web}/workflow")

    assert status == 200
    assert "text/html" in content_type
    assert "工作流" in html
    assert "/static/workflow.js" in html


def test_the_workflow_list_endpoint_returns_the_builtin_flows(web):
    status, _, body = _get(f"{web}/api/workflows")

    payload = json.loads(body)
    names = {item["name"] for item in payload["workflows"]}
    assert status == 200
    assert {"mini_se", "test_pipeline", "code_review"} <= names


def test_the_detail_endpoint_returns_nodes_and_edges(web):
    status, _, body = _get(f"{web}/api/workflow?name=test_pipeline")

    payload = json.loads(body)
    assert status == 200
    assert payload["workflow"]["name"] == "test_pipeline"
    nodes = {node["name"]: node for node in payload["workflow"]["nodes"]}
    assert "generate" in nodes
    labels = [edge["label"] for edge in nodes["check"]["edges"]]
    assert "没过，回到生成" in labels


def test_the_detail_endpoint_rejects_an_unknown_flow(web):
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(f"{web}/api/workflow?name={quote('查无此流程')}")

    assert error.value.code == 404
    assert "没有这个流程" in error.value.read().decode("utf-8")


def test_running_a_workflow_streams_progress_and_is_saved(web):
    status, body = _post(
        f"{web}/api/workflow/run",
        {"name": "mini_se", "inputs": {"request": "写一个计算器"}},
    )
    started = json.loads(body)
    assert status == 202
    assert started["run_id"]
    assert started["stream"] == f"/api/workflow/stream?run_id={started['run_id']}"

    _, content_type, text = _get(f"{web}{started['stream']}")
    assert "text/event-stream" in content_type
    assert "event: node" in text
    assert "event: saved" in text
    assert '"status": "succeeded"' in text

    _, _, listed = _get(f"{web}/api/workflow/runs")
    assert started["run_id"] in listed

    _, _, record_body = _get(f"{web}/api/workflow/record?run_id={started['run_id']}")
    record = json.loads(record_body)
    assert record["status"] == "succeeded"
    assert record["state"]["request"] == "写一个计算器"
    assert [item["name"] for item in record["nodes"]][:2] == ["requirements", "design"]


def test_running_an_unknown_workflow_is_rejected(web):
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{web}/api/workflow/run", {"name": "查无此流程"})

    assert error.value.code == 404


def test_an_unknown_live_run_cannot_be_streamed(web):
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(f"{web}/api/workflow/stream?run_id={quote('没有这个运行')}")

    assert error.value.code == 404
