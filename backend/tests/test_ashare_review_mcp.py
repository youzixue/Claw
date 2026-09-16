import json

from scripts import ashare_review_mcp as mcp


def test_mcp_catalog_and_resources_are_available():
    initialized = mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert initialized["result"]["serverInfo"]["name"] == "claw-ashare-readonly"

    tools = mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {item["name"] for item in tools["result"]["tools"]}
    assert names == set(mcp.TOOLS)
    assert "ashare_prediction_quality" in names
    assert {"ashare_shadow_runs", "ashare_shadow_evaluations", "ashare_deployments"} <= names

    resources = mcp._handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert len(resources["result"]["resources"]) == 3
    first_uri = resources["result"]["resources"][0]["uri"]
    content = mcp._handle(
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": first_uri}}
    )
    assert content["result"]["contents"][0]["text"].startswith("#")


def test_mcp_tools_are_get_only_and_quality_cannot_refresh(monkeypatch):
    captured = []

    def fake_get(path, query):
        captured.append((path, query))
        return {"path": path, "query": query, "gate_passed": False}

    monkeypatch.setattr(mcp, "_http_get", fake_get)
    response = mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "ashare_prediction_quality",
                "arguments": {"snapshot_context": "promotion_2000", "lookback_days": 60},
            },
        }
    )
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["gate_passed"] is False
    assert captured == [
        (
            "/governance/prediction-quality",
            {"snapshot_context": "promotion_2000", "lookback_days": "60"},
        )
    ]
    assert "refresh" not in captured[0][1]



def test_mcp_shadow_and_deployment_tools_remain_read_only(monkeypatch):
    captured = []

    def fake_get(path, query):
        captured.append((path, query))
        return {"path": path, "query": query}

    monkeypatch.setattr(mcp, "_http_get", fake_get)
    for name, arguments in (
        ("ashare_shadow_runs", {"artifact_id": 7, "target_board": 1}),
        ("ashare_shadow_evaluations", {"artifact_id": 7, "limit": 20}),
        ("ashare_deployments", {}),
    ):
        response = mcp._handle(
            {
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert response["result"]["isError"] is False

    assert captured == [
        ("/model-lab/shadow-runs", {"artifact_id": "7", "target_board": "1"}),
        ("/model-lab/shadow-evaluations", {"artifact_id": "7", "limit": "20"}),
        ("/model-lab/deployments", {}),
    ]


def test_mcp_stdio_handler_reports_tool_errors_without_crashing():
    response = mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "ashare_review_snapshot", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["tool"] == "ashare_review_snapshot"
