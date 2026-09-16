#!/usr/bin/env python3
"""Read-only MCP stdio facade for Claw's governed A-share APIs.

The server deliberately exposes GET endpoints only. Model training, snapshot
creation, parameter changes, notes, orders, and database writes are outside this
boundary and remain explicit application/user actions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SERVER_NAME = "claw-ashare-readonly"
SERVER_VERSION = "1.1.0"
DEFAULT_API_BASE = "http://127.0.0.1:8000/api/v1"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_ROOT = PROJECT_ROOT / ".dsh" / "skills" / "ashare-daily-review" / "references"


TOOLS: dict[str, dict[str, Any]] = {
    "ashare_review_history": {
        "description": "List immutable premarket, intraday, or postmarket review snapshots.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {"type": "string", "enum": ["premarket", "intraday", "postmarket"]},
                "review_date": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
    },
    "ashare_review_snapshot": {
        "description": "Read one immutable review snapshot with optional prediction attributions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "integer", "minimum": 1},
                "include_attributions": {"type": "boolean", "default": True},
            },
            "required": ["snapshot_id"],
            "additionalProperties": False,
        },
    },
    "ashare_review_attributions": {
        "description": "List observed promotion-pipeline hits, recall/ranking misses, gate blocks, and false positives.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outcome_trade_date": {"type": "string", "description": "YYYY-MM-DD"},
                "target_board": {"type": "integer", "enum": [1, 2]},
                "attribution_type": {
                    "type": "string",
                    "enum": ["hit", "hit_not_actionable", "ranking_miss", "recall_miss", "false_positive"],
                },
                "market_regime": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 300},
            },
            "additionalProperties": False,
        },
    },
    "ashare_market_regimes": {
        "description": "Read versioned point-in-time A-share market-regime history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "primary_regime": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 120},
                "include_features": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    "ashare_prediction_runs": {
        "description": "List immutable production prediction runs and their model/data identities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trade_date": {"type": "string", "description": "YYYY-MM-DD"},
                "snapshot_context": {"type": "string"},
                "model_version": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "additionalProperties": False,
        },
    },
    "ashare_training_runs": {
        "description": "Read challenger training records, walk-forward metrics, and acceptance decisions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_board": {"type": "integer", "enum": [1, 2]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            "additionalProperties": False,
        },
    },
    "ashare_shadow_runs": {
        "description": "Read paired Champion/Challenger scores produced on the same immutable candidate run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "integer", "minimum": 1},
                "target_board": {"type": "integer", "enum": [1, 2]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 300, "default": 100},
            },
            "additionalProperties": False,
        },
    },
    "ashare_shadow_evaluations": {
        "description": "Read cumulative out-of-time shadow metrics, regime slices, and manual-review eligibility.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "integer", "minimum": 1},
                "target_board": {"type": "integer", "enum": [1, 2]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 300, "default": 100},
            },
            "additionalProperties": False,
        },
    },
    "ashare_deployments": {
        "description": "Read current per-lane manual deployment state and append-only approval/rollback audit events.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "ashare_prediction_quality": {
        "description": "Read the latest prediction truth, calendar, and snapshot quality audit without refreshing it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot_context": {"type": "string"},
                "lookback_days": {"type": "integer", "minimum": 20, "maximum": 365, "default": 120},
            },
            "additionalProperties": False,
        },
    },
}


RESOURCE_FILES = {
    "claw://contracts/domain": REFERENCE_ROOT / "domain-contract.md",
    "claw://contracts/evaluation": REFERENCE_ROOT / "evaluation-policy.md",
    "claw://contracts/tools": REFERENCE_ROOT / "tool-contract.md",
}


def _api_base() -> str:
    return os.environ.get("CLAW_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")


def _clean_query(arguments: dict[str, Any], allowed: set[str]) -> dict[str, str]:
    query: dict[str, str] = {}
    for key in allowed:
        value = arguments.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            query[key] = "true" if value else "false"
        else:
            query[key] = str(value)
    return query


def _tool_request(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, str]]:
    if name == "ashare_review_history":
        return "/daily-review/snapshots", _clean_query(arguments, {"phase", "review_date", "limit"})
    if name == "ashare_review_snapshot":
        snapshot_id = int(arguments["snapshot_id"])
        if snapshot_id < 1:
            raise ValueError("snapshot_id must be positive")
        return f"/daily-review/snapshots/{snapshot_id}", _clean_query(arguments, {"include_attributions"})
    if name == "ashare_review_attributions":
        return "/daily-review/attributions", _clean_query(
            arguments,
            {"outcome_trade_date", "target_board", "attribution_type", "market_regime", "limit"},
        )
    if name == "ashare_market_regimes":
        return "/market-regime/history", _clean_query(
            arguments,
            {"start_date", "end_date", "primary_regime", "limit", "include_features"},
        )
    if name == "ashare_prediction_runs":
        return "/model-lab/runs", _clean_query(
            arguments, {"trade_date", "snapshot_context", "model_version", "limit"}
        )
    if name == "ashare_training_runs":
        return "/model-lab/training-runs", _clean_query(arguments, {"target_board", "limit"})
    if name == "ashare_shadow_runs":
        return "/model-lab/shadow-runs", _clean_query(
            arguments, {"artifact_id", "target_board", "limit"}
        )
    if name == "ashare_shadow_evaluations":
        return "/model-lab/shadow-evaluations", _clean_query(
            arguments, {"artifact_id", "target_board", "limit"}
        )
    if name == "ashare_deployments":
        return "/model-lab/deployments", {}
    if name == "ashare_prediction_quality":
        # Never pass refresh=true: this MCP boundary is intentionally read-only.
        return "/governance/prediction-quality", _clean_query(
            arguments, {"snapshot_context", "lookback_days"}
        )
    raise ValueError(f"unknown tool: {name}")


def _http_get(path: str, query: dict[str, str]) -> Any:
    url = f"{_api_base()}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    request = Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Claw API response exceeds MCP size limit")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(8192).decode("utf-8", errors="replace")
        raise RuntimeError(f"Claw API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Claw API is unavailable at {_api_base()}: {exc.reason}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Claw API response exceeds MCP size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Claw API returned invalid JSON") from exc


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if name not in TOOLS:
        return _tool_result({"error": f"unknown tool: {name}"}, is_error=True)
    if not isinstance(arguments, dict):
        return _tool_result({"error": "tool arguments must be an object"}, is_error=True)
    try:
        path, query = _tool_request(name, arguments)
        return _tool_result(_http_get(path, query))
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return _tool_result({"error": str(exc), "tool": name}, is_error=True)


def _resource_list() -> list[dict[str, Any]]:
    return [
        {
            "uri": uri,
            "name": path.stem.replace("-", " ").title(),
            "description": "Claw A-share point-in-time review contract",
            "mimeType": "text/markdown",
        }
        for uri, path in RESOURCE_FILES.items()
    ]


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method and str(method).startswith("notifications/"):
        return None
    if method == "initialize":
        requested_version = (request.get("params") or {}).get("protocolVersion")
        result = {
            "protocolVersion": requested_version or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Read-only local A-share evidence. Preserve dates, as-of times, quality, and model/data versions.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": name, **definition} for name, definition in TOOLS.items()
            ]
        }
    elif method == "tools/call":
        result = _call_tool(request.get("params") or {})
    elif method == "resources/list":
        result = {"resources": _resource_list()}
    elif method == "resources/read":
        uri = str((request.get("params") or {}).get("uri") or "")
        path = RESOURCE_FILES.get(uri)
        if path is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32002, "message": f"unknown resource: {uri}"},
            }
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32002, "message": str(exc)},
            }
        result = {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": content}]}
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve() -> int:
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response = _handle(request)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


def self_test() -> int:
    initialize = _handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    tools = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    resources = _handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
    assert initialize and initialize["result"]["serverInfo"]["name"] == SERVER_NAME
    assert tools and len(tools["result"]["tools"]) == len(TOOLS)
    assert resources and len(resources["result"]["resources"]) == len(RESOURCE_FILES)
    for path in RESOURCE_FILES.values():
        assert path.is_file(), f"missing resource: {path}"
    print(json.dumps({"status": "ok", "tools": sorted(TOOLS), "resources": sorted(RESOURCE_FILES)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    return self_test() if args.self_test else serve()


if __name__ == "__main__":
    raise SystemExit(main())
