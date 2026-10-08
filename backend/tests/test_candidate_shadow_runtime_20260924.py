"""Synthetic temp-file-only forward observer tests: no business DB/network."""
import asyncio
import gzip
from copy import deepcopy
from datetime import datetime, timedelta
import threading

import pytest

from app.paper import candidate_shadow as shadow
from app.paper import intraday_route_research as core

START = datetime(2026, 9, 23, 10)
BINDINGS = {"challenger_b": {"account_id": 7, "strategy_version": "execution-v7"}}


def quote(seconds=0, **changes):
    at = START + timedelta(seconds=seconds)
    q = dict(code="600001", price=10.3, prev_close=10, avg_price=10.1,
             ask1_price=10.3, ask1_volume=100, source_quote_at=at.isoformat(),
             received_at=at.isoformat(), observed_at=at.isoformat())
    q.update(changes)
    return q


def frame(seconds=0, **changes):
    p = dict(route="B2", observed_at=START+timedelta(seconds=seconds),
             producer_reported_at=START+timedelta(seconds=seconds),
             account_id=7, account_name="challenger_b", production_version="source-v1",
             code="600001", name="synthetic", evidence_ref=f"f:{seconds}", scan_id="episode1",
             stage="predicate", reason="synthetic", original_candidate=True,
             original_confirmed=False, original_gate=True, quote=quote(seconds),
             gate_inputs={"minimum_change_pct": 0}, rule_snapshot={"min_vwap_slope_pct": 0},
             identities={})
    p.update(changes)
    return p


def runtime(tmp_path, bindings=None):
    r = shadow._Runtime(tmp_path, BINDINGS if bindings is None else bindings)
    r.core, r.core_sha = core, "synthetic-unit"
    return r


def records(r):
    return r.batch


def test_inactive_callbacks_do_not_touch_objects(monkeypatch):
    monkeypatch.setattr(shadow, "_runtime", None)
    class Explode:
        def __iter__(self):
            raise AssertionError("inactive touched input")
    shadow.capture_frame(Explode())
    shadow.capture_quotes(Explode(), observed_at=Explode(), round_id=Explode())
    assert shadow.candidate_shadow_status()["active"] is False


def test_owned_capture_and_queue_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "_now", lambda: START + timedelta(seconds=60))
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_runtime", r)
    p = frame()
    shadow.capture_frame(p)
    p["quote"]["price"] = 99
    captured = r.queue.get_nowait()[1]
    assert captured["quote"]["price"] == 10.3
    for _ in range(shadow.QUEUE_SIZE + 10):
        shadow.capture_frame(frame())
    assert r.queue.qsize() == shadow.QUEUE_SIZE
    assert r.counts["queue_full"] == 10
    r.queue.get_nowait()
    shadow.capture_frame({"quote": object()})
    assert r.counts["capture_invalid"] == 1


def test_core_adapter_uses_actual_gates_and_never_rejudges(tmp_path):
    r = runtime(tmp_path)
    p = frame()
    before = deepcopy(p)
    r.frame(p)
    r.frame(frame(30))
    frames = [x for x in records(r) if x["kind"] == "frame"]
    assert p == before
    assert frames[-1]["result"]["candidate"]["value"] is True
    assert frames[-1]["execution_strategy_version"] == "execution-v7"
    assert frames[-1]["result"]["production_version"] == "source-v1"
    r.frame(frame(60, original_gate=False))
    assert records(r)[-1]["result"]["candidate"]["value"] is False


def test_duplicate_frames_and_gaps_do_not_complete_streak(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame())
    r.frame(frame())
    assert r.counts["duplicate_frames"] == 1
    r.lost("queue_full", frame())
    r.boundary()
    r.frame(frame(30))
    assert records(r)[-1]["result"]["candidate"]["value"] is False
    assert any(x["kind"] == "gap" for x in records(r))


@pytest.mark.parametrize("change", [
    {"account_id": 123}, {"account_name": "default"},
    {"original_gate": None}, {"original_confirmed": None},
])
def test_unknown_and_binding_not_fabricated(tmp_path, change):
    r = runtime(tmp_path)
    r.frame(frame(**change))
    result = records(r)[-1]
    assert result["result"]["candidate"]["value"] is not True
    if "original_confirmed" in change:
        assert result["result"]["baseline"]["value"] is None


def test_c3_double_null_identity(tmp_path):
    r = runtime(tmp_path)
    p = frame(route="C3", account_id=None, account_name=None, original_confirmed=True,
              identities={"original_confirmed_at": START.isoformat()})
    r.frame(p)
    out = records(r)[-1]
    assert out["execution_binding_valid"]
    assert out["candidate_input"]["account_id"] is None
    assert out["execution_strategy_version"] is None


def test_future_predicate_and_future_quote_rejected(tmp_path):
    r = runtime(tmp_path)
    with pytest.raises(ValueError):
        r.frame(frame(observed_at=datetime.now()+timedelta(days=1)))
    r.frame(frame(quote=quote(30)))
    assert records(r)[0]["result"]["candidate"]["value"] is None
    assert records(r)[-1]["kind"] == "label_censored"


def test_formal_confirmation_not_invented_from_gate(tmp_path):
    r = runtime(tmp_path, {"mainline": {"account_id": 4, "strategy_version": "exec"}})
    r.frame(frame(route="C", account_id=4, account_name="mainline",
                  original_confirmed=True, identities={"pool_identity": True, "mainline_identity": True}))
    assert records(r)[0]["result"]["baseline"]["value"] is None
    assert records(r)[0]["result"]["candidate"]["value"] is None


def test_history_has_no_original_gate_and_cannot_backdate_candidate(tmp_path):
    r = runtime(tmp_path)
    r.quotes({"observed_at": START.isoformat(), "round_id": "q0", "records": [quote()]})
    p = frame(30, route="C3", account_id=None, account_name=None, original_confirmed=True,
              identities={"original_confirmed_at": (START+timedelta(seconds=30)).isoformat()},
              sector_relative_strength_pct=1)
    r.frame(p)
    out = records(r)[0]
    assert out["candidate_input"]["frozen_at"] == p["observed_at"].isoformat()
    history = [x for x in out["sample_inputs"] if x.get("sample_role") == "feature_history"]
    assert len(history) == 1
    assert "original_gate" not in history[0]


def test_labels_isolated_preserve_control_and_gap(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame(original_gate=False))
    original = deepcopy(records(r)[0])
    for sec in range(30, 301, 30):
        r.quotes({"observed_at": (START+timedelta(seconds=sec)).isoformat(),
                  "round_id": str(sec), "records": [quote(sec, price=10.4)]})
    labels = [x for x in records(r) if x["kind"] == "future_label"]
    assert labels[0]["horizon_minutes"] == 5
    assert labels[0]["status"] == "observed"
    assert labels[0]["reference_is_fill"] is False and labels[0]["net_profit"] is None
    assert records(r)[0] == original
    r.quotes({"observed_at": (START+timedelta(minutes=15)).isoformat(),
              "round_id": "gap", "records": [quote(900)]})
    assert [x for x in records(r) if x["kind"] == "future_label"][-1]["status"] == "unknown"


def test_scan_identity_never_crosses_cohort(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame())
    r.frame(frame(30, scan_id="different"))
    assert records(r)[-1]["result"]["candidate"]["value"] is False


def test_b_d_are_not_confirmed_signals(tmp_path):
    r = runtime(tmp_path, {"promotion": {"account_id": 3, "strategy_version": "exec"},
                           "auction": {"account_id": 5, "strategy_version": "exec"}})
    for route, account, aid in [("B", "promotion", 3), ("D", "auction", 5)]:
        r.frame(frame(route=route, account_name=account, account_id=aid))
        assert records(r)[-1]["result"]["candidate"]["value"] is None


def test_resources_bounded_and_censor_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "MAX_PENDING", 2)
    monkeypatch.setattr(shadow, "MAX_STREAMS", 2)
    r = runtime(tmp_path)
    for index in range(5):
        r.frame(frame(index, scan_id=str(index)))
    assert len(r.pending) == len(r.streams) == 2
    assert any(x.get("reason") == "pending_capacity" for x in records(r))
    assert any(x.get("reason") == "stream_capacity" for x in records(r))


def test_file_failure_recovery_boundary(tmp_path, monkeypatch):
    from app.paper import research_reports
    r = runtime(tmp_path)
    r.frame(frame())
    publish = shadow._publish_shadow
    monkeypatch.setattr(shadow, "_publish_shadow", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    r.flush()
    assert r.counts["write_failed"] == 1
    assert r.batch == []
    r.boundary()
    monkeypatch.setattr(shadow, "_publish_shadow", publish)
    r.flush()
    out = shadow.read_candidate_shadow_report(trade_date=datetime.now().date(), output_dir=tmp_path)
    assert any(x["kind"] == "gap" for x in out["records"])


def test_readonly_files_hash_truncation_and_no_directory_creation(tmp_path):
    missing = tmp_path / "missing"
    out = shadow.read_candidate_shadow_report(trade_date="2026-09-23", output_dir=missing)
    assert out["records"] == [] and not missing.exists()
    r = runtime(tmp_path)
    r.frame(frame())
    r.frame(frame(30))
    r.flush()
    out = shadow.read_candidate_shadow_report(trade_date="2026-09-23", output_dir=tmp_path, limit=1)
    assert out["truncated"] and out["count"] == 1
    file = next((tmp_path/"2026-09-23").glob("*.json.gz"))
    file.write_text("{}")
    assert shadow.read_candidate_shadow_report(trade_date="2026-09-23", output_dir=tmp_path)["errors"]


def test_async_lifecycle_single_thread_restart_no_old_state(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "_runtime", None)
    monkeypatch.setattr(shadow, "_now", lambda: START + timedelta(seconds=60))
    caller = threading.get_ident()
    threads = []
    original = core.evaluate_strategy_candidate_experiment
    def checked(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(core, "evaluate_strategy_candidate_experiment", checked)
    async def run():
        first = await shadow.start_candidate_shadow(output_dir=tmp_path, bindings=BINDINGS)
        again = await shadow.start_candidate_shadow(output_dir=tmp_path, bindings=BINDINGS)
        assert first["session_id"] == again["session_id"]
        shadow.capture_frame(frame())
        shadow.capture_frame(frame(30))
        stopped = await shadow.stop_candidate_shadow()
        assert not stopped["active"] and stopped["queue_size"] == 0
        second = await shadow.start_candidate_shadow(output_dir=tmp_path, bindings=BINDINGS)
        assert second["session_id"] != first["session_id"]
        shadow.capture_frame(frame(60))
        await shadow.stop_candidate_shadow()
    asyncio.run(run())
    assert threads and all(t != caller for t in threads)
    rows = shadow.read_candidate_shadow_report(trade_date="2026-09-23", output_dir=tmp_path)["records"]
    frames = [x for x in rows if x["kind"] == "frame"]
    assert len(frames) == 3
    assert sum(x["result"]["candidate"]["value"] is True for x in frames) == 1


@pytest.mark.parametrize("seconds", [-86400, 86400, -181])
def test_callback_rejects_old_day_future_and_stale_capture(tmp_path, monkeypatch, seconds):
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_runtime", r)
    monkeypatch.setattr(shadow, "_now", lambda: START)
    shadow.capture_frame(frame(seconds))
    assert r.queue.empty() and r.counts["capture_clock_invalid"] == 1


def test_duplicate_quote_source_not_new_prehistory(tmp_path):
    r = runtime(tmp_path)
    r.quotes({"observed_at": START.isoformat(), "round_id": "one", "records": [quote()]})
    r.quotes({"observed_at": (START+timedelta(seconds=30)).isoformat(), "round_id": "two",
              "records": [quote(received_at=(START+timedelta(seconds=30)).isoformat(),
                                observed_at=(START+timedelta(seconds=30)).isoformat())]})
    assert len(r.history["600001"]) == 1
    r.quotes({"observed_at": (START+timedelta(seconds=30)).isoformat(), "round_id": "conflict",
              "records": [quote(price=10.4)]})
    assert not r.history.get("600001")
    assert any(x["kind"] == "quote_gap" for x in records(r))


def test_six_minute_history_expiry_and_capacity_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "MAX_HISTORY_ROWS", 2)
    r = runtime(tmp_path)
    for sec in (0, 10, 20):
        r.quotes({"observed_at": (START+timedelta(seconds=sec)).isoformat(),
                  "round_id": str(sec), "records": [quote(sec)]})
    assert len(r.history["600001"]) == 2
    assert r.counts["history_row_capacity"] == 1
    r.quotes({"observed_at": (START+timedelta(seconds=400)).isoformat(),
              "round_id": "expire", "records": []})
    assert not r.history


def test_predicate_clock_not_replaced_with_quote_clock(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame(20, quote=quote()))
    out = records(r)[0]
    assert out["sample_inputs"][-1]["observed_at"] == (START+timedelta(seconds=20)).isoformat()
    assert out["sample_inputs"][-1]["quote_observed_at"] == START.isoformat()
    assert out["sample_inputs"][-1]["experiment_observation_basis"] == "producer_actual_predicate_read"
    assert out["sample_inputs"][-1]["predicate_observed_at"] == (START+timedelta(seconds=20)).isoformat()
    assert out["result"]["evaluated_at"] == (START+timedelta(seconds=20)).isoformat()


def test_labels_lunch_crossing_and_price_basis_unknown(tmp_path):
    r = runtime(tmp_path)
    seconds = 89*60
    r.frame(frame(seconds))
    for sec in (seconds+30, seconds+60, 3*3600, 3*3600+4*60):
        r.quotes({"observed_at": (START+timedelta(seconds=sec)).isoformat(),
                  "round_id": str(sec), "records": [quote(sec)]})
    labels = [x for x in records(r) if x["kind"] == "future_label"]
    assert labels and labels[0]["status"] == "unknown"
    assert labels[0]["reference_markout_pct"] is None


@pytest.mark.parametrize("route", core.STRATEGY_CANDIDATE_ROUTES)
def test_all_thirteen_route_identities_are_independent(tmp_path, route):
    name = core.STRATEGY_CANDIDATE_ACCOUNTS[route]
    aid = 7 if name else None
    r = runtime(tmp_path, {name: {"account_id": aid, "strategy_version": "exec"}} if name else {})
    r.frame(frame(route=route, account_name=name, account_id=aid))
    out = records(r)[0]
    assert out["execution_binding_valid"] is True
    assert out["result"]["production_permission"] is False
    assert out["result"]["orders_created"] == out["result"]["pushes_created"] == 0


def test_core_source_drift_stops_observer_only(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "SEALED_CORE_SHA256", "mismatch")
    r = runtime(tmp_path)
    r.run()
    assert r.active is False and r.counts["worker_fatal"] == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("stage", ["scan_completed", "not_scanned"])
def test_scan_receipts_never_enter_core_or_label_set(tmp_path, monkeypatch, stage):
    r = runtime(tmp_path)
    monkeypatch.setattr(core, "evaluate_strategy_candidate_experiment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("not stock")))
    r.frame(frame(code="", stage=stage, quote={}))
    assert records(r)[0]["kind"] == "scan_receipt"
    assert not r.streams and not r.pending
    assert r.counts["scan_receipts"] == 1


def test_no_quote_unknown_does_not_overwrite_or_poison_later_frame(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame(quote={}))
    old = deepcopy(records(r)[0])
    r.frame(frame(30))
    r.frame(frame(60))
    frames = [x for x in records(r) if x["kind"] == "frame"]
    assert frames[0] == old and frames[0]["result"]["candidate"]["value"] is None
    assert frames[-1]["result"]["candidate"]["value"] is True


def test_optional_packet_leaves_and_cheap_active_getter(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_runtime", r)
    monkeypatch.setattr(shadow, "_now", lambda: START)
    monkeypatch.setattr(r, "status", lambda: (_ for _ in ()).throw(AssertionError("expensive status")))
    assert shadow.candidate_shadow_active() is True
    shadow.capture_frame(frame(probability=0.2, producer_reported_at=START-timedelta(seconds=5),
                               source_persistence="producer_observed_not_commit_receipt"))
    owned = r.queue.get_nowait()[1]
    assert owned["probability"] == 0.2
    assert owned["producer_reported_at"] == (START-timedelta(seconds=5)).isoformat()
    assert owned["source_persistence"] == "producer_observed_not_commit_receipt"
    r.active = False
    assert shadow.candidate_shadow_active() is False


def test_top_level_probability_is_core_ranking_input_only(tmp_path):
    r = runtime(tmp_path, {"promotion": {"account_id": 3, "strategy_version": "exec"}})
    r.frame(frame(route="B", account_id=3, account_name="promotion", probability=0.2,
                  identities={"pool_identity": True}))
    out = records(r)[0]["result"]
    assert out["early_observation"]["ranking_score"] == 0.2
    assert out["candidate"]["value"] is None


def test_real_shadow_producer_packets_episode_stages_and_nominal_quote(tmp_path, monkeypatch):
    from app.paper import strategy_iteration_shadow as producer
    current = [START]
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]
    monkeypatch.setattr(producer, "datetime", FrozenDatetime)
    monkeypatch.setattr(shadow, "_now", lambda: current[0])
    bindings = {"challenger_b": {"account_id": 7, "strategy_version": "exec"},
                "challenger_c": {"account_id": 8, "strategy_version": "exec"}}
    r = runtime(tmp_path, bindings)
    monkeypatch.setattr(shadow, "_runtime", r)
    def send(route_id, sec, stage, gate=None, confirmed=None):
        current[0] = START+timedelta(seconds=sec, milliseconds=1 if stage == "confirmed" else 2 if stage == "post_confirm_observation" else 0)
        q = quote(sec, avg_price=10.0+sec/300, quote_round_id=f"round-{sec}")
        q.pop("observed_at")
        q["updated_at"] = q["received_at"]
        producer._capture_candidate_projection(
            route_id, START+timedelta(seconds=sec), code="600001", quote=q,
            stage=stage, reason="test", original_candidate=True,
            original_confirmed=confirmed, original_gate=gate,
            gate_inputs={"captured_original_gate": True}, rules={"min_vwap_slope_pct": 0},
            metrics={"relative_strength_pct": 1.0}, version="source-v1")
        kind, packet, _ = r.queue.get_nowait()
        assert kind == "frame"
        r.frame(packet)
    for route_id in (producer.ROUTE_B, producer.ROUTE_C):
        send(route_id, 0, "structural")
        send(route_id, 0, "static_gate", gate=True)
        send(route_id, 0, "confirmation_sample")
        send(route_id, 30, "static_gate", gate=True)
        send(route_id, 30, "confirmation_sample")
        send(route_id, 30, "confirmed", confirmed=True)
        send(route_id, 30, "post_confirm_observation")
    frames = [x for x in records(r) if x["kind"] == "frame"]
    for route in ("B2", "C2"):
        selected = [x for x in frames if x["route"] == route]
        assert len({x["candidate_id"] for x in selected}) == 1
        assert selected[-1]["result"]["candidate"]["value"] is True
        assert selected[-1]["result"]["sample_count"] == 2
        assert selected[-1]["candidate_input"]["account_id"] == bindings[selected[-1]["capture_input"]["account_name"]]["account_id"]
        assert selected[-1]["capture_input"]["account_id"] is None
        assert selected[-1]["sample_inputs"][-1]["quote_observation_basis"] == "nominal_updated_visible_by_capture"
        assert selected[0]["result"]["candidate"]["value"] is not True


def test_real_primary_producer_packets_cohort_receipts_and_rows(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.api.v1 import paper as producer
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return START
    monkeypatch.setattr(producer, "datetime", FrozenDatetime)
    monkeypatch.setattr(shadow, "_now", lambda: START)
    monkeypatch.setattr(producer.settings, "PAPER_CANDIDATE_SHADOW_ENABLED", True)
    r = runtime(tmp_path, {"promotion": {"account_id": 3, "strategy_version": producer._strategy_version("promotion")}})
    monkeypatch.setattr(shadow, "_runtime", r)
    account = SimpleNamespace(id=3, account_name="promotion")
    spot = SimpleNamespace(**quote(), updated_at=START, quote_round_id="qr1")
    for run, prediction in (("run1", 1), ("run2", 1), ("run3", 2)):
        producer._capture_primary_candidate_shadow(
            account=account, candidate={"code": "600001", "_source": "promotion",
                                       "probability": 0.4, "prediction_run_id": prediction},
            spot=spot, run_id=run, stage="candidate", original_candidate=True)
        r.frame(r.queue.get_nowait()[1])
    producer._capture_primary_candidate_shadow(account=account, run_id="run4",
                                               stage="scan_completed", reason="done")
    r.frame(r.queue.get_nowait()[1])
    frames = [x for x in records(r) if x["kind"] == "frame"]
    assert frames[0]["candidate_id"] == frames[1]["candidate_id"]
    assert frames[1]["candidate_id"] != frames[2]["candidate_id"]
    assert frames[0]["result"]["early_observation"]["ranking_score"] == 0.4
    assert records(r)[-1]["kind"] == "scan_receipt"
    r.flush()
    monkeypatch.setattr(shadow, "_now", lambda: START+timedelta(seconds=300))
    for sec in range(30, 301, 30):
        r.quotes({"observed_at": (START+timedelta(seconds=sec)).isoformat(),
                  "round_id": str(sec), "records": [quote(sec)]})
    r.flush()
    report = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path, limit=3)
    assert len(report["rows"]) == 3 and report["truncated"]
    assert report["rows"][0]["baseline"]["value"] is None
    assert report["rows"][0]["candidate"]["value"] is None
    assert report["rows"][0]["future_labels"][0]["status"] == "observed"
    assert {"enabled", "running", "version"} <= shadow.candidate_shadow_status().keys()


def test_producer_reset_and_same_source_gate_conflict_break_continuity(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame(episode_id="stable"))
    r.frame(frame(30, episode_id="stable", scan_id="scan2"))
    assert records(r)[-1]["result"]["candidate"]["value"] is True
    r.frame(frame(30, episode_id="stable", scan_id="scan2", stage="confirmed", original_gate=False))
    assert records(r)[-1]["result"]["candidate"]["reason"] == "same_source_gate_or_quote_conflict"
    r.frame(frame(60, episode_id="stable", scan_id="scan3"))
    assert records(r)[-1]["result"]["candidate"]["value"] is False
    r.frame(frame(90, episode_id="stable", scan_id="scan4", stage="reset", original_gate=False))
    assert any(x.get("reason") == "producer_reset" for x in records(r))
    r.frame(frame(120, episode_id="stable", scan_id="scan5"))
    assert records(r)[-1]["result"]["candidate"]["value"] is False


def test_nominal_quote_visibility_does_not_fake_commit_or_repair_bad_clock(tmp_path):
    r = runtime(tmp_path)
    q = quote()
    q.pop("observed_at")
    q["updated_at"] = (START-timedelta(seconds=1)).isoformat()
    r.frame(frame(quote=q))
    assert records(r)[0]["result"]["candidate"]["value"] is None
    q["updated_at"] = START.isoformat()
    q["committed_at"] = (START+timedelta(seconds=1)).isoformat()
    r.frame(frame(quote=q, evidence_ref="future-commit"))
    assert [x for x in records(r) if x["kind"] == "frame"][-1]["result"]["candidate"]["value"] is None


def test_top_level_auction_source_contract_is_passed_without_signal(tmp_path):
    r = runtime(tmp_path, {"auction": {"account_id": 5, "strategy_version": "exec"}})
    contract = {"two_distinct_verified_frames": True, "original_source_quality_passed": True,
                "evidence_ref": "source-verified", "evidence_at": START.isoformat(),
                "observed_at": START.isoformat()}
    r.frame(frame(route="D", account_id=5, account_name="auction", quote={},
                  source_contract=contract))
    result = records(r)[0]["result"]
    assert result["early_observation"]["value"] is True
    assert result["candidate"]["value"] is None
    assert result["production_permission"] is False


def test_byte_budget_is_admission_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "MAX_QUEUE_BYTES", 1000)
    monkeypatch.setattr(shadow, "_now", lambda: START)
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_runtime", r)
    shadow.capture_frame(frame())
    assert r.queue.empty() and r.counts["queue_full"] == 1
    assert r.queue.queued_bytes == 0


def test_repeated_states_preserve_all_frames_but_one_label_anchor(tmp_path):
    r = runtime(tmp_path)
    for sec in (0, 30, 60, 90):
        r.frame(frame(sec, original_gate=False, episode_id="stable", scan_id=str(sec)))
    frames = [x for x in records(r) if x["kind"] == "frame"]
    assert len(frames) == 4 and len(r.pending) == 1
    assert frames[0]["label_sampling"]["selected"]
    assert all(not row["label_sampling"]["selected"] for row in frames[1:])
    assert len({row["label_sampling"]["anchor_frame_id"] for row in frames}) == 1


def test_latest_frame_limit_independent_of_labels_and_directory_order(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_now", lambda: START+timedelta(seconds=90))
    for sec in (60, 0, 30):
        r.frame(frame(sec, episode_id=str(sec), scan_id=str(sec)))
    r.flush()
    # Outcomes are later publications and must not displace latest frame rows.
    for index in range(8):
        r.emit({"kind": "label_censored", "trade_date": START.date().isoformat(),
                "frame_id": f"unrelated-{index}", "route": "B2", "reason": "test",
                "remaining_horizons": ["5"]})
    r.flush()
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path, limit=2)
    assert [row["predicate_asof"] for row in out["rows"]] == [
        (START+timedelta(seconds=60)).isoformat(), (START+timedelta(seconds=30)).isoformat()]
    assert out["truncated"]


def test_capacity_5000_quotes_300_candidates_ten_rounds(tmp_path, monkeypatch):
    import json
    import time
    current = [START]
    monkeypatch.setattr(shadow, "_now", lambda: current[0])
    monkeypatch.setattr(shadow, "_runtime", None)
    timings = []
    async def run():
        await shadow.start_candidate_shadow(output_dir=tmp_path, bindings=BINDINGS)
        r = shadow._runtime
        for round_index in range(10):
            sec = round_index * 30
            current[0] = START+timedelta(seconds=sec)
            quotes = [quote(sec, code=f"{600000+index:06d}") for index in range(5000)]
            began = time.perf_counter()
            shadow.capture_quotes(quotes, observed_at=current[0], round_id=str(round_index))
            for index in range(300):
                gate = (False, True, None)[index % 3]
                for stage in ("static_gate", "confirmation_sample", "observation"):
                    shadow.capture_frame(frame(sec, code=f"{600000+index:06d}",
                        quote=quotes[index], episode_id=f"episode:{index}", scan_id=str(round_index),
                        stage=stage, evidence_ref=f"{round_index}:{index}:{stage}",
                        original_gate=gate if stage == "static_gate" else None,
                        original_confirmed=False if stage == "static_gate" else None))
            enqueue_sec = time.perf_counter() - began
            await asyncio.wait_for(asyncio.to_thread(r.queue.join), timeout=30)
            drain_sec = time.perf_counter() - began
            timings.append({"enqueue_seconds": round(enqueue_sec, 4), "drain_seconds": round(drain_sec, 4)})
            assert drain_sec < 30
            assert r.generation == 0
        status = r.status()
        pending = len(r.pending)
        assert status["counts"]["evaluated_frames"] == 9000
        assert status["counts"].get("queue_full", 0) == 0
        assert status["counts"].get("gap_discarded_packets", 0) == 0
        assert status["counts"].get("label_repeated_state", 0) > 7000
        assert status["counts"].get("core_evaluation_reused", 0) > 3000
        assert pending <= 900
        assert r.queue.peak_bytes <= shadow.MAX_QUEUE_BYTES
        await shadow.stop_candidate_shadow()
        files = list(tmp_path.rglob("*.json.gz"))
        durable_frames = sum(sum(row.get("kind") == "frame" for row in json.loads(gzip.decompress(path.read_bytes()))["records"])
                             for path in files)
        assert durable_frames == 9000
        print("CAPACITY_EVIDENCE=" + json.dumps({
            "quotes_per_round": 5000, "candidates_per_round": 300, "stages": 3,
            "rounds": 10, "logical_interval_seconds": 30, "round_timings": timings,
            "peak_queue_records": status["peak_queue_records"],
            "peak_queue_bytes": status["peak_queue_bytes"], "pending_anchors": pending,
            "counts": status["counts"], "files": len(files), "durable_frames_after_stop": durable_frames,
            "total_file_bytes": sum(path.stat().st_size for path in files)}, sort_keys=True))
    asyncio.run(run())


def test_leaf_tree_projection_budget_rejects_before_unbounded_copy():
    huge = {str(i): {str(j): "x"*2048 for j in range(64)} for i in range(64)}
    with pytest.raises(ValueError, match="owned_tree_byte_budget"):
        shadow._leaf(huge)


def test_latest_file_selection_scans_past_unordered_head(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    for sec in (90, 0, 60, 30):
        monkeypatch.setattr(shadow, "_now", lambda sec=sec: START+timedelta(seconds=sec))
        r.frame(frame(sec, episode_id=str(sec), scan_id=str(sec)))
        r.flush()
    monkeypatch.setattr(shadow, "MAX_REPORT_FILES", 2)
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path, limit=1)
    assert out["total_files"] == 4 and out["scanned_files"] == 2
    assert out["rows"][0]["predicate_asof"] == (START+timedelta(seconds=90)).isoformat()
    assert out["truncated"] is True


def test_real_b2_producer_second_31s_experiment_precedes_third_formal(tmp_path, monkeypatch):
    from app.paper import strategy_iteration_shadow as producer
    current = [START+timedelta(seconds=1)]
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]
    monkeypatch.setattr(producer, "datetime", FrozenDatetime)
    monkeypatch.setattr(shadow, "_now", lambda: current[0])
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_runtime", r)
    for index, seconds in enumerate((0, 31, 62)):
        # Nominal quote clocks precede the actual producer read by a real second.
        current[0] = START+timedelta(seconds=seconds+1, milliseconds=index*3)
        q = quote(seconds, quote_round_id=f"round-{index}")
        q.pop("observed_at")
        q["updated_at"] = (START+timedelta(seconds=seconds, milliseconds=100)).isoformat()
        producer._capture_candidate_projection(
            producer.ROUTE_B, START+timedelta(seconds=seconds), code="600001", quote=q,
            stage="static_gate", reason="actual_original_static_pass",
            original_candidate=True, original_confirmed=False, original_gate=True,
            gate_inputs={"captured_original_gate": True}, rules={"min_vwap_slope_pct": 0},
            version="source-v1")
        r.frame(r.queue.get_nowait()[1])
        latest = [x for x in records(r) if x["kind"] == "frame"][-1]
        sample = latest["sample_inputs"][-1]
        assert sample["observed_at"] == current[0].isoformat()
        assert sample["quote_observed_at"] is None
        assert sample["quote_visibility_at"] == q["updated_at"]
        assert sample["source_quote_at"] == q["source_quote_at"]
        assert latest["result"]["baseline"]["value"] is False
        assert latest["result"]["candidate"]["value"] is (index >= 1)
        if index == 1:
            second = deepcopy(latest)
        if index == 2:
            current[0] += timedelta(milliseconds=1)
            producer._capture_candidate_projection(
                producer.ROUTE_B, START+timedelta(seconds=seconds), code="600001", quote=q,
                stage="confirmed", reason="original_three_frames_60s",
                original_candidate=True, original_confirmed=True, original_gate=None,
                gate_inputs={"captured_original_gate": True}, rules={"min_vwap_slope_pct": 0},
                version="source-v1")
            r.frame(r.queue.get_nowait()[1])
    frames = [x for x in records(r) if x["kind"] == "frame"]
    assert frames[1] == second
    assert frames[-1]["result"]["baseline"]["value"] is True
    assert frames[-1]["result"]["sample_count"] == 3
    assert frames[-1]["result"]["policy"]["clock_jitter_sec"] == 0
    assert frames[-1]["sample_inputs"][-1]["observed_at"] < current[0].isoformat()


def test_b2_actual_observation_does_not_relax_thirty_second_formula(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame(1, quote=quote(0), episode_id="stable"))
    r.frame(frame(30, quote=quote(29), episode_id="stable", scan_id="next"))
    out = records(r)[-1]["result"]
    assert out["sample_count"] == 2
    assert out["candidate"]["value"] is False
    assert out["policy"]["min_persistence_sec"] == 30 and out["policy"]["clock_jitter_sec"] == 0


def test_feature_history_uses_actual_payload_publish_clock(tmp_path):
    r = runtime(tmp_path)
    at = START+timedelta(seconds=2)
    r.quotes({"observed_at": at.isoformat(), "round_id": "published", "records": [quote()]})
    sample = r.history["600001"][-1]
    assert sample["observed_at"] == at.isoformat()
    assert sample["quote_observed_at"] == START.isoformat()
    assert sample["experiment_observation_basis"] == "quote_payload_actual_publish"
    assert "original_gate" not in sample


def test_reader_cumulative_byte_budget_spans_both_hash_verified_passes(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    for sec in (0, 30, 60, 90):
        monkeypatch.setattr(shadow, "_now", lambda sec=sec: START+timedelta(seconds=sec))
        r.frame(frame(sec, episode_id=str(sec), scan_id=str(sec)))
        r.flush()
    file_sizes = [path.stat().st_size for path in (tmp_path/START.date().isoformat()).glob("*.json.gz")]
    budget = max(file_sizes) * 3
    monkeypatch.setattr(shadow, "MAX_REPORT_TOTAL_BYTES", budget)
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path, limit=200)
    assert out["bytes_read"] <= budget
    assert out["partial"] and out["truncated"]
    assert "frame_pass_byte_budget" in out["partial_reasons"]
    assert "total_read_byte_budget" in out["partial_reasons"]
    assert out["frame_selection_complete"] is False and out["labels_complete"] is False
    assert out["rows"][0]["predicate_asof"] == (START+timedelta(seconds=90)).isoformat()
    assert out["full_day_denominator"] is False


def test_reader_zero_deadline_does_not_read_files_and_is_partial(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    r.frame(frame())
    r.flush()
    monkeypatch.setattr(shadow, "MAX_REPORT_SECONDS", 0)
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path)
    assert out["bytes_read"] == 0 and out["rows"] == []
    assert out["partial"] and "time_budget" in out["partial_reasons"]
    assert out["directory_complete"] is False
    assert out["frame_selection_complete"] is False
    assert out["labels_complete"] is False


def test_reader_directory_budget_never_claims_complete_latest_selection(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    for sec in (0, 30, 60):
        r.frame(frame(sec, scan_id=str(sec)))
        r.flush()
    monkeypatch.setattr(shadow, "MAX_REPORT_DIRECTORY_ENTRIES", 1)
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path)
    assert out["partial"] and "directory_entry_budget" in out["partial_reasons"]
    assert not out["directory_complete"] and not out["frame_selection_complete"]
    assert out["total_files"] == 1  # lower bound, not complete daily file denominator


def test_b2_constant_two_second_projection_delay_preserves_thirty_second_scan(tmp_path):
    r = runtime(tmp_path)
    for source_sec in (0, 30):
        r.frame(frame(source_sec+2, quote=quote(source_sec),
                      producer_reported_at=START+timedelta(seconds=source_sec),
                      episode_id="stable", scan_id=str(source_sec)))
    out = records(r)[-1]["result"]
    assert out["sealed_candidate"]["value"] is True
    assert out["candidate"]["value"] is True
    guard = out["producer_scan_guard"]
    assert guard["producer_reported_span_sec"] == 30
    assert guard["source_span_sec"] == guard["actual_observed_span_sec"] == 30
    assert guard["clock_jitter_sec"] == 0


@pytest.mark.parametrize("source_second,scan_first,scan_second,actual_first,actual_second", [
    (29, 0, 29, 0, 31),       # Actual jitter cannot compensate source AND scan 29.
    (30, 1, 30, 2, 33),       # Pure source+actual would pass, original scan is only29.
])
def test_b2_varying_projection_delay_cannot_manufacture_scan_duration(
        tmp_path, monkeypatch, source_second, scan_first, scan_second, actual_first, actual_second):
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_runtime", r)
    for source_sec, scan_sec, actual_sec in ((0, scan_first, actual_first),
                                            (source_second, scan_second, actual_second)):
        monkeypatch.setattr(shadow, "_now", lambda actual_sec=actual_sec: START+timedelta(seconds=actual_sec))
        shadow.capture_frame(frame(actual_sec, quote=quote(source_sec),
                           producer_reported_at=START+timedelta(seconds=scan_sec),
                           episode_id="stable", scan_id=str(source_sec)))
    # Delayed worker must not supply either persistence clock.
    monkeypatch.setattr(shadow, "_now", lambda: START+timedelta(minutes=10))
    while not r.queue.empty():
        r.frame(r.queue.get_nowait()[1])
    out = records(r)[-1]["result"]
    assert out["candidate"]["value"] is False
    assert out["producer_scan_guard"]["producer_reported_span_sec"] == 29
    assert out["producer_scan_guard"]["actual_observed_span_sec"] == 31
    if source_second == 30:
        assert out["sealed_candidate"]["value"] is True
        assert out["candidate"]["reason"] == "producer_reported_persistence_not_proven"


@pytest.mark.parametrize("reported", [None, START-timedelta(seconds=1), START+timedelta(seconds=100)])
def test_b2_missing_before_received_or_future_original_scan_is_unknown(tmp_path, reported):
    r = runtime(tmp_path)
    r.frame(frame(2, quote=quote(), producer_reported_at=reported))
    out = records(r)[-1]["result"]
    assert out["candidate"]["value"] is None
    assert out["producer_scan_guard"]["reason"] == "producer_reported_clock_unproven"


def test_same_source_later_stage_cannot_repair_first_static_scan_clock(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame(2, quote=quote(), producer_reported_at=None, episode_id="stable"))
    r.frame(frame(3, quote=quote(), producer_reported_at=START,
                  original_gate=None, original_confirmed=None, stage="confirmation_sample",
                  episode_id="stable", evidence_ref="later"))
    r.frame(frame(32, quote=quote(30), producer_reported_at=START+timedelta(seconds=30),
                  episode_id="stable", scan_id="next"))
    out = records(r)[-1]["result"]
    assert out["sealed_candidate"]["value"] is True
    assert out["candidate"]["value"] is None
    assert records(r)[-1]["sample_inputs"][0]["producer_reported_at"] is None


@pytest.mark.parametrize("code", ["", "MARKET"])
@pytest.mark.parametrize("stage", ["probability_contract_blocked", "source_blocked",
                                   "coverage_blocked", "evidence_blocked"])
def test_all_global_issue_stages_are_non_stock_receipts(tmp_path, monkeypatch, code, stage):
    r = runtime(tmp_path)
    monkeypatch.setattr(core, "evaluate_strategy_candidate_experiment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("global entered stock core")))
    r.frame(frame(code=code, stage=stage, reason="original global diagnostic", quote={},
                  gate_inputs={"candidate_count": 17, "counts": {"visited": 5000, "allowed": 4500}}))
    record = records(r)[0]
    assert record["kind"] == "scan_receipt" and record["receipt_type"] == "scan_issue"
    assert record["stage"] == stage and record["reason"] == "original global diagnostic"
    assert record["coverage_counts"] == {"candidate_count": 17, "visited": 5000, "allowed": 4500}
    assert record["stock_denominator"] is False
    assert not r.pending and not r.streams


@pytest.mark.parametrize("code", ["ABC123", "６００００１", "60001", "6000017", "MARKET ", None])
def test_malformed_stock_identity_is_not_global_or_stock_denominator(tmp_path, monkeypatch, code):
    r = runtime(tmp_path)
    monkeypatch.setattr(core, "evaluate_strategy_candidate_experiment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("invalid entered core")))
    r.frame(frame(code=code, stage="source_blocked", quote={}))
    assert records(r)[0]["kind"] == "invalid_identity"
    assert records(r)[0]["stock_denominator"] is False
    assert not r.pending and not r.streams


def test_reader_coverage_separates_stock_global_and_invalid_identities(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame())
    r.frame(frame(code="", stage="probability_contract_blocked", reason="source unavailable", quote={},
                  gate_inputs={"counts": {"candidate_count": 5}}))
    r.frame(frame(code="MARKET", stage="scan_completed", quote={}))
    r.frame(frame(code="ABC123", stage="source_blocked", quote={}))
    r.flush()
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path)
    assert out["row_count"] == 1 and out["rows"][0]["code"] == "600001"
    coverage = out["coverage"]
    assert coverage["stock_frame_records"] == 1
    assert coverage["non_stock_receipt_records"] == 2
    assert coverage["invalid_identity_records"] == 1
    assert coverage["receipt_types"]["scan_issue"] == 1
    assert coverage["receipt_types"]["scan_completed"] == 1
    assert any(row["reason"] == "source unavailable" for row in coverage["latest_receipts"])
    assert coverage["scope"] == "bounded_read_subset" and not coverage["full_day_denominator"]


def test_lightweight_receipts_survive_raw_strip_and_keep_expected_unknown(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    r.frame(frame(code="", stage="scan_completed", quote={}, scan_id="primary-run",
                  gate_inputs={"candidate_count": 0, "diagnostic_count": 1}))
    r.frame(frame(code="", stage="probability_contract_blocked", reason="frozen source missing",
                  quote={}, scan_id="primary-run"))
    r.frame(frame(code="MARKET", route="A2", account_name="challenger_a", stage="scan_complete",
                  quote={}, scan_id="a2-round", gate_inputs={"visited_quote_count": 5000,
                      "noncandidate_aggregated_count": 4999, "detailed_observation_count": 1}))
    r.flush()
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path)
    out.pop("records")
    assert out["rows"] == [] and out["summary"]["frame_records"] == 0
    assert out["summary"]["scope"] == "delivered_original_candidate_projection"
    coverage = out["coverage"]
    receipts = coverage["recent_receipts"]
    assert len(receipts) == 3
    assert any(row["reason"] == "frozen source missing" for row in receipts)
    primary = coverage["expected_scanned_by_route"]["B2"]
    assert primary["expected_candidate_count"] == 0 and primary["diagnostic_count"] == 1
    assert primary["expected_scanned_count"] is None
    assert primary["expected_scanned_status"] == "unknown"
    a2 = coverage["expected_scanned_by_route"]["A2"]
    assert a2["expected_scanned_count"] == 5000
    assert a2["expected_scanned_status"] == "producer_reported"
    assert any(row["gate_inputs"].get("noncandidate_aggregated_count") == 4999 for row in receipts)
    assert not coverage["full_day_denominator"]


def test_receipt_counts_never_join_different_original_scans(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    r.frame(frame(code="", stage="scan_completed", quote={}, scan_id="old",
                  gate_inputs={"candidate_count": 20, "diagnostic_count": 0}))
    r.frame(frame(1, code="", stage="source_blocked", quote={}, scan_id="new",
                  reason="new source unknown"))
    r.flush()
    out = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path)
    scan = out["coverage"]["expected_scanned_by_route"]["B2"]
    assert scan["scan_id"] == "new" and scan["expected_candidate_count"] is None
    assert scan["expected_scanned_status"] == "unknown"


async def _never_executor(*args, **kwargs):
    raise AssertionError("stop must not occupy default executor")


def test_stop_timeout_is_bounded_and_blocks_second_writer(tmp_path, monkeypatch):
    import time
    r = runtime(tmp_path)
    class StuckThread:
        def is_alive(self):
            return True
        def join(self, timeout=None):
            raise AssertionError("never join a live stuck writer")
    r.thread = StuckThread()  # No real blocked host thread.
    monkeypatch.setattr(shadow, "_runtime", r)
    monkeypatch.setattr(shadow, "STOP_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(asyncio, "to_thread", _never_executor)
    async def exercise():
        started = time.monotonic()
        result = await shadow.stop_candidate_shadow()
        assert time.monotonic() - started < 1
        assert result["status"] == "drain_incomplete"
        assert result["worker_alive"] and result["stop_timed_out"]
        assert result["drain_incomplete"] and result["drain_status"] == "unknown"
        assert not result["running"] and not shadow.candidate_shadow_active()
        assert result["watermarks"]["durable"] is None
        shadow.capture_frame(object())
        assert r.queue.empty()
        restarted = await shadow.start_candidate_shadow(output_dir=tmp_path / "forbidden")
        assert shadow._runtime is r and restarted["session_id"] == r.session_id
        assert restarted["status"] == "drain_incomplete"
        await shadow.stop_candidate_shadow()
        assert r.counts["stop_timeout"] == 1
        assert not (tmp_path / "forbidden").exists()
    asyncio.run(exercise())


def test_normal_stop_drains_without_default_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "_runtime", None)
    monkeypatch.setattr(asyncio, "to_thread", _never_executor)
    async def exercise():
        await shadow.start_candidate_shadow(output_dir=tmp_path, bindings=BINDINGS)
        result = await shadow.stop_candidate_shadow()
        assert result["status"] == "stopped"
        assert result["drain_complete"] and not result["drain_incomplete"]
        assert not result["worker_alive"] and not result["stop_timed_out"]
        assert result["watermarks"]["durable"] is not None
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["low", "query_error", "write_would_cross_reserve"])
def test_disk_reserve_stops_only_observer_without_touching_existing_files(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    from app.paper import research_reports
    sentinel = tmp_path / "existing-business-or-research-file"
    sentinel.write_bytes(b"untouched")
    r = runtime(tmp_path / "new-observer-output")
    r.frame(frame())
    before = len(r.batch)
    assert r.pending
    monkeypatch.setattr(shadow, "_runtime", r)
    def stats(path):
        if mode == "query_error":
            raise OSError("synthetic statvfs failure")
        available = shadow.MIN_DISK_RESERVE_BYTES + (1 if mode == "write_would_cross_reserve" else -1)
        return SimpleNamespace(f_bavail=available, f_frsize=1)
    monkeypatch.setattr(shadow.os, "statvfs", stats)
    def forbidden(*args):
        raise AssertionError("must not publish after disk budget rejection")
    monkeypatch.setattr(shadow, "_publish_shadow", forbidden)
    r.flush()
    status = r.status()
    assert not status["active"] and r.stopping.is_set()
    assert status["disk_budget"]["blocked"]
    assert status["disk_budget"]["reason"] == (
        "disk_space_query_failed" if mode == "query_error" else "disk_reserve_insufficient")
    assert r.counts["disk_budget"] == 1
    assert r.counts["disk_budget_undurable_records"] == before
    assert r.counts["disk_budget_censored_anchors"] == 1
    assert r.counts["disk_budget_censored_horizons"] == 3
    assert status["watermarks"]["durable"] is None
    shadow.capture_frame(object())
    assert r.queue.empty()
    # Budget trip is latched; even later apparent recovery cannot restart writes.
    monkeypatch.setattr(shadow.os, "statvfs", lambda p: (_ for _ in ()).throw(AssertionError("latched")))
    r.boundary()
    r.flush()
    assert r.counts["disk_budget"] == 1
    assert sentinel.read_bytes() == b"untouched"
    assert not r.directory.exists()


def test_disk_reserve_real_worker_exits_unknown_without_writing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(shadow, "_runtime", None)
    monkeypatch.setattr(shadow.os, "statvfs", lambda p: SimpleNamespace(f_bavail=0, f_frsize=4096))
    async def exercise():
        await shadow.start_candidate_shadow(output_dir=tmp_path / "absent", bindings=BINDINGS)
        status = await shadow.stop_candidate_shadow()
        assert not status["worker_alive"]
        assert status["status"] == "stopped_incomplete"
        assert status["disk_budget"]["blocked"] and status["drain_incomplete"]
        assert status["counts"]["undurable_records"] >= 1
        assert not (tmp_path / "absent").exists()
    asyncio.run(exercise())


@pytest.mark.parametrize("route", ["E", "E2"])
@pytest.mark.parametrize("interruption", [None, "quote_missing", "quote_conflict"])
def test_real_primary_metadata_preserves_reseal_but_quote_failures_break_it(
        tmp_path, monkeypatch, route, interruption):
    from types import SimpleNamespace
    from app.api.v1 import paper as producer
    current = [START]
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]
    monkeypatch.setattr(producer, "datetime", FrozenDatetime)
    monkeypatch.setattr(shadow, "_now", lambda: current[0])
    monkeypatch.setattr(producer.settings, "PAPER_CANDIDATE_SHADOW_ENABLED", True)
    name = core.STRATEGY_CANDIDATE_ACCOUNTS[route]
    r = runtime(tmp_path, {name: {"account_id": 3, "strategy_version": producer._strategy_version(name)}})
    monkeypatch.setattr(shadow, "_runtime", r)
    account = SimpleNamespace(id=3, account_name=name)
    candidate = {"code": "600001", "_source": "tenbagger_midline"}
    def send(sec, stage, source_sec=None, price=10.7):
        current[0] = START+timedelta(seconds=sec)
        spot = None
        if source_sec is not None:
            leaves = quote(source_sec, price=price, ask1_price=price, limit_up=11,
                           updated_at=START+timedelta(seconds=source_sec), quote_round_id=str(source_sec))
            leaves.pop("observed_at")
            spot = SimpleNamespace(**leaves)
        producer._capture_primary_candidate_shadow(
            account=account, candidate=candidate, spot=spot, run_id=f"round-{int(sec//30)}",
            stage=stage, original_candidate=True,
            original_gate=True if source_sec is not None else None,
            original_confirmed=True if stage == "strategy_confirmed" else None)
        r.frame(r.queue.get_nowait()[1])
    send(1, "candidate_observed")
    send(2, "candidate_quote", 0)
    send(3, "strategy_confirmed", 0)
    if interruption == "quote_missing":
        send(15, "quote_missing")  # Real missing quote, not candidate enumeration.
    elif interruption == "quote_conflict":
        send(15, "candidate_quote", 0, price=10.6)  # Same source changed price.
    send(31, "candidate_observed")
    send(32, "candidate_quote", 30, price=10.99)
    send(33, "strategy_confirmed", 30, price=10.99)
    frames = [row for row in records(r) if row["kind"] == "frame"]
    metadata = [row for row in frames if row["capture_input"]["stage"] == "candidate_observed"]
    assert len(metadata) == 2
    assert all(row["capture_input"]["quote"] == {} for row in metadata)
    assert all(row["result"]["candidate"]["value"] is None for row in metadata)
    assert r.counts["metadata_quote_absent_preserved"] == 2
    final = frames[-1]
    assert len({row["candidate_id"] for row in frames}) == 1
    assert final["result"]["baseline"]["value"] is True
    if interruption is None:
        assert len(final["sample_inputs"]) == 2
        assert final["result"]["candidate"]["value"] is True
    else:
        assert len(final["sample_inputs"]) == 1
        assert final["result"]["candidate"]["value"] is not True


def test_lightweight_rows_expose_real_quote_capture_and_emit_clocks(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    monkeypatch.setattr(shadow, "_runtime", r)
    monkeypatch.setattr(shadow, "_now", lambda: START+timedelta(seconds=5))
    r.frame(frame(2, quote=quote(), producer_reported_at=START))
    r.frame(frame(3, code="", quote={}, stage="scan_completed",
                  gate_inputs={"candidate_count": 1}))
    r.flush()
    result = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path)
    row = result["rows"][0]
    assert row["source_quote_at"] == START.isoformat()
    assert row["observed_at"] == row["predicate_asof"] == (START+timedelta(seconds=2)).isoformat()
    assert row["recorded_at"] == (START+timedelta(seconds=5)).isoformat()
    assert row["reference_price"] == 10.3
    receipt = result["coverage"]["recent_receipts"][0]
    assert receipt["counts"] == receipt["coverage_counts"] == {"candidate_count": 1}
    assert result["status"]["session_id"] == r.session_id
    assert result["status_scope"] == "current_process_not_historical_day"
    assert result["coverage"]["status"] == "observed"
    assert "full_day_denominator_unknown" in result["coverage"]["reason"]
    limited = shadow.read_candidate_shadow_report(trade_date=START.date(), limit=1, output_dir=tmp_path)
    assert limited["coverage"]["status"] == "partial"
    assert "row_or_record_limit" in limited["coverage"]["reason"]
    empty = shadow.read_candidate_shadow_report(trade_date=START.date(), output_dir=tmp_path / "absent")
    assert empty["coverage"]["status"] == "unknown"
    assert "no_matching_evidence_received" in empty["coverage"]["reason"]


@pytest.mark.parametrize("route", ["A2", "B2", "C2", "D2", "F2", "C3"])
def test_actual_producers_keep_proven_confirmation_on_followup(tmp_path, monkeypatch, route):
    from app.paper import strategy_iteration_shadow as generic
    from app.paper import momentum_retest_shadow as momentum
    from test_momentum_retest_shadow import _policy
    account = core.STRATEGY_CANDIDATE_ACCOUNTS[route]
    bindings = {account: {"account_id": 7, "strategy_version": "exec"}} if account else {}
    r = runtime(tmp_path, bindings)
    clock = [START]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(shadow, "_runtime", r)
    monkeypatch.setattr(shadow, "_now", lambda: clock[0])
    monkeypatch.setattr(generic, "datetime", Clock)
    monkeypatch.setattr(momentum, "datetime", Clock)
    routes = {"B2": generic.ROUTE_B, "C2": generic.ROUTE_C, "D2": generic.ROUTE_D,
              "F2": generic.ROUTE_F2, "C3": generic.ROUTE_C3}
    seen = []
    for seconds in (0, 31):
        clock[0] = START + timedelta(seconds=seconds)
        stage = "confirmed" if not seconds else "observation" if route == "A2" else "static_gate"
        if route == "A2":
            momentum._capture_momentum_projection(_policy(), quote(seconds), clock[0],
                stage=stage, reason="confirmed_membership", candidate=True, confirmed=True, gate=True)
        else:
            generic._capture_candidate_projection(routes[route], clock[0], code="600001",
                quote=quote(seconds), stage=stage, reason="confirmed_membership",
                original_candidate=True, original_confirmed=True, original_gate=True,
                rules={}, version="source-v1")
        packet = r.queue.get_nowait()[1]
        assert ("original_confirmed_at" in packet["identities"]) is (seconds == 0)
        r.frame(packet)
        seen.append(deepcopy([row for row in records(r) if row["kind"] == "frame"][-1]))
    assert all(row["result"]["baseline"]["value"] is True for row in seen)
    assert seen[1]["candidate_input"]["original_confirmed_at"] == START.isoformat()
    assert seen[0]["result"]["orders_created"] == seen[1]["result"]["pushes_created"] == 0


@pytest.mark.parametrize("reset", [
    {"original_confirmed": False},
    {"stage": "reset", "original_confirmed": None},
    {"stage": "reset", "original_confirmed": True},
])
def test_proven_confirmation_clears_on_false_or_reset(tmp_path, reset):
    r = runtime(tmp_path)
    r.frame(frame(original_confirmed=True, identities={"original_confirmed_at": START.isoformat()}))
    assert records(r)[-1]["result"]["baseline"]["value"] is True
    r.frame(frame(30, **reset))
    assert list(r.streams.values())[0]["baseline_at"] is None
    r.frame(frame(60, original_confirmed=True))
    assert [row for row in records(r) if row["kind"] == "frame"][-1]["result"]["baseline"]["value"] is None


@pytest.mark.parametrize("change", [
    {"scan_id": "different-episode"}, {"production_version": "source-v2"},
    {"identities": {"round_id": "new-round"}},
])
def test_confirmation_proof_never_crosses_episode_or_version(tmp_path, change):
    r = runtime(tmp_path)
    r.frame(frame(original_confirmed=True, identities={"original_confirmed_at": START.isoformat()}))
    r.frame(frame(30, original_confirmed=True, **change))
    assert [row for row in records(r) if row["kind"] == "frame"][-1]["result"]["baseline"]["value"] is None


@pytest.mark.parametrize("clock_value", [
    None, "bad-clock", (START + timedelta(seconds=15)).isoformat(),
    (START - timedelta(seconds=1)).isoformat(), (START - timedelta(days=1)).isoformat(),
])
def test_unproven_confirmation_clock_cannot_mature_without_new_evidence(tmp_path, clock_value):
    r = runtime(tmp_path)
    r.frame(frame(original_confirmed=True, identities={"original_confirmed_at": clock_value}))
    assert records(r)[0]["result"]["baseline"]["value"] is None
    r.frame(frame(30, original_confirmed=True))
    assert [row for row in records(r) if row["kind"] == "frame"][-1]["result"]["baseline"]["value"] is None


def test_explicit_invalid_confirmation_replaces_old_proof_with_unknown(tmp_path):
    r = runtime(tmp_path)
    r.frame(frame(original_confirmed=True, identities={"original_confirmed_at": START.isoformat()}))
    r.frame(frame(30, original_confirmed=True,
                  identities={"original_confirmed_at": (START + timedelta(seconds=60)).isoformat()}))
    assert [row for row in records(r) if row["kind"] == "frame"][-1]["result"]["baseline"]["value"] is None
    r.frame(frame(90, original_confirmed=True))
    assert [row for row in records(r) if row["kind"] == "frame"][-1]["result"]["baseline"]["value"] is None


def test_import_call_surface_contains_no_business_execution():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(shadow.__file__).read_text())
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert all(not any(s in module for s in ("trading", "push", "portfolio", "db", "models"))
               for module in imports)
