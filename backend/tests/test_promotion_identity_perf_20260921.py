"""Explicit identity is authoritative; avoid decoding unused large JSON per access."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from app.api.v1 import promotion as p


def row(**overrides):
    values = dict(id=1, code="000001", target_board=1, prediction_trade_date=date(2026,9,18),
                  snapshot_source="schedule", snapshot_context="promotion_2000",
                  model_version="model-a", snapshot_recorded_at=datetime(2026,9,18,20),
                  snapshot_batch_key="schedule:promotion_2000:batch-a",
                  created_at=None, updated_at=None, factors_json='{"extra":"'+"x"*65536+'"}')
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("func,expected", [
    (p._prediction_record_snapshot_source, "schedule"),
    (p._prediction_record_snapshot_context, "promotion_2000"),
    (p._prediction_record_model_version, "model-a"),
])
def test_explicit_identity_does_not_decode_unused_factors(monkeypatch, func, expected):
    def unexpected(*args):
        raise AssertionError("explicit identity must not deserialize unused blob")
    monkeypatch.setattr(p, "_json_loads_safe", unexpected)
    assert func(row()) == expected


def test_latest_batch_selection_avoids_all_explicit_blob_decodes(monkeypatch):
    def unexpected(*args):
        raise AssertionError("fully explicit batch must not deserialize unused blob")
    records = [
        row(id=1, snapshot_context="promotion_1510", snapshot_recorded_at=datetime(2026,9,18,15,10)),
        row(id=2),
        row(id=3, code="000002"),
        row(id=4, snapshot_context="promotion_1305", snapshot_recorded_at=datetime(2026,9,18,13,5)),
        row(id=5, snapshot_source="page"),
        row(id=6, prediction_trade_date=date(2026,9,21), snapshot_context="promotion_1305"),
    ]
    monkeypatch.setattr(p, "_json_loads_safe", unexpected)
    assert [r.id for r in p._latest_learning_batch_records(records)] == [5, 2, 3]


@pytest.mark.parametrize("explicit", ["legacy", "", None, "  "])
def test_legacy_source_context_still_fall_back_to_original_factors(explicit):
    r = row(snapshot_source=explicit, snapshot_context=explicit, model_version=None,
            factors_json=json.dumps(dict(prediction_snapshot_source="schedule",
                prediction_snapshot_context="promotion_1510", prediction_model_version="old-model")))
    assert p._prediction_record_snapshot_source(r) == "schedule"
    assert p._prediction_record_snapshot_context(r) == "promotion_1510"
    assert p._prediction_record_model_version(r) == "old-model"


@pytest.mark.parametrize("raw", [None, "", "{bad json", "null"])
def test_explicit_identity_survives_original_broken_factors(raw):
    r = row(factors_json=raw)
    assert p._prediction_record_snapshot_source(r) == "schedule"
    assert p._prediction_record_snapshot_context(r) == "promotion_2000"
    assert p._prediction_record_model_version(r) == "model-a"


@pytest.mark.parametrize("explicit,expected", [("", "from-factors"), (None, "from-factors"),
                                             ("  ", ""), ("legacy", "legacy"), (" m ", "m")])
def test_model_version_truthiness_and_whitespace_semantics_preserved(explicit, expected):
    assert p._prediction_record_model_version(row(model_version=explicit,
        factors_json='{"prediction_model_version":"from-factors"}')) == expected


def test_supplied_legacy_factor_mapping_preserves_precedence():
    r = row(snapshot_source="legacy", snapshot_context="legacy", model_version=None,
            factors_json='{"prediction_snapshot_source":"page"}')
    supplied = dict(prediction_snapshot_source="schedule", prediction_snapshot_context="promotion_1510",
                    prediction_model_version="supplied")
    assert p._prediction_record_snapshot_source(r, supplied) == "schedule"
    assert p._prediction_record_snapshot_context(r, supplied) == "promotion_1510"
    assert p._prediction_record_model_version(r, supplied) == "supplied"
