"""The producer must reject bad input before defaults/clamps erase missingness."""
from copy import deepcopy

import pytest

from app.api.v1 import promotion
from app.promotion.versioning import ProbabilityContractError

INVALID = [None, "", "0.5", True, False, float("nan"), float("inf"),
           -float("inf"), -0.01, 1.01, [], {}, 10**1000]


def candidate():
    return {
        "code": "000888", "target_board": 2, "candidate_route": "second_board_promotion",
        "probability": 0.5, "probability_factors": {},
    }


@pytest.mark.parametrize("value", INVALID)
def test_learning_rejects_invalid_original_before_defaulting(value):
    row = candidate()
    row["probability"] = value
    with pytest.raises(ProbabilityContractError, match="probability:"):
        promotion._apply_promotion_learning_to_candidates([row], {})


def test_learning_rejects_missing_original():
    row = candidate()
    del row["probability"]
    with pytest.raises(ProbabilityContractError, match="probability:"):
        promotion._apply_promotion_learning_to_candidates([row], {})


@pytest.mark.parametrize("field", ["empirical_probability", "avg_predicted_probability"])
@pytest.mark.parametrize("value", INVALID)
def test_learning_does_not_substitute_invalid_supplied_statistics(field, value):
    row = candidate()
    bucket = promotion._promotion_learning_bucket(2, row["candidate_route"])
    with pytest.raises(ProbabilityContractError, match=f"{field}:"):
        promotion._apply_promotion_learning_to_candidates([row], {
            bucket: {"sample_count": 12, "success_count": 3, field: value},
        })


@pytest.mark.parametrize("value", [0, 1, 0.5])
def test_learning_preserves_valid_original_and_does_not_mutate_candidates(value):
    row = candidate()
    row["probability"] = value
    before = deepcopy(row)
    result = promotion._apply_promotion_learning_to_candidates([row], {})[0]
    assert row == before
    assert result["raw_probability"] == value
    # Existing Bayesian/logit smoothing is unchanged; raw zero is not missing.
    assert 0 <= result["probability"] <= 1


@pytest.mark.asyncio
async def test_empty_schedule_adapter_returns_unproven_not_successful_ledger():
    from datetime import date

    result = await promotion._record_promotion_predictions(
        None, [], {1: date(2026, 9, 11)}, snapshot_source="schedule", return_details=True,
    )
    payload = result.as_payload()
    assert payload["status"] == "empty_unproven"
    assert payload["ledger_recorded"] is False
    assert payload["input_count"] == payload["touched"] == 0
    assert result.ledger is None
