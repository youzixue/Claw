"""Shared label contract validation; no archive/source authorization here."""
from copy import deepcopy
import pytest
from app.promotion.outcome_evidence import require_paired_material_rows
from test_promotion_outcome_materials import archive as physical_fixture_archive


def inputs():
    rows = [dict(code=code, prediction_limit_up=False, outcome_limit_up=hit,
        before_close=10., after_close=10.2, after_prev_close=10.)
        for code, hit in (("600001",True),("600002",False))]
    gate = {"profile":"fixture-profile","passed":True,"reasons":[],"evidence_hash":"a"*64,
        "per_code":[dict(r,status="verified",reasons=[],refs=["owned-fixture"]) for r in rows]}
    return rows, gate


def apply(rows, gate):
    return require_paired_material_rows(gate,profile="fixture-profile",read_time_rows=rows)


def test_sealed_rows_remain_owned_and_do_not_change_current_facts():
    rows, gate = inputs()
    original = deepcopy((rows,gate))
    result = apply(rows,gate)
    assert result["600001"]["outcome_limit_up"] is True
    assert result["600002"]["outcome_limit_up"] is False
    result["600001"]["after_close"] = 99
    assert (rows,gate) == original


@pytest.mark.parametrize("mutation", [
    lambda r,g:g.update(passed=False), lambda r,g:g.update(passed="true"),
    lambda r,g:g.update(profile="other"), lambda r,g:g.update(evidence_hash=None),
    lambda r,g:g.update(evidence_hash="A"*64), lambda r,g:g.update(reasons=["bad"]),
    lambda r,g:g["per_code"].pop(), lambda r,g:g["per_code"].append(deepcopy(g["per_code"][0])),
    lambda r,g:g["per_code"][1].update(code="600001"),
    lambda r,g:g["per_code"][1].update(code="600999"),
    lambda r,g:g["per_code"][1].update(status="unknown"),
    lambda r,g:g["per_code"][1].update(reasons=["gap"]),
    lambda r,g:g["per_code"][1].update(outcome_limit_up=0),
    lambda r,g:g["per_code"][1].update(prediction_limit_up="false"),
    lambda r,g:g["per_code"][1].update(after_close=True),
    lambda r,g:g["per_code"][1].update(after_close=float("nan")),
    lambda r,g:g["per_code"][1].update(after_close=10**400),
    lambda r,g:g["per_code"][1].update(after_prev_close=None),
    lambda r,g:g["per_code"][1].update(before_close=0),
    lambda r,g:r[1].update(outcome_limit_up=True),
    lambda r,g:r[1].update(after_close=10.200001),
    lambda r,g:r[1].update(before_close=9.9),
    lambda r,g:r[1].update(after_prev_close=10.01),
    lambda r,g:r[1].update(code="６００００２"),
    lambda r,g:r.pop(),
])
def test_unknown_mismatch_and_partial_sets_fail_closed(mutation):
    rows, gate = inputs()
    mutation(rows,gate)
    with pytest.raises(ValueError):
        apply(rows,gate)


async def load_material_fixture(db):
    """Unit-test dependency only: project fixture rows, NOT an archive producer."""
    from sqlalchemy import select
    from app.models.stock import StockKline, LimitUpPool
    bars = (await db.scalars(select(StockKline))).all()
    pools = (await db.execute(select(LimitUpPool.code, LimitUpPool.trade_date).where(
        LimitUpPool.quarantined.is_(False)))).all()
    return {"bars": {(b.code, b.trade_date): {"close": b.close, "prev_close": b.prev_close}
                     for b in bars}, "pools": set(pools)}


def outcome_ready_fixture(index, *, codes, prediction_day, outcome_day, prediction_at, evaluation_as_of):
    """Explicit fake material gate for unrelated rank/training mechanics tests."""
    import hashlib
    import json
    from app.promotion.outcome_materials import PROFILE
    per_code = []
    for code in sorted(codes):
        before = index["bars"].get((code,prediction_day), {})
        after = index["bars"].get((code,outcome_day), {})
        per_code.append({"code": code, "status": "verified", "reasons": [],
            "prediction_limit_up": (code,prediction_day) in index["pools"],
            "outcome_limit_up": (code,outcome_day) in index["pools"],
            "before_close": before.get("close"), "after_close": after.get("close"),
            "after_prev_close": after.get("prev_close"), "refs": ["synthetic-gate-not-raw-proof"]})
    result = {"passed": True, "profile": PROFILE, "reasons": [], "per_code": per_code}
    raw = json.dumps({"gate": result, "prediction_at": str(prediction_at),
                     "prediction_day": str(prediction_day), "outcome_day": str(outcome_day)}, sort_keys=True)
    result["evidence_hash"] = hashlib.sha256(raw.encode()).hexdigest()
    return result


def test_real_fixture_archive_gate_matches_parent_contract_and_label_semantics(physical_fixture_archive):
    # Reviewed test parser + real temp physical blobs, not business-ready material.
    # Registered calendar is validated separately by both DB consumers.
    from test_promotion_outcome_materials import DAYS, publish, index, gate
    from app.promotion.outcome_materials import PROFILE
    from app.promotion.labels import promotion_event_label
    for day in DAYS:
        publish(physical_fixture_archive, day)
    result = gate(index(physical_fixture_archive))
    assert result["passed"] and result["per_code"][0]["status"] == "verified"
    rows = [{"code": c, "prediction_limit_up": hit, "outcome_limit_up": hit,
             "before_close": 10., "after_close": 10., "after_prev_close": 10.}
            for c, hit in (("600001", True), ("600002", False))]
    sealed = require_paired_material_rows(result, profile=PROFILE, read_time_rows=rows)
    assert promotion_event_label(target_board=1, prediction_day_limit_up=sealed["600001"]["prediction_limit_up"],
                                 outcome_limit_up=sealed["600001"]["outcome_limit_up"]) == 0
    assert promotion_event_label(target_board=2, prediction_day_limit_up=sealed["600001"]["prediction_limit_up"],
                                 outcome_limit_up=sealed["600001"]["outcome_limit_up"]) == 1
    rows[0]["outcome_limit_up"] = False
    with pytest.raises(ValueError, match="read_time_conflict"):
        require_paired_material_rows(result, profile=PROFILE, read_time_rows=rows)


def test_no_current_rows_cannot_masquerade_as_vacuous_complete_batch():
    rows, gate = inputs()
    gate["per_code"] = []
    with pytest.raises(ValueError):
        apply([],gate)
