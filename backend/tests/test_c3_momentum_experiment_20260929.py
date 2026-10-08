"""C3 isolated confirmation-window hypothesis; no scanner/order/push hook."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.paper.intraday_route_research import (
    RouteResearchPolicy, evaluate_c3_confirmation_momentum_experiment,
)

START = datetime(2026, 9, 29, 10, 0)
ANCHOR = START + timedelta(seconds=60)
POLICY = RouteResearchPolicy("research:c3_original_window", 3, 60, 75, 90, 1)


def fixture():
    c = dict(candidate_id="event:1", route="C3", account_id=None, account_name=None,
        code="600001", production_version="c3:v2", trade_date="2026-09-29",
        frozen_at=ANCHOR.isoformat(), context_start_at=START.isoformat(),
        original_candidate=True, original_confirmed=True,
        original_confirmed_at=ANCHOR.isoformat(), evidence_ref="confirmed:1",
        gate_ref="gate:1", gate_inputs={"min_relative_strength_pct":0.5},
        reference_price=10.2, sector_code="sector:1",
        confirmation_window=dict(first_sample_at=START.isoformat(),
            last_sample_at=ANCHOR.isoformat(), sample_count=3, source_sample_count=3,
            min_samples=3, min_persistence_sec=60, max_sample_gap_sec=75))
    rows=[]
    for i, price in enumerate((10.1,10.15,10.2)):
        at=(START+timedelta(seconds=i*30)).isoformat()
        rows.append(dict(candidate_id="event:1", sample_id=f"s{i}", evidence_ref=f"frame:{i}",
            account_id=None, account_name=None, code="600001", production_version="c3:v2",
            source_quote_at=at, received_at=at, observed_at=at, price=price, prev_close=10.0,
            avg_price=10.05, ask1_price=price, ask1_volume=100, original_gate=True,
            original_gate_ref=f"gate:{i}", sector_code="sector:1",
            sector_relative_strength_pct=0.8+i*0.2))
    return c, rows


def evaluate(c, rows, **kw):
    return evaluate_c3_confirmation_momentum_experiment(c, rows, as_of=kw.pop("as_of",ANCHOR),
                                                       policy=POLICY, **kw)


def test_rising_price_nondeclining_relative_can_pass_with_flat_vwap():
    c, rows=fixture()
    before=deepcopy((c,rows))
    out=evaluate(c,rows)
    assert out["momentum_candidate"]["value"] is True
    assert out["candidate"]["value"] is False  # old positive-VWAP comparator retained
    assert out["baseline"]["value"] is True
    assert out["production_permission"] is False
    assert out["orders_created"] == out["pushes_created"] == 0
    assert out["momentum_candidate"]["is_buy_point"] is False
    assert (c,rows)==before


@pytest.mark.parametrize("prices,relative,expected",[
    ((10.2,10.2,10.2),(1,1,1),False),
    ((10.3,10.2,10.2),(1,1,1),False),
    ((10.1,10.15,10.2),(1.2,1.1,1.0),False),
    ((10.1,10.15,10.2),(1,1,1),True),
])
def test_fixed_zero_threshold_no_winner_tuning(prices,relative,expected):
    c,rows=fixture()
    for i,r in enumerate(rows):
        r.update(price=prices[i],ask1_price=prices[i],sector_relative_strength_pct=relative[i])
    assert evaluate(c,rows)["momentum_candidate"]["value"] is expected


@pytest.mark.parametrize("field,value",[
    ("sector_code",None),("sector_code","different"),("sector_relative_strength_pct",None),
    ("sector_relative_strength_pct",float("nan")),("sector_relative_strength_pct",True),
    ("code","600002"),("production_version","other"),("account_id",3),
    ("prev_close",9.9),("source_quote_at",None),("original_gate",None),
    ("original_gate",False),("ask1_volume",None),("ask1_volume",0),
])
def test_incomplete_or_conflicting_original_window_unknown(field,value):
    c,rows=fixture()
    rows[0][field]=value
    out=evaluate(c,rows)
    assert out["momentum_candidate"]["value"] is None


def test_missing_middle_or_endpoint_not_shorter_window():
    c,rows=fixture()
    for subset in (rows[1:],rows[:-1],[rows[0],rows[-1]],[]):
        assert evaluate(c,subset)["momentum_candidate"]["value"] is None


def test_duplicate_cannot_replace_missing_middle():
    c,rows=fixture()
    assert evaluate(c,[rows[0],dict(rows[0],sample_id="copy"),rows[-1]])["momentum_candidate"]["value"] is None


def test_future_and_post_anchor_frames_do_not_change_any_result():
    c,rows=fixture()
    future=dict(rows[-1],sample_id="later",observed_at=(ANCHOR+timedelta(seconds=30)).isoformat(),
                received_at=(ANCHOR+timedelta(seconds=30)).isoformat(),
                source_quote_at=(ANCHOR+timedelta(seconds=30)).isoformat(),price=20.0)
    assert evaluate(c,rows)==evaluate(c,rows+[future],as_of=ANCHOR+timedelta(minutes=5))
    future_predicate=dict(rows[-1],sample_id="later-predicate",
                         predicate_observed_at=(ANCHOR+timedelta(seconds=1)).isoformat())
    assert evaluate(c,rows)==evaluate(c,rows+[future_predicate])


def test_anchor_future_not_usable():
    c,rows=fixture()
    assert evaluate(c,rows,as_of=ANCHOR-timedelta(seconds=1))["momentum_candidate"]["value"] is None


@pytest.mark.parametrize("mutator",[
    lambda c,r: c.update(route="C2",account_id=9,account_name="challenger_c"),
    lambda c,r: c.update(reference_price=10.3),
    lambda c,r: c.update(original_confirmed=False),
    lambda c,r: c["confirmation_window"].update(min_persistence_sec=30),
    lambda c,r: c["confirmation_window"].update(sample_count=4),
    lambda c,r: c["confirmation_window"].update(source_sample_count=2),
    lambda c,r: c["confirmation_window"].update(first_sample_at=(START-timedelta(seconds=1)).isoformat()),
    lambda c,r: r[1].update(committed_at=(ANCHOR+timedelta(seconds=1)).isoformat()),
    lambda c,r: r[1].update(original_gate_ref=None),
])
def test_confirmation_binding_and_original_rules_required(mutator):
    c,rows=fixture()
    mutator(c,rows)
    assert evaluate(c,rows)["momentum_candidate"]["value"] is None


def test_outcome_labels_never_used():
    c,rows=fixture()
    old=evaluate(c,rows)
    c.update(close=1.0,return_pct=-90,notification_status="sent")
    for r in rows: r.update(close=100,return_pct=1000)
    assert evaluate(c,rows)["momentum_candidate"]==old["momentum_candidate"]
