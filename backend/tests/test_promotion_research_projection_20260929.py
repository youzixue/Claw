"""Display projection cannot silently regenerate incomplete recorded research ranks."""
from copy import deepcopy

import pytest

from app.api.v1 import promotion as p
from app.promotion.direction_research import annotate_direction_research


def candidates():
    return annotate_direction_research([
        {"code": "600001", "target_board": 1, "probability": .8, "direction_probability": .2,
         "probability_factors": {"prediction_rank_eligible": True}},
        {"code": "600002", "target_board": 1, "probability": .1, "direction_probability": .7,
         "probability_factors": {"prediction_rank_eligible": True}},
    ])[0]


def test_display_preserves_independent_ranking_and_never_certifies_persistence():
    rows = candidates()
    original = deepcopy(rows)
    result = p._direction_research_payload(rows)
    assert result["status"] == "insufficient_candidates"
    assert [r["code"] for r in result["candidates"]] == ["600002", "600001"]
    assert result["candidate_count"] == 2
    assert result["production_unchanged"] is True
    assert result["manual_review_eligible"] is False
    assert result["frozen"] is False
    assert result["persistence_status"] == "not_verified"
    assert rows == original


@pytest.mark.parametrize("field,value", [
    ("version", "future_v9"), ("version", None), ("label_version", "limit_up"),
    ("scope", "production"),
])
def test_unknown_contract_is_unavailable_not_current_probability_reconstruction(field, value):
    rows = candidates()
    rows[0]["probability_factors"]["direction_research"][field] = value
    result = p._direction_research_payload(rows)
    assert result["status"] == "unavailable"
    assert result["reason"] == "direction_research_contract_unsupported"
    assert result["candidates"] == []


@pytest.mark.parametrize("field,value", [
    ("probability", .99), ("probability_method", "unrecorded_override"),
    ("candidate_count", 3), ("eligible_count", 3), ("selected_count", 0),
    ("rank_position", 1), ("rank_limit", 30), ("eligible", 1), ("selected", 1),
])
def test_conflicting_proof_blocks_instead_of_replacing_proof_with_current_fields(field, value):
    rows = candidates()
    rows[0]["probability_factors"]["direction_research"][field] = value
    original = deepcopy(rows)
    result = p._direction_research_payload(rows)
    assert result["status"] == "blocked"
    assert result["reason"] == "direction_research_contract_mismatch"
    assert result["selected_count"] == 0 and result["candidates"] == []
    assert rows == original


def test_partial_universe_keeps_original_denominator_conflict_and_cannot_be_reranked():
    result = p._direction_research_payload(candidates()[:1])
    assert result["status"] == "blocked"
    assert result["reason"] == "direction_research_contract_mismatch"


def test_recordability_failure_exposes_count_without_unblocking_or_padding():
    rows = candidates()
    for row in rows:
        row["probability_factors"]["direction_research"].update(
            rank_contract_complete=False, error="eligible_candidates_not_recordable",
            missing_recordable_count=1, selected=False, selected_count=0, rank_position=None)
    result = p._direction_research_payload(rows)
    assert result["status"] == "blocked"
    assert result["reason"] == "eligible_candidates_not_recordable"
    assert result["missing_recordable_count"] == 1
    assert result["candidates"] == []


def cached_blocked_payload():
    rows = candidates()
    for row in rows:
        row["probability_factors"]["direction_research"].update(
            rank_contract_complete=False, error="eligible_candidates_not_recordable",
            missing_recordable_count=1, selected=False, selected_count=0, rank_position=None)
    metadata = p._direction_research_payload(rows)
    metadata.pop("missing_recordable_count")
    return {"direction_research": metadata, "ranked_first_board_candidates": rows}


def test_cached_missing_count_uses_existing_annotations_without_rank_reconstruction(monkeypatch):
    payload = cached_blocked_payload()
    original = deepcopy(payload)
    def forbidden(*args, **kwargs):
        raise AssertionError("old ranks must not be recomputed")
    monkeypatch.setattr(p, "annotate_direction_research", forbidden)
    result = p._project_promotion_candidates_payload(payload, compact=True)
    research = result["direction_research"]
    assert research["missing_recordable_count"] == 1
    assert research["recordability_count_source"] == "original_candidate_annotations"
    assert research["status"] == "blocked" and research["candidates"] == []
    assert research["frozen"] is False and research["persistence_status"] == "not_verified"
    assert payload == original


@pytest.mark.parametrize("field,value", [
    ("version", "wrong"), ("label_version", "wrong"), ("scope", "production"),
    ("rank_contract_complete", True), ("error", None),
    ("candidate_count", 3), ("eligible_count", 3),
    ("missing_recordable_count", 2), ("missing_recordable_count", None),
    ("missing_recordable_count", True), ("missing_recordable_count", 0),
])
def test_cached_conflicting_annotation_count_stays_unknown(field, value):
    payload = cached_blocked_payload()
    payload["ranked_first_board_candidates"][0]["probability_factors"]["direction_research"][field] = value
    result = p._project_promotion_candidates_payload(payload, compact=True)
    assert result["direction_research"].get("missing_recordable_count") is None
    assert result["direction_research"]["status"] == "blocked"
    assert result["direction_research"]["candidates"] == []


def test_cached_count_is_not_inferred_when_annotations_are_absent():
    payload = cached_blocked_payload()
    payload["ranked_first_board_candidates"] = []
    result = p._project_promotion_candidates_payload(payload, compact=True)
    assert result["direction_research"].get("missing_recordable_count") is None


def test_empty_directional_denominator_has_no_coverage_percentage():
    assert p._promotion_directional_metrics(0, 0, 0)["directional_coverage"] is None
    assert p._promotion_directional_metrics(2, 0, 0)["directional_coverage"] == 0
