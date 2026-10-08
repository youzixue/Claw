"""Real end-boundary brackets, never a synthetic 14:30 observation."""
from datetime import date, datetime, timedelta

import pytest
from app.config.settings import settings
from app.paper import strategy_iteration_shadow as shadow

DAY=date(2026,9,29)
END=datetime(2026,9,29,14,30)


def times(start, end, step=30):
    values=[]
    while start<=end:
        values.append(start)
        start+=timedelta(seconds=step)
    return values


def dense_frames():
    # Real polling runs at :24 and :54, never exactly :00.
    return times(datetime(2026,9,29,9,30,24),datetime(2026,9,29,11,29,54))+times(
        datetime(2026,9,29,13,0,24),END-timedelta(seconds=6))


def test_first_real_post_cutoff_frame_proves_boundary_without_becoming_confirmation():
    frames=dense_frames()
    legacy=shadow._first_board_session_observation_health(frames,DAY)
    assert legacy["complete"] is False
    assert legacy["checks"]["covered_route_end"] is False
    result=shadow._first_board_session_observation_health(frames+[END+timedelta(seconds=24)],DAY)
    assert result["complete"] is True
    assert result["last_frame_at"]=="2026-09-29T14:29:54"
    assert result["route_end_coverage_at"]=="2026-09-29T14:30:24"
    assert result["route_end_bracket_gap_sec"]==30
    assert result["afternoon_frame_count"]==legacy["afternoon_frame_count"]
    assert not shadow._first_board_confirmation_time(END+timedelta(seconds=24))


def test_exact_cutoff_still_works():
    result=shadow._first_board_session_observation_health(dense_frames()+[END],DAY)
    assert result["complete"] is True
    assert result["route_end_coverage_at"]==END.isoformat()


@pytest.mark.parametrize("offset",[175,181,1800])
def test_late_recovery_cannot_bridge_missing_route_end(offset):
    result=shadow._first_board_session_observation_health(dense_frames()+[END+timedelta(seconds=offset)],DAY)
    assert result["checks"]["covered_route_end"] is False
    assert result["complete"] is False


def test_gap_boundary_inclusive_not_relaxed():
    result=shadow._first_board_session_observation_health(dense_frames()+[END+timedelta(seconds=174)],DAY)
    assert result["route_end_bracket_gap_sec"]==180
    assert result["complete"] is True


def test_missing_afternoon_or_morning_not_rescued_by_bracket():
    frames=dense_frames()
    for incomplete in ([f for f in frames if f.hour<12],
                       [f for f in frames if f.hour>=13],
                       [f for f in frames if not datetime(2026,9,29,13,30)<=f<=datetime(2026,9,29,13,35)]):
        assert not shadow._first_board_session_observation_health(incomplete+[END+timedelta(seconds=24)],DAY)["complete"]


def test_post_cutoff_frames_do_not_inflate_minimum_counts(monkeypatch):
    frames=dense_frames()
    afternoon=sum(f.hour>=13 for f in frames)
    monkeypatch.setattr(settings,"PAPER_FIRST_BOARD_SHADOW_MIN_AFTERNOON_FRAMES",afternoon+1)
    result=shadow._first_board_session_observation_health(frames+times(END+timedelta(seconds=24),END+timedelta(minutes=20)),DAY)
    assert result["checks"]["covered_route_end"] is True
    assert result["checks"]["enough_afternoon_frames"] is False
    assert result["complete"] is False


def test_post_cutoff_cannot_pad_total_sample_floor(monkeypatch):
    frames=dense_frames()
    monkeypatch.setattr(settings,"PAPER_FIRST_BOARD_SHADOW_MIN_SESSION_FRAMES",len(frames)+1)
    result=shadow._first_board_session_observation_health(
        frames+times(END+timedelta(seconds=24),END+timedelta(minutes=20)),DAY)
    assert result["frame_count"]==len(frames)
    assert result["provided_frame_count"]>len(frames)
    assert result["checks"]["enough_frames"] is False
    assert result["complete"] is False


def test_wrong_day_outside_market_and_duplicate_frames_do_not_supply_endpoint():
    frames=dense_frames()
    for extra in ([END+timedelta(days=1)], [END+timedelta(hours=2)], [frames[-1]]*100):
        assert not shadow._first_board_session_observation_health(frames+extra,DAY)["complete"]


def test_repair_rotates_only_c3_contract(monkeypatch):
    original=shadow._rules
    current=shadow.route_version_for(shadow.ROUTE_C3)
    generic={r:shadow.route_version_for(r) for r in (shadow.ROUTE_B,shadow.ROUTE_C,shadow.ROUTE_D,shadow.ROUTE_F2)}
    assert original(shadow.ROUTE_C3)["session_observation"]["coverage_semantics"]=="bracketed_route_end_v2"
    def legacy_rules(route):
        rules=original(route)
        if route==shadow.ROUTE_C3:
            rules["session_observation"].pop("coverage_semantics")
        return rules
    monkeypatch.setattr(shadow,"_rules",legacy_rules)
    assert current!=shadow.route_version_for(shadow.ROUTE_C3)
    assert generic=={r:shadow.route_version_for(r) for r in generic}
