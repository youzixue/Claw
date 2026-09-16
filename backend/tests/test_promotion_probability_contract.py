"""Probability contract guards; all DB access uses pytest's isolated temp DB."""
from copy import deepcopy
from datetime import date
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.signal import PromotionPredictionRecord
from app.models.promotion import PromotionPredictionSnapshot
from app.promotion.persistence import record_promotion_predictions
from app.promotion.versioning import (
    PROBABILITY_CONTRACT_VERSION,
    LEGACY_PROBABILITY_CONTRACT_VERSION,
    ProbabilityContractError,
    get_promotion_model_identity,
    resolve_promotion_probability,
)

FIELDS = ("p_raw", "p_calibrated", "production_probability")
INVALID = [None, "", "0.8", "NaN", float("nan"), float("inf"),
           -float("inf"), True, False, -0.01, 1.01, {}, [], 10**1000]


def candidate():
    return {
        "code": "000001", "target_board": 1, "candidate_route": "news_catalyst_start",
        "probability_contract_version": PROBABILITY_CONTRACT_VERSION,
        "p_raw": 0.4, "p_calibrated": 0.2, "production_probability": 0.0,
        # Conflicting legacy aliases must never substitute for versioned fields.
        "raw_probability": 0.99, "probability": 0.98,
        "probability_factors": {
            "historical_evidence": {"keep": 1},
            "prediction_snapshot_source": "schedule",
            "prediction_snapshot_context": "promotion_2000",
            "prediction_snapshot_recorded_at": "2026-09-11T20:00:00",
            "prediction_snapshot_batch_key": "contract-test",
        },
    }


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", INVALID)
def test_versioned_invalid_never_falls_back(field, value):
    item = candidate()
    item[field] = value
    with pytest.raises(ProbabilityContractError):
        resolve_promotion_probability(item, allow_legacy=True)


@pytest.mark.parametrize("field", FIELDS)
def test_versioned_missing_never_falls_back(field):
    item = candidate()
    del item[field]
    with pytest.raises(ProbabilityContractError):
        resolve_promotion_probability(item, allow_legacy=True)


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, 0.25])
def test_valid_boundaries(value):
    item = candidate()
    item.update(dict.fromkeys(FIELDS, value))
    result = resolve_promotion_probability(item)
    assert all(result.as_payload()[field] == value for field in FIELDS)


@pytest.mark.parametrize("version", [None, "", "unknown", True])
def test_unknown_version_is_not_legacy(version):
    item = candidate()
    item["probability_contract_version"] = version
    with pytest.raises(ProbabilityContractError):
        resolve_promotion_probability(item, allow_legacy=True)


def test_unmarked_new_fields_are_not_legacy():
    item = candidate()
    del item["probability_contract_version"]
    with pytest.raises(ProbabilityContractError):
        resolve_promotion_probability(item, allow_legacy=True)


def test_legacy_requires_explicit_opt_in_and_preserves_zero():
    item = {"raw_probability": 0.0, "probability": 0.25}
    with pytest.raises(ProbabilityContractError):
        resolve_promotion_probability(item)
    result = resolve_promotion_probability(item, allow_legacy=True)
    assert result.contract_version == LEGACY_PROBABILITY_CONTRACT_VERSION
    assert result.p_raw == 0
    assert result.production_probability == 0.25
    assert resolve_promotion_probability({"probability": 0}, allow_legacy=True).p_raw == 0
    explicit = {**item, "probability_contract_version": LEGACY_PROBABILITY_CONTRACT_VERSION}
    assert resolve_promotion_probability(explicit, allow_legacy=True) == result


@pytest.mark.parametrize("field", ["probability", "raw_probability"])
@pytest.mark.parametrize("value", INVALID)
def test_legacy_invalid_is_not_defaulted(field, value):
    item = {"probability": 0.9, "raw_probability": 0.9, field: value}
    with pytest.raises(ProbabilityContractError):
        resolve_promotion_probability(item, allow_legacy=True)


async def persist(db, items, *, is_recordable=lambda item: True):
    identity = get_promotion_model_identity()
    return await record_promotion_predictions(
        db, items, {1: date(2026, 9, 11)}, snapshot_source="schedule",
        model_version=identity.active_model_version, model_identity=identity,
        is_recordable=is_recordable, reason_builder=lambda item: {},
        learning_bucket_builder=lambda target, route: "test",
    )


@pytest.mark.asyncio
async def test_invalid_batch_rejected_before_any_db_access_or_filter():
    item = candidate()
    item["production_probability"] = None
    # None DB proves even storage creation is forbidden before validation.
    with pytest.raises(ProbabilityContractError):
        await persist(None, [candidate(), item], is_recordable=lambda item: False)


@pytest.mark.asyncio
async def test_storage_projects_production_and_freezes_all_three_without_mutating_input(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            item = candidate()
            original = deepcopy(item)
            result = await persist(db, [item])
            assert result.touched == 1
            assert result.status == "recorded"
            assert result.as_payload()["ledger_recorded"] is True
            assert result.input_count == result.recordable_count == result.prepared_count == 1
            assert item == original
            row = await db.scalar(select(PromotionPredictionRecord))
            snapshot = await db.scalar(select(PromotionPredictionSnapshot))
            assert row.predicted_probability == snapshot.raw_probability == 0.4
            assert row.calibrated_probability == snapshot.calibrated_probability == 0.0
            evidence = json.loads(row.factors_json)
            assert evidence["historical_evidence"] == {"keep": 1}
            assert evidence["probability_contract"] == resolve_promotion_probability(item).as_payload()
            # Invalid retry cannot delete the previous pending compatibility row.
            item["p_raw"] = False
            with pytest.raises(ProbabilityContractError):
                await persist(db, [item])
            assert await db.scalar(select(PromotionPredictionRecord)) is row
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_batch_is_unproven_not_a_completed_zero_pool():
    result = await persist(None, [])
    assert result.touched == 0
    assert result.ledger is None
    assert result.status == "empty_unproven"
    assert result.as_payload() == {
        "status": "empty_unproven",
        "reason": "No candidates supplied; empty universe is not proven and no run was appended.",
        "input_count": 0, "recordable_count": 0, "prepared_count": 0,
        "filtered_count": 0, "invalid_identity_count": 0,
        "touched": 0, "ledger_recorded": False, "ledger": None,
    }


@pytest.mark.asyncio
async def test_fully_filtered_batch_has_distinct_status_without_db_access():
    result = await persist(None, [candidate(), candidate()], is_recordable=lambda item: False)
    assert result.status == "fully_filtered"
    assert result.input_count == 2
    assert result.recordable_count == result.prepared_count == 0
    assert result.as_payload()["filtered_count"] == 2
    assert result.as_payload()["ledger_recorded"] is False
    assert result.touched == 0 and result.ledger is None
    assert result.reason


@pytest.mark.asyncio
async def test_missing_identity_has_distinct_status_without_db_access():
    item = candidate()
    del item["code"]
    result = await persist(None, [item])
    assert result.status == "invalid_candidate_identity"
    assert result.input_count == result.recordable_count == 1
    assert result.prepared_count == 0
    assert result.as_payload()["filtered_count"] == 0
    assert result.as_payload()["invalid_identity_count"] == 1
    assert result.as_payload()["ledger_recorded"] is False
