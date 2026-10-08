"""Independent close-up research rankings; never a production execution signal.

Only newly emitted candidates are annotated. Historical snapshots without this
contract stay unavailable: no reconstruction from outcomes or today's scores.
"""
from __future__ import annotations

import math

DIRECTION_LABEL_VERSION = "next_day_close_up_v1"
DIRECTION_RESEARCH_VERSION = "direction_rank_research_v1"
DIRECTION_RANK_LIMIT = 12
DIRECTION_TARGET_PRECISION = 0.80


def valid_probability(value) -> bool:
    return (type(value) in (int, float) and 0 <= value <= 1
            and math.isfinite(value))


def annotate_direction_research(candidates: list[dict], *, limit: int = DIRECTION_RANK_LIMIT) -> tuple[list[dict], dict]:
    """Keep the existing eligible universe and every Champion field unchanged.

    One missing eligible score blocks the entire research ranking instead of
    selectively dropping that stock. Stable code ties never depend on outcomes.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("direction rank limit must be a positive integer")
    lane = [item for item in candidates if item.get("target_board", 1) == 1]
    codes = [str(item.get("code") or "").strip() for item in lane]
    error = "duplicate_or_missing_code" if (not all(type(item.get("code")) is str
        and item["code"] == code and len(code) == 6 and code.isascii() and code.isdigit()
        for item, code in zip(lane, codes)) or len(set(codes)) != len(codes)) else ""
    eligible = [item for item in lane
                if (item.get("probability_factors") or {}).get("prediction_rank_eligible") is True]
    missing = sum(not valid_probability(item.get("direction_probability")) for item in eligible)
    if missing and not error:
        error = "eligible_direction_probability_missing"
    ordered = sorted(eligible, key=lambda item: (-item["direction_probability"], item["code"])) if not error else []
    positions = {item["code"]: index for index, item in enumerate(ordered[:limit], 1)}
    annotated = []
    for item in candidates:
        if item.get("target_board", 1) != 1:
            annotated.append(item)
            continue
        factors = item.get("probability_factors") or {}
        probability = item.get("direction_probability")
        proof = {
            "version": DIRECTION_RESEARCH_VERSION,
            "label_version": DIRECTION_LABEL_VERSION,
            "scope": "research_only",
            "probability": probability if valid_probability(probability) else None,
            "probability_method": str(factors.get("learning_direction_probability_method") or "unknown"),
            "eligible": factors.get("prediction_rank_eligible") is True,
            "selected": item.get("code") in positions,
            "rank_position": positions.get(item.get("code")),
            "rank_limit": limit,
            "candidate_count": len(lane),
            "eligible_count": len(eligible),
            "selected_count": len(positions),
            "rank_contract_complete": not bool(error),
            "error": error or None,
        }
        annotated.append({**item, "probability_factors": {**factors, "direction_research": proof}})
    selected = sorted([item for item in annotated if item.get("code") in positions
                       and item.get("target_board", 1) == 1], key=lambda item: positions[item["code"]])
    metadata = {
        "version": DIRECTION_RESEARCH_VERSION, "label_version": DIRECTION_LABEL_VERSION,
        "scope": "research_only", "rank_limit": limit,
        "target_precision": DIRECTION_TARGET_PRECISION,
        "candidate_count": len(lane), "eligible_count": len(eligible),
        "missing_probability_count": missing, "selected_count": len(selected),
        "status": "blocked" if error else "available" if len(selected) == limit else "insufficient_candidates",
        "reason": error or None,
        "candidates": selected,
        "production_unchanged": True, "manual_review_eligible": False,
        "notes": ["按独立次日收涨概率排序，不按涨停经验分排序；当前概率来源保持原样，非新模型已获验证",
                  "仅研究观察，不替换正式涨停榜、不生成订单；缺少冻结证据不得回补旧榜"],
    }
    return annotated, metadata


def frozen_direction_probability(factors: dict) -> float:
    """Read a frozen, versioned directional baseline, never the limit-up score."""
    proof = factors.get("direction_research")
    if (not isinstance(proof, dict)
            or proof.get("version") != DIRECTION_RESEARCH_VERSION
            or proof.get("label_version") != DIRECTION_LABEL_VERSION
            or proof.get("scope") != "research_only"
            or proof.get("rank_contract_complete") is not True
            or not valid_probability(proof.get("probability"))):
        raise ValueError("frozen_direction_contract_missing_or_invalid")
    return float(proof["probability"])


def validate_frozen_direction_universe(entries: list[tuple[str, dict]]) -> None:
    """Require the complete frozen ranking contract before selecting eligible rows."""
    proofs = {code: factors["direction_research"] for code, factors in entries}
    if len(proofs) != len(entries):
        raise ValueError("direction_frozen_universe_duplicate")
    eligible = []
    for code, factors in entries:
        probability = frozen_direction_probability(factors)
        proof = proofs[code]
        if (type(proof.get("eligible")) is not bool
                or proof["eligible"] != (factors.get("prediction_rank_eligible") is True)):
            raise ValueError("direction_frozen_eligibility_mismatch")
        if proof["eligible"]:
            eligible.append((code, probability))
    eligible.sort(key=lambda item: (-item[1], item[0]))
    positions = {code: index for index, (code, _) in enumerate(eligible[:DIRECTION_RANK_LIMIT], 1)}
    for code, proof in proofs.items():
        if (type(proof.get("candidate_count")) is not int or proof["candidate_count"] != len(entries)
                or type(proof.get("eligible_count")) is not int or proof["eligible_count"] != len(eligible)
                or type(proof.get("selected_count")) is not int or proof["selected_count"] != len(positions)
                or type(proof.get("rank_limit")) is not int or proof["rank_limit"] != DIRECTION_RANK_LIMIT
                or type(proof.get("selected")) is not bool or proof["selected"] != (code in positions)
                or (code in positions and type(proof.get("rank_position")) is not int)
                or proof.get("rank_position") != positions.get(code)):
            raise ValueError("direction_frozen_universe_incomplete_or_rank_mismatch")


def project_frozen_direction_research(rows: list[dict], *, batch_valid: bool) -> dict:
    """Read an explicitly selected immutable batch; no re-ranking or fallback."""
    result = {
        "version": DIRECTION_RESEARCH_VERSION, "label_version": DIRECTION_LABEL_VERSION,
        "scope": "research_only", "rank_limit": DIRECTION_RANK_LIMIT,
        "target_precision": DIRECTION_TARGET_PRECISION, "production_unchanged": True,
        "manual_review_eligible": False, "frozen": True, "persistence_status": "immutable_ledger",
        "status": "blocked", "reason": "research_batch_incomplete",
        "candidate_count": len(rows) if type(rows) is list else None, "eligible_count": None, "selected_count": 0,
        "missing_probability_count": None, "missing_recordable_count": None, "candidates": [],
        "notes": ["只读所选历史批次的原始冻结排名；不会替换当前最新榜或解除其阻断",
                  "未认证历史身份、封存结局或训练资格；不是买入建议，不生成订单"],
    }
    if batch_valid is not True or type(rows) is not list or not rows:
        return result
    if any(type(row) is not dict or type(row.get("code")) is not str or len(row["code"]) != 6
           or not row["code"].isascii() or not row["code"].isdigit()
           or type(row.get("factors")) is not dict for row in rows):
        return result
    entries = [(row["code"], row["factors"]) for row in rows]
    proofs = [factors.get("direction_research") for _, factors in entries]
    if any(type(proof) is not dict for proof in proofs):
        return {**result, "status": "unavailable", "reason": "direction_research_contract_missing"}
    if any(proof.get("version") != DIRECTION_RESEARCH_VERSION
           or proof.get("label_version") != DIRECTION_LABEL_VERSION
           or proof.get("scope") != "research_only" for proof in proofs):
        return {**result, "status": "unavailable", "reason": "direction_research_contract_unsupported"}
    if any(type(factors.get("prediction_rank_eligible")) is not bool for _, factors in entries) or any(
        "rank_position" not in proof for proof in proofs
    ):
        return {**result, "reason": "direction_research_contract_mismatch"}
    result["eligible_count"] = sum(proof.get("eligible") is True for proof in proofs)
    result["missing_probability_count"] = sum(proof.get("eligible") is True
        and not valid_probability(proof.get("probability")) for proof in proofs)
    missing = [proof.get("missing_recordable_count") for proof in proofs]
    if any("missing_recordable_count" in proof for proof in proofs):
        if not (all(type(count) is int and count >= 0 for count in missing)
                and len(set(missing)) == 1):
            return {**result, "reason": "direction_research_contract_mismatch"}
        result["missing_recordable_count"] = missing[0]
    if any("error" not in proof or (proof["error"] is not None and type(proof["error"]) is not str)
           or type(proof.get("probability_method")) is not str or not proof["probability_method"]
           for proof in proofs):
        return {**result, "reason": "direction_research_contract_mismatch"}
    # An explicit error cannot be erased by a contradictory complete=True flag.
    errors = {proof["error"] for proof in proofs if proof["error"]}
    if errors:
        return {**result, "reason": sorted(errors)[0]}
    if result["missing_recordable_count"]:
        return {**result, "reason": "direction_candidates_not_recordable"}
    try:
        validate_frozen_direction_universe(entries)
    except (KeyError, TypeError, ValueError):
        return {**result, "reason": "direction_research_contract_mismatch"}
    selected = []
    # Only expose the known scalar evidence contract, not arbitrary nested JSON
    # injected alongside it (including unserializable/non-finite extra values).
    proof_fields = (
        "version", "label_version", "scope", "probability", "probability_method",
        "eligible", "selected", "rank_position", "rank_limit", "candidate_count",
        "eligible_count", "selected_count", "rank_contract_complete", "error",
        "missing_recordable_count",
    )
    for row in rows:
        proof = row["factors"]["direction_research"]
        if proof["selected"]:
            selected.append({
                "code": row["code"], "name": row.get("name") if type(row.get("name")) is str else None, "target_board": 1,
                "direction_probability": proof["probability"],
                "limit_up_probability": (row.get("limit_up_probability")
                                         if valid_probability(row.get("limit_up_probability")) else None),
                "probability_factors": {"direction_research": {key: proof[key] for key in proof_fields if key in proof}},
            })
    selected.sort(key=lambda item: item["probability_factors"]["direction_research"]["rank_position"])
    return {**result, "status": "available" if len(selected) == DIRECTION_RANK_LIMIT else "insufficient_candidates",
            "reason": None, "selected_count": len(selected), "candidates": selected}
