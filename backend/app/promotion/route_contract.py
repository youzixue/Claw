"""Pure route identity / frozen quality contract, not a trading authorization.

A route label, a global data watermark and per-candidate PIT/confirmation gates
are different namespaces. Never synthesize a missing historical gate from this
registry or from current data. Legacy names remain raw, without recategorization.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

ROUTE_CONTRACT_VERSION = "promotion_route_quality_v1_20260914"
ROUTE_LABELS = MappingProxyType({
    "platform_relaunch": "平台二次点火",
    "support_squeeze_start": "低位支撑首波",
    "fresh_mainline_start": "主线首波点火",
    "fresh_relay_start": "分支卡位热启动",
    "fresh_hot_start": "无记忆热启动",
    "news_catalyst_start": "消息催化首板",
    "auction_surge_start": "竞价高开强攻",
    "mainline_spread_start": "主线扩散补涨",
    "pre_board_probe_start": "涨停试盘首板",
    "oversold_reversal_start": "跌后反包首板",
    "hot_primary": "显性点火",
    "quiet_setup": "静默蓄势",
    "mainline_relay": "主线补涨",
    "relay_fillup": "主线补涨",
    "second_board_promotion": "首板晋级",
})
LEGACY_ROUTE_NAMES = frozenset({"fresh_hot_start", "mainline_relay"})
KNOWN_CANDIDATE_ROUTES = frozenset(ROUTE_LABELS)
GENERATED_CANDIDATE_ROUTES = KNOWN_CANDIDATE_ROUTES - LEGACY_ROUTE_NAMES
# Registration must not expand the B/C/D execution consumer subset.
EXECUTION_ROUTES = ("second_board_promotion", "mainline_spread_start", "auction_surge_start")
_BASE_DATASETS = frozenset({"stock_kline", "fund_flow", "limit_up_pool"})
REQUIRED_DATASETS_BY_ROUTE = MappingProxyType({
    route: _BASE_DATASETS | ({"auction_data"} if route == "auction_surge_start" else set())
    for route in ROUTE_LABELS
})
# Descriptions of independently required candidate checks, NOT their results.
_COMMON_EVIDENCE = ("candidate_price_history_and_confirmation", "tradeability_and_execution_risk")
CANDIDATE_EVIDENCE_CONTRACTS = MappingProxyType({
    route: _COMMON_EVIDENCE + {
        "news_catalyst_start": ("direct_verified_entity_and_content_analysis_available_before_decision",),
        "auction_surge_start": ("verified_incremental_auction_frames_before_decision",),
        "pre_board_probe_start": ("probe_shape_and_strict_confirmation",),
        "oversold_reversal_start": ("reversal_shape_and_strict_confirmation",),
    }.get(route, ())
    for route in ROUTE_LABELS
})


def route_identity(route) -> str:
    if not isinstance(route, str) or not route.strip():
        return "invalid_missing"
    if route in LEGACY_ROUTE_NAMES:
        return "known_legacy"
    return "known" if route in KNOWN_CANDIDATE_ROUTES else "unknown_illegal"


def route_contract_payload() -> dict:
    """Small owned JSON for NEW audit/run evidence only."""
    return {
        "version": ROUTE_CONTRACT_VERSION,
        "required_datasets_by_route": {
            route: sorted(datasets) for route, datasets in REQUIRED_DATASETS_BY_ROUTE.items()
        },
        "legacy_route_names": sorted(LEGACY_ROUTE_NAMES),
        "execution_routes": list(EXECUTION_ROUTES),
        "candidate_evidence_contracts": {
            route: list(checks) for route, checks in CANDIDATE_EVIDENCE_CONTRACTS.items()
        },
        "candidate_or_execution_authorization": False,
    }


def persisted_contract_status(quality) -> str:
    if not isinstance(quality, Mapping) or "route_contract" not in quality:
        return "legacy_unversioned"
    contract = quality.get("route_contract")
    if not isinstance(contract, dict):
        return "invalid_contract"
    if contract.get("version") != ROUTE_CONTRACT_VERSION:
        return "unsupported_contract_version"
    # Freeze the whole declaration, not just a version string attached to a
    # weakened dependency set. JSON is owned ledger data, not a live service.
    return "supported" if contract == route_contract_payload() else "contract_declaration_mismatch"


def persisted_route_gate(quality, route) -> dict:
    """Read the SAME persisted run only; missing/invalid proof is never True.

    Unversioned explicit per-route bits retain their original meaning, labelled
    as legacy evidence. A legacy *batch-only* pass is not route-level evidence.
    """
    quality = quality if isinstance(quality, Mapping) else {}
    contract_status = persisted_contract_status(quality)
    gates = quality.get("route_gates")
    identity = route_identity(route)
    known = identity in {"known", "known_legacy"}
    gate = gates.get(route) if known and isinstance(gates, dict) else None
    declared = isinstance(gate, dict)
    gate = gate if declared else {}
    raw_passed = gate.get("gate_passed")
    passed = raw_passed if type(raw_passed) is bool else None
    evidence = "route_gates" if isinstance(gates, dict) else "legacy_batch_gate_only"
    issue = None
    if not known:
        issue = identity
    elif contract_status not in {"supported", "legacy_unversioned"}:
        issue = contract_status
    elif not declared:
        issue = "missing_route_gate"
    elif type(raw_passed) is not bool:
        issue = "invalid_route_gate_boolean"
    elif contract_status == "supported" and gate.get("required_datasets") != sorted(REQUIRED_DATASETS_BY_ROUTE[route]):
        issue = "route_required_datasets_mismatch"
    elif passed is True and (
        gate.get("status", "ok") != "ok"
        or gate.get("blocking_datasets")
        or gate.get("blocking_issue_types")
        or gate.get("blocking_count", 0) != 0
    ):
        issue = "contradictory_route_gate"
    if issue is not None:
        passed = None
    return {
        "route_identity": identity,
        "declared_in_quality_contract": declared and known,
        "contract_status": contract_status,
        "recorded_contract_version": (
            quality["route_contract"].get("version")
            if isinstance(quality.get("route_contract"), dict) else None
        ),
        "gate_evidence": evidence,
        "gate_status": "passed" if passed is True else "blocked" if passed is False else "unknown",
        "gate_passed": passed,
        "raw_gate_passed": raw_passed if type(raw_passed) is bool else None,
        "gate_issue": issue,
        "blocking_datasets": [
            x for x in (gate.get("blocking_datasets") or []) if isinstance(x, str)
        ] if isinstance(gate.get("blocking_datasets"), list) else [],
        "blocking_issue_types": [
            x for x in (gate.get("blocking_issue_types") or []) if isinstance(x, str)
        ] if isinstance(gate.get("blocking_issue_types"), list) else [],
    }
