"""Only returned-sample diagnostics; temp SQLite/fixture HTTP, never natural certification."""
import copy
import json
from datetime import date, datetime, time, timedelta

import httpx
import pytest

from app.data.after_hours import append_observations, observation
from app.data.sources.after_hours_source import AfterHoursSource
from app.models.review import DailyReviewSnapshot
from app.review import after_hours_acceptance as checklist
from app.review.overnight_evidence import read_premarket_context
from test_dsh_overnight_evidence_20261001 import (
    local_db, _baseline, _content, _analysis, _frozen_stats, _json,
)
from test_after_hours_research_20261002 import DAY, sse

TARGET = date(2026, 10, 8)
CUTOFF = datetime(2026, 10, 8, 8)
HASH = "a" * 64


def ref(stage, identity):
    stamp = "2026-09-30T15:01:00" if stage == "regular_close" else "2026-09-30T15:35:00"
    return {"id": identity, "stage": stage, "quality_status": "observed", "integrity_verified": True,
        "source": "tencent_close" if stage == "regular_close" else "sse_fixed_price",
        "source_version": "fixture_v1", "content_hash": HASH,
        "source_quote_at": stamp, "received_at": stamp, "recorded_at": stamp, "available_at": stamp}


def context():
    post = {"status": "available", "review_date": "2026-09-30", "analysis_trade_date": "2026-09-30",
        "snapshot_id": 1, "payload_hash": HASH, "as_of_at": "2026-09-30T20:35:00",
        "created_at": "2026-09-30T20:35:01", "data_version": "d", "schema_version": "s",
        "quality_status": "partial"}
    item = {"code": "600000", "observation_ids": [1, 2],
        "source_refs": [ref("regular_close", 1), ref("after_hours", 2)],
        "status": "observed", "missing": [], "close_price_1500": 9.48,
        "regular_volume_shares": 10000, "after_volume_shares": 100,
        "after_amount_yuan": 948, "all_day_volume_shares": 10100, "all_day_amount_yuan": 95748,
        "ratio_basis": "observed_official_daily_total_after_closed_session"}
    return {"trade_date": TARGET.isoformat(), "as_of_at": CUTOFF.isoformat(),
        "timezone": "Asia/Shanghai", "clock_convention": "naive",
        "calendar": {"target_status": "trade_day", "expected_previous": "2026-09-30",
                     "coverage": {"complete": True}},
        "freshness": {"alignment": {key: "matched" for key in ("kline", "trusted_kline_anchor", "postmarket")}},
        "previous_postmarket": post,
        "research_fusion": {"recomputed_previous_day_from_current_spot": False,
            "automatic_weight_update": False, "execution_authorized": False,
            "statistics": {"dimensions": {key: {"status": "available", "snapshot_id": 1,
                "payload_hash": HASH} for key in ("fundamental", "technical", "capital")}}},
        "after_hours": {"trade_date": "2026-09-30", "as_of_at": CUTOFF.isoformat(),
            "items": [item], "returned_codes": 1, "stored_code_count": 1, "truncated": False},
        "news": {"status": "available", "as_of_at": CUTOFF.isoformat(),
            "window_start": "2026-09-30T15:00:00", "items": [], "count": 0,
            "coverage": {"known_zero_news": True, "count_complete": True}}}


def visible_news():
    stamp = "2026-10-01T09:00:00"
    return {"origin": "observed", "content_version_id": 1, "content_hash": HASH,
        **{key: stamp for key in ("publish_time", "first_received_at", "received_at",
            "recorded_at", "content_available_at", "entity_verified_at", "available_at")},
        "analysis_version_id": None}


def check(value):
    return checklist.summarize_stored_acceptance(value)


def assert_not_certified(result):
    for key in ("natural_acceptance_verified", "scheduled_job_execution_certified",
        "recheck_run_receipt_certified", "first_source_availability_certified",
        "individual_input_availability_certified", "historical_pit_certified",
        "complete_market_coverage", "all_news_coverage_certified",
        "automatic_weight_update", "execution_authorized"):
        assert result[key] is False
    assert result["runtime_loaded_version"] is None


@pytest.mark.parametrize("source", ["tencent_after_hours", "sina_after_hours"])
def test_supplier_pairs_are_observations_not_official_certification(source):
    from app.data.after_hours import SUPPLIER_VERSIONS
    data = context()
    item = data["after_hours"]["items"][0]
    item["ratio_basis"] = "observed_supplier_native_daily_total_after_session"
    item["source_refs"][1].update(source=source, source_version=SUPPLIER_VERSIONS[source])
    result = check(data)
    assert result["status"] == "ready_for_operator_review"
    assert result["sample_stage_pair_count"] == 0
    assert result["sample_supplier_pair_count"] == 1
    assert result["checks"]["same_day_baseline_and_official_closed_pairs"] is False
    assert result["source_class_checks"]["all_returned_pairs_supplier"] is True
    assert_not_certified(result)
    item["source_refs"][1]["source_version"] = "unknown"
    assert check(data)["sample_supplier_pair_count"] == 0
    assert check(data)["status"] == "partial"


def test_complete_sample_only_opens_operator_review_not_certification_or_jobs():
    value = context()
    value["natural_acceptance_verified"] = value["execution_authorized"] = True
    original = copy.deepcopy(value)
    result = check(value)
    assert result["status"] == "ready_for_operator_review"
    assert all(result["checks"].values())
    assert result["sample_code_count"] == result["sample_stage_pair_count"] == 1
    assert "20:10_recheck" in result["scheduled_slots_not_proved"]
    assert_not_certified(result)
    assert value == original


@pytest.mark.parametrize("path,value", [
    (("as_of_at",), "2026-10-08T08:01:00"), (("as_of_at",), "2026-10-08T07:59:59"),
    (("as_of_at",), "2026-10-08T08:00:00+08:00"), (("timezone",), "UTC"),
    (("calendar", "expected_previous"), "2026-10-08"),
    (("calendar", "coverage", "complete"), False), (("calendar", "target_status"), "closed"),
    (("freshness", "alignment", "postmarket"), "stale"),
    (("previous_postmarket", "as_of_at"), "2026-09-30T20:34:59"),
    (("previous_postmarket", "created_at"), "2026-10-08T08:00:01"),
    (("previous_postmarket", "payload_hash"), "unknown"),
    (("previous_postmarket", "data_version"), " "),
    (("previous_postmarket", "snapshot_id"), True),
    (("research_fusion", "statistics", "dimensions", "fundamental", "status"), "unavailable"),
    (("research_fusion", "statistics", "dimensions", "technical", "snapshot_id"), 2),
    (("research_fusion", "recomputed_previous_day_from_current_spot"), True),
    (("after_hours", "truncated"), True), (("after_hours", "stored_code_count"), 2),
    (("after_hours", "returned_codes"), True),
    (("news", "coverage", "known_zero_news"), False), (("news", "coverage", "count_complete"), False),
])
def test_missing_stale_or_late_sample_never_passes(path, value):
    data = context()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    result = check(data)
    assert result["recorded_sample_checks_passed"] is False
    assert result["missing_checks"]
    assert_not_certified(result)


@pytest.mark.parametrize("field,value", [
    ("stage", "regular_close"), ("quality_status", "partial"), ("integrity_verified", False),
    ("id", True), ("source", "unknown"), ("source_version", "v\x00"),
    ("source_quote_at", "2026-09-30T15:29:59"), ("received_at", "2026-09-30T15:00:00"),
    ("recorded_at", "2026-10-01T00:00:00"), ("available_at", "2026-10-08T08:00:01"),
])
def test_reference_missing_integrity_or_clocks_never_measured_pair(field, value):
    data = context()
    data["after_hours"]["items"][0]["source_refs"][1][field] = value
    result = check(data)
    assert result["sample_stage_pair_count"] == 0
    assert not result["recorded_sample_checks_passed"]


@pytest.mark.parametrize("field,value", [
    ("after_volume_shares", None), ("after_volume_shares", False), ("after_volume_shares", 1.5),
    ("after_amount_yuan", None), ("regular_volume_shares", 0), ("close_price_1500", float("inf")),
    ("all_day_volume_shares", 50), ("all_day_volume_shares", 100),
    ("all_day_volume_shares", 10099), ("ratio_basis", "unknown"), ("missing", ["missing_clock"]),
    ("observation_ids", [2, 1]), ("code", "６０００００"),
])
def test_required_sample_facts_and_identity_not_missing_defaults(field, value):
    data = context()
    data["after_hours"]["items"][0][field] = value
    result = check(data)
    assert not result["recorded_sample_checks_passed"]
    assert_not_certified(result)


@pytest.mark.parametrize("count", [0, -1, True, 0.5])
def test_nonempty_news_cannot_claim_zero_invalid_or_too_small_count(count):
    data = context()
    data["news"].update(items=[visible_news()], count=count)
    assert not check(data)["recorded_sample_checks_passed"]


def test_same_observation_rows_cannot_belong_to_two_sample_codes():
    data = context()
    duplicate = copy.deepcopy(data["after_hours"]["items"][0])
    duplicate["code"] = "600001"
    data["after_hours"]["items"].append(duplicate)
    data["after_hours"].update(returned_codes=2, stored_code_count=2)
    assert not check(data)["recorded_sample_checks_passed"]


def test_explicit_official_zero_is_not_unknown_or_empty_sample():
    data = context()
    data["after_hours"]["items"][0].update(after_volume_shares=0, after_amount_yuan=0)
    assert check(data)["recorded_sample_checks_passed"]
    data["after_hours"]["items"] = []
    data["after_hours"].update(returned_codes=0, stored_code_count=0)
    assert not check(data)["recorded_sample_checks_passed"]


@pytest.mark.parametrize("field,value", [
    ("origin", "legacy_unknown"), ("first_received_at", None), ("content_hash", "bad"),
    ("available_at", "2026-10-08T08:00:01"), ("recorded_at", "2026-10-01T09:00:01"),
    ("analysis_version_id", True),
])
def test_news_missing_late_or_noncausal_is_not_visible_sample(field, value):
    data = context()
    item = visible_news()
    item[field] = value
    data["news"].update(items=[item], count=1)
    assert not check(data)["recorded_sample_checks_passed"]


def test_unanalyzed_visible_news_is_valid_and_future_analysis_is_not():
    data = context()
    item = visible_news()
    data["news"].update(items=[item], count=1)
    assert check(data)["recorded_sample_checks_passed"]
    item.update(analysis_version_id=2, analysis_completed_at="2026-10-08T08:00:01",
        analysis_available_at="2026-10-08T08:00:01", analysis_result_hash=HASH)
    assert not check(data)["recorded_sample_checks_passed"]


@pytest.mark.parametrize("bad_id", [True, 1.0])
@pytest.mark.parametrize("location", ["dimension", "observations"])
def test_linked_ids_require_integer_not_equal_value_alias(bad_id, location):
    data = context()
    if location == "dimension":
        data["research_fusion"]["statistics"]["dimensions"]["fundamental"]["snapshot_id"] = bad_id
    else:
        data["after_hours"]["items"][0]["observation_ids"][0] = bad_id
    assert not check(data)["recorded_sample_checks_passed"]


def test_complete_clock_retains_recorded_microsecond_precision():
    data = context()
    data["after_hours"]["items"][0]["source_refs"][1].update(
        source_quote_at="2026-09-30T15:35:00.000001",
        received_at="2026-09-30T15:35:00.000002",
        recorded_at="2026-09-30T15:35:00.000003",
        available_at="2026-09-30T15:35:00.000003")
    assert check(data)["recorded_sample_checks_passed"]


def test_date_without_clock_does_not_invent_midnight_visibility():
    data = context()
    item = visible_news()
    item["first_received_at"] = "2026-10-01"
    data["news"].update(items=[item], count=1)
    assert not check(data)["recorded_sample_checks_passed"]


def test_input_budget_utf8_exact_plus_one_and_limits(monkeypatch):
    data = context()
    data["unused"] = "汉" * 100
    size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    monkeypatch.setattr(checklist, "MAX_INPUT_BYTES", size)
    assert check(data)["recorded_sample_checks_passed"]
    monkeypatch.setattr(checklist, "MAX_INPUT_BYTES", size - 1)
    assert check(data)["status"] == "unavailable"
    monkeypatch.setattr(checklist, "MAX_INPUT_BYTES", size + 1)
    monkeypatch.setattr(checklist, "MAX_CODES", 0)
    assert check(data)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_readonly_premarket_holiday_frozen_observations_and_pit_news(local_db):
    env = local_db
    # Past fixture dates avoid falsely sampling a future natural session; the
    # stored calendar declares Monday closed, so the prior is Friday, not target-1.
    previous, target, cutoff = date(2026, 9, 4), date(2026, 9, 8), datetime(2026, 9, 8, 8)
    async with env.maker() as db:
        await _baseline(db, previous=previous, target=target, closed=[date(2026, 9, 7)])
        frozen = datetime.combine(previous, time(20, 35))
        db.add(DailyReviewSnapshot(review_key="acceptance-latest", review_date=previous,
            analysis_trade_date=previous, phase="postmarket", as_of_at=frozen, created_at=frozen,
            data_version="fixture-v1", schema_version="fixture", quality_status="partial",
            payload_json=_json(_frozen_stats(previous=previous))))
        await append_observations(db, [observation(code="600000", day=previous, stage="regular_close",
            source="tencent_close", source_version="fixture_v1",
            received_at=datetime.combine(previous, time(15, 1)), accepted_at=datetime.combine(previous, time(15, 1)),
            source_quote_at=datetime.combine(previous, time(15)),
            values={"close_price_1500": 9.48, "regular_volume_shares": 10000})])
        async def fixture(_request):
            return httpx.Response(200, json=sse(date=20260904))
        # Real bounded adapter over fixture HTTP, never calls external network.
        source = AfterHoursSource()
        source_now = datetime.combine(previous, time(16, 30))
        import app.data.sources.after_hours_source as adapter
        from unittest.mock import patch
        with patch.object(adapter, "local_now", return_value=source_now):
            async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
                material = await source.collect("600000", trade_date=previous, client=client)
        row = observation(code="600000", day=previous, stage="after_hours",
            accepted_at=source_now, **material)
        await append_observations(db, [row])
        # Same-content 20:10 recheck deduplicates; no job-run receipt is invented.
        await append_observations(db, [observation(code="600000", day=previous, stage="after_hours",
            accepted_at=datetime.combine(previous, time(20, 10)),
            **{**material, "received_at": datetime.combine(previous, time(20, 10))})])
        _, content = await _content(db, publish=datetime(2026, 9, 5, 9),
            received=datetime(2026, 9, 5, 9, 1), available=datetime(2026, 9, 5, 9, 1),
            recorded=datetime(2026, 9, 5, 9, 1), source_id="holiday-visible")
        await _analysis(db, content, completed=datetime(2026, 9, 5, 9, 2),
            available=datetime(2026, 9, 5, 9, 2))
        await _content(db, publish=datetime(2026, 9, 8, 7), received=cutoff+timedelta(seconds=1),
            available=cutoff+timedelta(seconds=1), recorded=cutoff+timedelta(seconds=1),
            source_id="late-at-target")
        await db.commit()
    async with env.read_maker() as db:
        result = await read_premarket_context(db, trade_date=target, as_of=cutoff, limit=100)
    diag = result["stored_evidence_acceptance"]
    assert diag["recorded_sample_checks_passed"], diag
    assert diag["status"] == "ready_for_operator_review"
    assert result["news"]["count"] == 1
    assert len(result["after_hours"]["items"][0]["source_refs"]) == 2
    assert result["after_hours"]["items"][0]["source_refs"][1]["recorded_at"] == source_now.isoformat()
    assert_not_certified(diag)
    assert not result["provenance"]["business_db_write"]
    assert all(s.lstrip().upper().startswith("SELECT") for s in env.statements)
