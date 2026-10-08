"""Forward-only v2 runtime integration; temp files, frozen synthetic clocks."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.paper import account_policy
from app.paper import candidate_shadow as shadow
from app.paper import intraday_route_research as core
from test_candidate_shadow_runtime_20260924 import START, frame, quote


BINDINGS = {"challenger_c": {"account_id": 9, "strategy_version": "execution-c2"}}


def packet(seconds=0, **changes):
    result = frame(seconds, route="C2", account_id=9, account_name="challenger_c",
                   original_confirmed=True, relative_strength_pct=1,
                   quote=quote(seconds, price=round(10.2+seconds/300, 2), avg_price=10.1),
                   identities={"original_confirmed_at": START.isoformat()})
    result.update(changes)
    return result


def runtime(tmp_path, monkeypatch, ttl=45, startup=None):
    monkeypatch.setattr(shadow, "_now", lambda: startup or START)
    monkeypatch.setattr(account_policy, "challenger_execution_policy",
                        lambda route: {"max_execution_delay_sec": ttl})
    r = shadow._Runtime(tmp_path, BINDINGS)
    r.core, r.core_sha = core, shadow.SEALED_CORE_SHA256
    monkeypatch.setattr(shadow, "_now", lambda: START+timedelta(seconds=300))
    return r


def last(r):
    return [x for x in r.batch if x["kind"] == "frame"][-1]


def test_startup_reads_real_route_policy_once_into_owned_binding(tmp_path, monkeypatch):
    calls = []
    original = account_policy.challenger_execution_policy
    def policy(route):
        calls.append(route)
        return original(route)
    bindings = {name: {"account_id": n+1, "strategy_version": f"execution-{n}"}
                for n, name in enumerate(account_policy.ROUTE_ACCOUNT_NAMES.values())}
    monkeypatch.setattr(account_policy, "challenger_execution_policy", policy)
    monkeypatch.setattr(shadow, "_now", lambda: START-timedelta(days=1))
    r = shadow._Runtime(tmp_path, bindings)
    assert len(calls) == 5
    assert set(r.execution_contracts) == {"A2", "B2", "C2", "D2", "F2"}
    for route_id, name in account_policy.ROUTE_ACCOUNT_NAMES.items():
        saved = next(c for c in r.execution_contracts.values() if c["account_name"] == name)
        assert saved["max_execution_delay_sec"] == original(route_id)["max_execution_delay_sec"]
        assert saved["policy_observed_at"] == (START-timedelta(days=1)).isoformat()
    bindings["challenger_c"]["strategy_version"] = "mutated"
    assert r.bindings["challenger_c"]["strategy_version"] != "mutated"
    assert r.execution_contracts["C2"]["execution_strategy_version"] != "mutated"


def test_startup_no_db_or_network(tmp_path, monkeypatch):
    import socket
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncSession
    def forbidden(*args, **kwargs):
        raise AssertionError("startup attempted business IO")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.setattr(AsyncSession, "execute", forbidden)
    r = shadow._Runtime(tmp_path, BINDINGS)
    assert "C2" in r.execution_contracts


def test_previous_day_startup_can_serve_new_confirmation(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch, startup=START-timedelta(days=1))
    r.frame(packet())
    r.frame(packet(30))
    record = last(r)
    assert record["result"]["baseline"]["value"] is True
    assert record["result"]["candidate"]["value"] is False
    assert record["result"]["candidate_v2"]["value"] is True
    assert record["candidate_input"]["execution_strategy_version"] == "execution-c2"
    assert record["execution_contract"]["max_execution_delay_sec"] == 45
    assert record["result"]["confirmation_freshness"]["age_sec"] == 30


def test_same_source_cache_revalidates_ttl_each_frame_without_mutating_old_result(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch)
    r.frame(packet())
    r.frame(packet(30))
    old_record = deepcopy(last(r))
    for seconds in (31, 46):
        r.frame(packet(seconds, quote=quote(30, price=10.3, avg_price=10.1),
                       stage="observation", original_gate=None, original_confirmed=None,
                       identities={}))
        record = last(r)
        assert record["evaluation"]["mode"] == "same_source_stage_reuse"
        assert record["result"]["baseline"] == old_record["result"]["baseline"]
        assert record["result"]["candidate"] == old_record["result"]["candidate"]
        assert record["result"]["confirmation_freshness"]["age_sec"] == seconds
        assert record["result"]["confirmation_freshness"]["value"] is (seconds <= 45)
        assert record["result"]["candidate_v2"]["value"] is (seconds <= 45)
    earlier = [x for x in r.batch if x.get("frame_id") == old_record["frame_id"]][0]
    assert earlier == old_record
    assert r.counts["v2_evaluated"] == 2
    assert r.counts["v2_shape_reused"] == 2
    assert r.counts["v2_freshness_checked"] == 4


def test_original_policy_not_reread_per_frame(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(account_policy, "challenger_execution_policy",
                        lambda route: (_ for _ in ()).throw(AssertionError("reread live policy")))
    r.frame(packet())
    r.frame(packet(30))
    assert last(r)["execution_contract"]["max_execution_delay_sec"] == 45


def test_explicit_reset_cannot_revive_same_original_anchor(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch, ttl=600)
    r.frame(packet())
    r.frame(packet(30))
    r.frame(packet(35, stage="reset", original_confirmed=None, original_gate=None, identities={}))
    r.frame(packet(60))  # producer repeats old true and old anchor
    record = last(r)
    assert record["result"]["baseline"]["value"] is True  # historical v1 retained
    assert record["result"]["confirmation_freshness"]["value"] is False
    assert record["result"]["candidate_v2"]["value"] is False
    assert record["result"]["candidate_v2"]["reason"] == "original_confirmation_invalidated"
    r.frame(packet(90, identities={"original_confirmed_at": (START+timedelta(seconds=90)).isoformat()}))
    assert last(r)["result"]["confirmation_freshness"]["value"] is True


@pytest.mark.parametrize("ttl", [None, -1, 0, True, "90", float("inf")])
def test_bad_policy_snapshot_cannot_fall_back_to_research_180(tmp_path, monkeypatch, ttl):
    r = runtime(tmp_path, monkeypatch, ttl=ttl)
    assert "C2" not in r.execution_contracts
    r.frame(packet())
    assert last(r)["result"]["confirmation_freshness"]["value"] is None


def test_future_policy_snapshot_remains_unknown(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch, startup=START+timedelta(seconds=100))
    r.frame(packet())
    r.frame(packet(30))
    assert last(r)["result"]["candidate_v2"]["value"] is None


@pytest.mark.parametrize("changes", [
    {"account_id": 10}, {"account_name": "default"},
])
def test_bad_runtime_binding_does_not_get_v2_rescue(tmp_path, monkeypatch, changes):
    r = runtime(tmp_path, monkeypatch)
    r.frame(packet(**changes))
    record = last(r)
    assert record["result"]["candidate_v2"]["value"] is None
    assert record["result"]["confirmation_freshness"]["value"] is None


def test_c3_e2_no_invented_challenger_ttl(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch)
    assert "C3" not in r.execution_contracts and "E2" not in r.execution_contracts
    r.frame(packet(route="C3", account_id=None, account_name=None))
    assert last(r)["result"]["candidate_v2"]["value"] is None
    assert last(r)["result"]["confirmation_freshness"]["value"] is None


def test_reader_new_v2_and_legacy_records_unknown_without_recompute(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch)
    r.frame(packet())
    r.frame(packet(30))
    legacy = [x for x in r.batch if x["kind"] == "frame"][0]
    for key in ("candidate_v2", "confirmation_freshness", "research_v2_version"):
        legacy["result"].pop(key)
    r.flush()
    assert list((tmp_path/"2026-09-23").glob("*.json.gz"))
    def forbidden(*args, **kwargs):
        raise AssertionError("reader must not recompute history")
    monkeypatch.setattr(core, "evaluate_strategy_candidate_experiment_v2", forbidden)
    out = shadow.read_candidate_shadow_report(trade_date="2026-09-23", output_dir=tmp_path)
    rows = out["rows"]
    old = next(x for x in rows if x["predicate_asof"] == START.isoformat())
    new = next(x for x in rows if x["predicate_asof"] != START.isoformat())
    assert old["candidate_v2"]["value"] is None
    assert old["confirmation_freshness"]["reason"] == "legacy_record_freshness_unavailable"
    assert old["baseline"]["value"] is True
    assert new["candidate_v2"]["value"] is True
    assert new["candidate"]["value"] is False


def test_status_research_v2_keeps_storage_protocol_independent():
    status = shadow._status_contract()
    assert status["version"] == "research:strategy_candidate_experiment_20260924_v2"
    assert status["storage_format"] == "compact_json_gzip_level1_v1"


def test_cached_shapes_skip_both_full_evaluators_and_ttl_crossing_gets_new_label(tmp_path, monkeypatch):
    r = runtime(tmp_path, monkeypatch)
    calls = {"v1": 0, "v2": 0, "freshness": 0}
    for key, name in (("v1", "evaluate_strategy_candidate_experiment"),
                      ("v2", "evaluate_strategy_candidate_experiment_v2"),
                      ("freshness", "_experiment_v2_freshness")):
        original = getattr(core, name)
        def counted(*args, _original=original, _key=key, **kwargs):
            calls[_key] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(core, name, counted)
    r.frame(packet())
    r.frame(packet(30))
    confirmed = deepcopy(last(r))
    assert calls == {"v1": 2, "v2": 2, "freshness": 2}
    for seconds in (31, 45, 46, 47):
        r.frame(packet(seconds, quote=quote(30, price=10.3, avg_price=10.1),
                       stage="observation", original_gate=None, original_confirmed=None,
                       identities={}))
        record = last(r)
        assert record["evaluation"]["mode"] == "same_source_stage_reuse"
        sampling = record["label_sampling"]
        assert sampling["policy"] == "first_state_per_episode_v2_with_confirmation_freshness"
        if seconds <= 45:
            assert not sampling["selected"]
            assert sampling["anchor_frame_id"] == confirmed["frame_id"]
        elif seconds == 46:
            expired = deepcopy(record)
            assert sampling["selected"]
            assert sampling["anchor_frame_id"] == record["frame_id"]
            assert sampling["anchor_frame_id"] != confirmed["frame_id"]
            assert r.pending[record["frame_id"]]["anchor"] == START+timedelta(seconds=46)
        else:
            assert not sampling["selected"]
            assert sampling["anchor_frame_id"] == expired["frame_id"]
        assert record["result"]["candidate_v2"]["value"] is (seconds <= 45)
        assert record["result"]["baseline"]["value"] is True
    assert calls == {"v1": 2, "v2": 2, "freshness": 6}
    # Reset invalidates the cache rather than reviving the old true shape.
    r.frame(packet(50, stage="reset", original_confirmed=None, original_gate=None, identities={}))
    r.frame(packet(60))
    assert last(r)["result"]["candidate_v2"]["value"] is False
    assert last(r)["result"]["confirmation_freshness"]["reason"] == "original_confirmation_invalidated"
    assert calls["v1"] == calls["v2"] == 4


def test_policy_observed_clock_is_after_actual_read_not_runtime_start(tmp_path, monkeypatch):
    current = [START]
    monkeypatch.setattr(shadow, "_now", lambda: current[0])
    def policy(route):
        current[0] += timedelta(seconds=1)
        return {"max_execution_delay_sec": 45}
    monkeypatch.setattr(account_policy, "challenger_execution_policy", policy)
    r = shadow._Runtime(tmp_path, BINDINGS)
    assert r.started_at == START.isoformat()
    c2_read_completed = START+timedelta(seconds=3)
    assert r.execution_contracts["C2"]["policy_observed_at"] == c2_read_completed.isoformat()
    assert r.execution_contracts["C2"]["policy_observed_at"] != r.started_at
