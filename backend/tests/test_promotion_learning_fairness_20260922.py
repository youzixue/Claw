"""Large learned history must cooperate without changing sample identities."""
import asyncio
from datetime import datetime

import pytest

from app.api.v1 import promotion as p
from app.models.signal import PromotionPredictionRecord
from test_promotion_learning_io_20260922 import db, seed, DAY


@pytest.mark.asyncio
async def test_learning_decodes_selected_factors_once_and_cooperates(db, monkeypatch):
    await seed(db, source="page")
    for i in range(520):
        db.add(PromotionPredictionRecord(
            code=f"60{i+100:04d}", target_board=1, prediction_trade_date=DAY,
            predicted_probability=.5, calibrated_probability=.5,
            outcome_status="failed", candidate_route="mainline_spread_start",
            learning_bucket="T1:mainline_spread_start", model_version=p.PROMOTION_MODEL_VERSION,
            snapshot_source="page", snapshot_context="page",
            snapshot_recorded_at=datetime(2026, 8, 13, 20),
            factors_json=p._json_dumps_safe({
                "promotion_event_label_version": p.PROMOTION_LABEL_VERSION,
                "prediction_ranked_selected": True, "learning_eligible": True,
            })))
    await db.commit()
    count = 0
    ticks = 0
    seen = []
    original = p._json_loads_safe
    def counted(value):
        nonlocal count
        count += 1
        seen.append(ticks)
        return original(value)
    monkeypatch.setattr(p, "_json_loads_safe", counted)
    done = False
    async def heartbeat():
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)
    pulse = asyncio.create_task(heartbeat())
    try:
        stats = await p._load_promotion_learning_stats(db)
    finally:
        done = True
        await pulse
    assert stats["T1:mainline_spread_start"]["sample_count"] == 524
    assert count <= 530, "same large factors JSON is decoded in multiple learning passes"
    assert len(set(seen)) >= 3, "learning CPU work did not yield between bounded chunks"


class Stream:
    def __init__(self, values, fail=False):
        self.values, self.fail, self.closed = values, fail, False
    def partitions(self, size):
        assert size == 256
        async def chunks():
            for offset in range(0, len(self.values), size):
                yield self.values[offset:offset+size]
                if self.fail:
                    raise asyncio.CancelledError()
        return chunks()
    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_record_cursor_is_bounded_and_closed_on_cancel(cancel):
    rows = Stream(list(range(770)), fail=cancel)
    class DB:
        async def stream_scalars(self, statement):
            assert statement.get_execution_options()["yield_per"] == 256
            return rows
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await p._load_promotion_learning_records(DB(), DAY)
    else:
        assert await p._load_promotion_learning_records(DB(), DAY) == list(range(770))
    assert rows.closed
