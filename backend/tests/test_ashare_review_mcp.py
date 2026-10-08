import io
import json
from email.message import Message
from urllib.error import URLError
from urllib.request import HTTPHandler, build_opener
from urllib.response import addinfourl

import pytest

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

    def fake_local(name, arguments):
        captured.append((name, arguments))
        return {"read_only": True}

    monkeypatch.setattr(mcp, "_local_read", fake_local)
    monkeypatch.setattr(mcp, "_http_get", lambda *a: pytest.fail("model evidence called business HTTP"))
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
        ("ashare_shadow_runs", {"artifact_id": 7, "target_board": 1}),
        ("ashare_shadow_evaluations", {"artifact_id": 7, "limit": 20}),
        ("ashare_deployments", {}),
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


@pytest.mark.parametrize("name,arguments,path,query", [
    ("paper_experiment_report", {}, "/paper/experiment/report", {}),
    ("paper_experiment_report", {"account_name": "challenger_f2"},
     "/paper/experiment/report", {"account_name": "challenger_f2"}),
    ("paper_daily_outcomes", {"target_date": "2026-09-30", "account_name": "mainline"},
     "/paper/audit/daily-outcomes", {"target_date": "2026-09-30", "account_name": "mainline"}),
    ("paper_candidate_shadow", {"trade_date": "2026-09-30", "route": "C3"},
     "/paper/research/candidate-shadow", {"trade_date": "2026-09-30", "route": "C3", "limit": "50"}),
    ("paper_candidate_shadow", {"trade_date": "2026-09-30", "limit": 200},
     "/paper/research/candidate-shadow", {"trade_date": "2026-09-30", "limit": "200"}),
    ("paper_c3_events", {"trade_date": "2026-09-30", "version": "all", "event_type": "block",
                        "page": 2, "page_size": 10, "keyword": "测试"},
     "/paper/research/c3/events", {"trade_date": "2026-09-30", "version": "all",
         "event_type": "block", "page": "2", "page_size": "10", "keyword": "测试"}),
])
def test_paper_tools_only_request_audited_endpoints(monkeypatch, name, arguments, path, query):
    captured = []
    evidence = {"read_only": True, "coverage_status": "missing", "accounts": []}
    monkeypatch.setattr(mcp, "_http_get", lambda p, q: captured.append((p, q)) or evidence)
    result = mcp._call_tool({"name": name, "arguments": arguments})
    assert result["isError"] is False
    assert result["structuredContent"] == evidence
    assert captured == [(path, query)]


@pytest.mark.parametrize("name,arguments", [
    ("paper_daily_outcomes", {}),
    ("paper_candidate_shadow", {}),
    ("paper_c3_events", {}),
    ("paper_candidate_shadow", {"trade_date": "2026-02-30"}),
    ("paper_candidate_shadow", {"trade_date": "20260930"}),
    ("paper_candidate_shadow", {"trade_date": "../../claw.db"}),
    ("paper_candidate_shadow", {"trade_date": "2026-09-30", "limit": True}),
    ("paper_candidate_shadow", {"trade_date": "2026-09-30", "limit": 0}),
    ("paper_candidate_shadow", {"trade_date": "2026-09-30", "limit": 201}),
    ("paper_candidate_shadow", {"trade_date": "2026-09-30", "route": "shared_50k"}),
    ("paper_c3_events", {"trade_date": "2026-09-30", "page_size": 101}),
    ("paper_c3_events", {"trade_date": "2026-09-30", "page": -1}),
    ("paper_c3_events", {"trade_date": "2026-09-30", "keyword": "x" * 81}),
    ("paper_c3_events", {"trade_date": "2026-09-30", "version": "promote"}),
    ("paper_experiment_report", {"account_name": "shared_50k"}),
    ("paper_experiment_report", {"account_name": "C3"}),
    ("paper_experiment_report", {"refresh": True}),
    ("ashare_prediction_quality", {"refresh": True}),
    ("ashare_review_snapshot", {"snapshot_id": True}),
    ("paper_experiment_report", []),
    ("paper_experiment_report", None),
    ("paper_account", {}),
    ("paper_challengers_comparison", {}),
    ("submit_order", {}),
])
def test_invalid_or_mutating_requests_never_reach_http(monkeypatch, name, arguments):
    def forbidden(*args):
        pytest.fail("invalid request crossed the read-only MCP boundary")
    monkeypatch.setattr(mcp, "_http_get", forbidden)
    assert mcp._call_tool({"name": name, "arguments": arguments})["isError"] is True


def test_catalog_advertises_readonly_and_stable_account_scope():
    catalog = mcp._handle({"id": 1, "method": "tools/list"})["result"]["tools"]
    for tool in catalog:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
    from app.paper.experiment import EXPERIMENT_ACCOUNTS
    for name in ("paper_experiment_report", "paper_daily_outcomes"):
        assert tuple(mcp.TOOLS[name]["inputSchema"]["properties"]["account_name"]["enum"]) == EXPERIMENT_ACCOUNTS


def test_http_get_rejects_redirect_into_account_refresh(monkeypatch):
    requests = []
    class FakeHTTP(HTTPHandler):
        def http_open(self, request):
            requests.append((request.get_method(), request.full_url))
            headers = Message()
            headers["Location"] = "http://127.0.0.1:8000/api/v1/paper/account"
            response = addinfourl(io.BytesIO(b"redirect denied"), headers, request.full_url, 302)
            response.msg = "Found"
            return response
    opener = build_opener(FakeHTTP(), mcp._NoRedirects())
    monkeypatch.setattr(mcp, "build_opener", lambda *handlers: opener)
    monkeypatch.setenv("CLAW_API_BASE_URL", "http://127.0.0.1:8000/api/v1")
    with pytest.raises(RuntimeError, match="HTTP 302"):
        mcp._http_get("/paper/experiment/report", {})
    assert requests == [("GET", "http://127.0.0.1:8000/api/v1/paper/experiment/report")]


def test_http_get_is_bounded_get_and_preserves_backend_error(monkeypatch):
    captured = []
    class FakeHTTP(HTTPHandler):
        def http_open(self, request):
            captured.append(request.get_method())
            headers = Message()
            headers["Content-Length"] = str(mcp.MAX_RESPONSE_BYTES + 1)
            response = addinfourl(io.BytesIO(b"{}"), headers, request.full_url, 200)
            response.msg = "OK"
            return response
    opener = build_opener(FakeHTTP(), mcp._NoRedirects())
    monkeypatch.setattr(mcp, "build_opener", lambda *handlers: opener)
    monkeypatch.setenv("CLAW_API_BASE_URL", "http://127.0.0.1:8000/api/v1")
    with pytest.raises(RuntimeError, match="size limit"):
        mcp._http_get("/paper/experiment/report", {})
    assert captured == ["GET"]


@pytest.mark.parametrize("body,expected,is_error", [
    (b"{}", {}, False),
    (b"[]", [], False),
    (b"null", None, False),
    (b"", None, True),
    (b"not-json", None, True),
    (b"\xff", None, True),
])
def test_http_empty_and_invalid_evidence_is_not_fabricated(monkeypatch, body, expected, is_error):
    class FakeHTTP(HTTPHandler):
        def http_open(self, request):
            response = addinfourl(io.BytesIO(body), Message(), request.full_url, 200)
            response.msg = "OK"
            return response

    opener = build_opener(FakeHTTP(), mcp._NoRedirects())
    monkeypatch.setattr(mcp, "build_opener", lambda *handlers: opener)
    result = mcp._call_tool({"name": "paper_experiment_report", "arguments": {}})
    assert result["isError"] is is_error
    payload = json.loads(result["content"][0]["text"])
    if is_error:
        assert payload == {
            "error": "Claw API returned invalid JSON", "tool": "paper_experiment_report",
        }
    else:
        assert payload == expected


def test_http_body_budget_is_enforced_without_content_length(monkeypatch):
    class FakeHTTP(HTTPHandler):
        def http_open(self, request):
            response = addinfourl(io.BytesIO(b" " * 9), Message(), request.full_url, 200)
            response.msg = "OK"
            return response

    opener = build_opener(FakeHTTP(), mcp._NoRedirects())
    monkeypatch.setattr(mcp, "build_opener", lambda *handlers: opener)
    monkeypatch.setattr(mcp, "MAX_RESPONSE_BYTES", 8)
    result = mcp._call_tool({"name": "paper_experiment_report", "arguments": {}})
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "Claw API response exceeds MCP size limit"


def test_backend_http_error_is_preserved_without_retry_or_write_fallback(monkeypatch):
    requests = []

    class FakeHTTP(HTTPHandler):
        def http_open(self, request):
            requests.append((request.get_method(), request.full_url))
            response = addinfourl(io.BytesIO(b"research unavailable"), Message(), request.full_url, 503)
            response.msg = "Service Unavailable"
            return response

    opener = build_opener(FakeHTTP(), mcp._NoRedirects())
    monkeypatch.setattr(mcp, "build_opener", lambda *handlers: opener)
    result = mcp._call_tool({"name": "paper_daily_outcomes", "arguments": {"target_date": "2026-09-30"}})
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "Claw API HTTP 503: research unavailable"
    assert len(requests) == 1 and requests[0][0] == "GET"


def test_backend_connection_error_does_not_return_empty_success(monkeypatch):
    class UnavailableHTTP(HTTPHandler):
        def http_open(self, request):
            raise URLError("connection refused")

    opener = build_opener(UnavailableHTTP(), mcp._NoRedirects())
    monkeypatch.setattr(mcp, "build_opener", lambda *handlers: opener)
    result = mcp._call_tool({"name": "paper_experiment_report", "arguments": {}})
    assert result["isError"] is True
    assert result["structuredContent"]["error"].endswith(": connection refused")


@pytest.mark.parametrize("section,cohort", [
    ("summary", "all"), ("features", "rising_or_limit"), ("features", "non_rising"),
])
def test_market_batch_sections_share_existing_local_tool(monkeypatch, section, cohort):
    calls = []
    monkeypatch.setattr(mcp, "_http_get", lambda *a: pytest.fail("batch fell back to business HTTP"))
    monkeypatch.setattr(mcp, "_local_read", lambda name, args: calls.append((name, args)) or {"read_only": True})
    arguments = {"trade_date": "2026-09-30", "as_of": "2026-09-30T21:45:00+08:00",
                 "section": section, "cohort": cohort, "limit": 200}
    response = mcp._call_tool({"name": "ashare_market_review_universe", "arguments": arguments})
    assert response["isError"] is False
    assert calls == [("ashare_market_review_universe", arguments)]


@pytest.mark.parametrize("bad", [{"section": "replay"}, {"cohort": "only_winners_no_denominator"}])
def test_market_batch_rejects_unknown_modes_without_read_or_refresh(monkeypatch, bad):
    monkeypatch.setattr(mcp, "_local_read", lambda *a: pytest.fail("invalid batch reached reader"))
    monkeypatch.setattr(mcp, "_http_get", lambda *a: pytest.fail("invalid batch reached HTTP"))
    response = mcp._call_tool({"name": "ashare_market_review_universe",
                              "arguments": {"trade_date": "2026-09-30", **bad}})
    assert response["isError"] is True
