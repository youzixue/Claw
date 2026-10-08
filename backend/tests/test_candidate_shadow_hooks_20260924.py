"""Observational producer/lifecycle wiring; temporary fixtures, no runtime I/O."""
import ast
import asyncio
import copy
import inspect
import sys
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace

import pytest

from app.api.v1 import paper
from app.config.settings import settings
from app.data.scheduler import DataScheduler

PRIMARY = {"default": "A", "promotion": "B", "mainline": "C", "auction": "D",
           "tenbagger": "E", "challenger_e": "E2", "reversal": "F"}


@pytest.fixture
def sink(monkeypatch):
    module = ModuleType("app.paper.candidate_shadow")
    rows = []
    module.capture_frame = lambda packet: rows.append(copy.deepcopy(packet))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(settings, "PAPER_CANDIDATE_SHADOW_ENABLED", True)
    return module, rows


@pytest.mark.parametrize("account_name,route", PRIMARY.items())
def test_primary_projection_does_not_mutate_source(account_name, route, sink):
    _, rows = sink
    account = SimpleNamespace(account_name=account_name, id=3)
    item = {"code": "600001", "name": "test", "_source": "next_day_plan",
            "nested": {"not": "a copied business object"}}
    quote = SimpleNamespace(code="600001", price=10., prev_close=9.9,
                            ask1_price=10., ask1_volume=20,
                            source_quote_at=datetime(2026, 9, 24, 10))
    old = copy.deepcopy((vars(account), item, vars(quote)))
    paper._capture_primary_candidate_shadow(
        account=account, candidate=item, spot=quote, run_id="run",
        stage="candidate_quote_observed", original_candidate=True,
        reported_at=datetime(2026, 9, 24, 10))
    assert len(rows) == 1 and rows[0]["route"] == route
    assert rows[0]["original_confirmed"] is None and rows[0]["original_gate"] is None
    assert rows[0]["quote"]["ask1_volume"] == 20
    assert rows[0]["source_persistence"] == "producer_observed_not_commit_receipt"
    assert "nested" not in rows[0]["gate_inputs"]
    assert (vars(account), item, vars(quote)) == old


@pytest.mark.parametrize("name", ("shared_50k", "challenger_a", "challenger_b",
                                  "challenger_c", "challenger_d", "challenger_f2", "unknown"))
def test_nonprimary_accounts_not_reinterpreted(name, sink):
    paper._capture_primary_candidate_shadow(account=SimpleNamespace(account_name=name, id=4))
    assert sink[1] == []


def test_quote_confirmation_not_promoted(sink):
    paper._capture_primary_candidate_shadow(
        account=SimpleNamespace(account_name="default", id=2),
        candidate={"code": "600001", "confirmation_sample_count": 3},
        stage="quote_path_ready", original_candidate=True)
    assert sink[1][0]["original_confirmed"] is None


def test_formal_confirmation_is_explicit(sink):
    paper._capture_primary_candidate_shadow(
        account=SimpleNamespace(account_name="default", id=2),
        candidate={"code": "600001"}, stage="strategy_confirmed",
        original_candidate=True, original_confirmed=True, original_gate=True)
    assert sink[1][0]["original_confirmed"] is True
    assert sink[1][0]["original_gate"] is True


@pytest.mark.parametrize("conditional,expected", ((True, True), (False, None), (None, None)))
def test_mainline_actionable_is_not_causal_identity(conditional, expected, sink):
    paper._capture_primary_candidate_shadow(
        account=SimpleNamespace(account_name="mainline", id=4),
        candidate={"code": "600001", "actionable": True,
                   "conditional_mainline_confirmation": conditional},
        original_candidate=True)
    assert sink[1][0]["identities"]["mainline_identity"] is expected


def test_hook_failures_do_not_escape(sink):
    def failed(*args, **kwargs):
        raise OSError("injected unavailable")
    sink[0].capture_frame = failed
    paper._capture_primary_candidate_shadow(
        account=SimpleNamespace(account_name="default", id=2),
        candidate={"code": "600001"}, original_candidate=True)


def test_disabled_hook_never_touches_account(monkeypatch, sink):
    monkeypatch.setattr(settings, "PAPER_CANDIDATE_SHADOW_ENABLED", False)
    paper._capture_primary_candidate_shadow(account=object())
    assert sink[1] == []


def test_same_quote_handoff_does_not_replace_trade_payload(monkeypatch, sink):
    module, _ = sink
    captured = []
    module.capture_quotes = lambda records, **kwargs: captured.append((records, kwargs))
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_enqueue_momentum_quote_round", lambda p: None)
    payload = {"round_id": "same-round", "records": [{"code": "600001", "price": 10.}]}
    scheduler._publish_quote_round(payload)
    assert scheduler._quote_round_payload is payload
    assert scheduler._quote_round_event.is_set()
    assert captured[0][0] is payload["records"]
    assert captured[0][1]["round_id"] == "same-round"


def test_quote_handoff_error_does_not_block_trade(monkeypatch, sink):
    def fail(*a, **k):
        raise RuntimeError("injected research failure")
    sink[0].capture_quotes = fail
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_enqueue_momentum_quote_round", lambda p: None)
    payload = {"round_id": "trade", "records": []}
    scheduler._publish_quote_round(payload)
    assert scheduler._quote_round_payload is payload and scheduler._quote_round_event.is_set()


@pytest.mark.asyncio
async def test_report_only_uses_file_reader(sink):
    seen = []
    sink[0].read_candidate_shadow_report = lambda **kwargs: seen.append(kwargs) or {
        "rows": [], "records": [{"sample_inputs": ["raw immutable evidence"]}],
        "coverage": {"recent_receipts": [{"stage": "not_scanned", "reason": "missing source"}]},
    }
    result = await paper.paper_candidate_shadow_report(
        trade_date=datetime(2026, 9, 24).date(), route="A", limit=20)
    assert result == {"rows": [], "raw_evidence_omitted": True,
                      "coverage": {"recent_receipts": [{"stage": "not_scanned", "reason": "missing source"}]}}
    assert seen == [{"trade_date": datetime(2026, 9, 24).date(), "route": "A", "limit": 20}]
    assert "db" not in inspect.signature(paper.paper_candidate_shadow_report).parameters


def test_formal_hook_stays_before_notification_and_budget():
    source = inspect.getsource(paper.run_paper_auto_trade)
    marker = 'stage="strategy_confirmed"'
    assert source.index("continuation_reason =") < source.index(marker)
    assert source.index(marker) < source.index("await record_buy_point(")
    assert source.index(marker) < source.index("pause_reason =")
    assert "original_confirmed=None" in source  # quote-only waits never made formal


def test_feature_flag_does_not_rotate_execution_versions(monkeypatch):
    from app.paper.experiment import EXPERIMENT_ACCOUNTS
    monkeypatch.setattr(settings, "PAPER_CANDIDATE_SHADOW_ENABLED", False)
    before = {name: paper._strategy_version(name) for name in EXPERIMENT_ACCOUNTS}
    monkeypatch.setattr(settings, "PAPER_CANDIDATE_SHADOW_ENABLED", True)
    after = {name: paper._strategy_version(name) for name in EXPERIMENT_ACCOUNTS}
    assert len(before) == 12 and before == after


def test_primary_scan_receipt_keeps_denominator_and_reason(sink):
    paper._capture_primary_candidate_shadow(
        account=SimpleNamespace(account_name="auction", id=5),
        candidate={"candidate_count": 0, "diagnostic_count": 1},
        stage="scan_completed", reason="original_candidate_count=0; missing auction evidence")
    row = sink[1][0]
    assert not row["original_candidate"] and row["code"] in ("", "MARKET")
    assert row["gate_inputs"]["candidate_count"] == 0
    assert row["gate_inputs"]["diagnostic_count"] == 1
    assert row["reason"].endswith("missing auction evidence")


def test_primary_old_reported_day_is_not_forward_capture(sink):
    paper._capture_primary_candidate_shadow(
        account=SimpleNamespace(account_name="default", id=2),
        candidate={"code": "600001"}, original_candidate=True,
        reported_at=datetime(2000, 1, 1, 10))
    assert not sink[1]


def test_primary_stable_episode_is_not_transport_round(sink):
    for run in ("round-one", "round-two"):
        paper._capture_primary_candidate_shadow(
            account=SimpleNamespace(account_name="promotion", id=3),
            candidate={"code": "600001", "_source": "promotion_promotion",
                       "prediction_run_id": 88, "probability": 0.71},
            run_id=run, original_candidate=True)
    first, second = sink[1]
    assert first["episode_id"] == second["episode_id"]
    assert first["scan_id"] != second["scan_id"]
    assert first["probability"] == 0.71


def test_primary_different_prediction_batch_has_distinct_episode(sink):
    for cohort in (88, 89):
        paper._capture_primary_candidate_shadow(
            account=SimpleNamespace(account_name="promotion", id=3),
            candidate={"code": "600001", "_source": "promotion_promotion",
                       "prediction_run_id": cohort},
            run_id="round", original_candidate=True)
    assert sink[1][0]["episode_id"] != sink[1][1]["episode_id"]


def test_formal_clock_is_producer_observation_not_round_clock(sink):
    paper._capture_primary_candidate_shadow(
        account=SimpleNamespace(account_name="default", id=2),
        candidate={"code": "600001"}, original_candidate=True,
        original_confirmed=True, original_gate=True)
    row = sink[1][0]
    assert row["identities"]["original_confirmed_at"] == row["observed_at"]


@pytest.mark.asyncio
async def test_lifecycle_waits_for_previous_worker_and_uses_readonly_bindings(sink):
    module, _ = sink
    sequence = []
    old_draining = asyncio.Event()
    started = asyncio.Event()

    async def old_owner():
        await old_draining.wait()
        sequence.append("old_drained")
        raise asyncio.CancelledError()

    async def start(**kwargs):
        sequence.append("new_started")
        assert isinstance(kwargs["bindings"], dict)
        started.set()

    async def stop():
        sequence.append("new_stopped")

    module.start_candidate_shadow = start
    module.stop_candidate_shadow = stop
    scheduler = DataScheduler()
    previous = asyncio.create_task(old_owner())
    task = asyncio.create_task(scheduler._candidate_shadow_lifecycle(previous))
    await asyncio.sleep(0)
    assert sequence == []
    old_draining.set()
    await asyncio.wait_for(started.wait(), timeout=5)
    assert sequence == ["old_drained", "new_started"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sequence == ["old_drained", "new_started", "new_stopped"]
    assert scheduler._candidate_shadow_start_error is None


@pytest.mark.asyncio
async def test_real_primary_adapter_to_worker_same_quotes(tmp_path, monkeypatch):
    """Adapter integration fixture, not a claim that the production scanner confirmed."""
    from app.paper import candidate_shadow as observer
    clock = [datetime(2026, 9, 24, 10)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(paper, "datetime", Clock)
    monkeypatch.setattr(observer, "_now", lambda: clock[0])
    monkeypatch.setattr(settings, "PAPER_CANDIDATE_SHADOW_ENABLED", True)
    name = "default"
    bindings = {name: {"account_id": 2, "strategy_version": paper._strategy_version(name)}}
    await observer.start_candidate_shadow(output_dir=tmp_path, bindings=bindings)
    try:
        for index, price in enumerate((9.8, 10.1)):
            clock[0] = datetime(2026, 9, 24, 10) + timedelta(seconds=index * 31)
            quote = dict(code="600001", price=price, prev_close=10., avg_price=10.,
                         ask1_price=price, ask1_volume=100, limit_up=11.,
                         source_quote_at=clock[0], received_at=clock[0],
                         updated_at=clock[0], quote_round_id=f"quote-{index}")
            observer.capture_quotes([quote], observed_at=clock[0], round_id=f"quote-{index}")
            paper._capture_primary_candidate_shadow(
                account=SimpleNamespace(account_name=name, id=2),
                candidate={"code": "600001", "_source": "next_day_plan", "signal_date": "2026-09-24"},
                spot=SimpleNamespace(**quote), run_id=f"scan-{index}",
                stage="strategy_confirmed" if index else "candidate_quote_observed",
                original_candidate=True, original_confirmed=True if index else None,
                original_gate=True if index else None,
            )
    finally:
        await observer.stop_candidate_shadow()
    report = observer.read_candidate_shadow_report(
        trade_date=clock[0].date(), route="A", limit=100, output_dir=tmp_path)
    frames = [r for r in report["records"] if r["kind"] == "frame"]
    assert len(frames) == 2
    confirmed = next(row for row in frames if row["capture_input"]["stage"] == "strategy_confirmed")
    assert confirmed["execution_binding_valid"] is True
    assert confirmed["result"]["baseline"]["value"] is True
    assert confirmed["result"]["candidate"]["value"] is True
    assert confirmed["result"]["candidate"]["reason"] == "recent_dual_line_reclaim"
    assert confirmed["result"]["orders_created"] == confirmed["result"]["pushes_created"] == 0
    assert report["production_permission"] is False
    # Real worker -> immutable files -> actual API row projection (not a UI mock).
    monkeypatch.setattr(observer, "DEFAULT_OUTPUT", tmp_path)
    visible = await paper.paper_candidate_shadow_report(
        trade_date=clock[0].date(), route="A", limit=100)
    assert "records" not in visible and visible["raw_evidence_omitted"] is True
    assert visible["read_only"] is True and visible["production_permission"] is False
    row = next(item for item in visible["rows"] if item["frame_id"] == confirmed["frame_id"])
    assert row["source_quote_at"] == clock[0].isoformat()
    assert row["observed_at"] == row["predicate_asof"] == clock[0].isoformat()
    assert datetime.fromisoformat(row["recorded_at"]) >= clock[0]
    assert row["reference_price"] == 10.1
    assert row["baseline"]["value"] is True and row["candidate"]["value"] is True
    assert row["label_sampling"]["anchor_frame_id"] == confirmed["frame_id"]
    assert row["future_labels"] and all(item.get("reference_markout_pct") is None for item in row["future_labels"])
    assert isinstance(visible["coverage"]["recent_receipts"], list)
    assert visible["status"]["running"] is False


def test_new_helper_has_no_trading_or_network_calls():
    source = inspect.getsource(paper._capture_primary_candidate_shadow)
    tree = ast.parse(source)
    names = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not names & {"record_buy_point", "submit_order", "buy", "get_db",
                        "_get_or_create_account", "capture_confirmed_signal"}
    assert not any(isinstance(n, ast.Await) for n in ast.walk(tree))
