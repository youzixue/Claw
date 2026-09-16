"""Opt-in local timing evidence for mechanics tests; no risk bypass or network."""
from sqlalchemy import select
from app.models.governance import TradeCalendarModel
from app.models.stock import QuoteRound


async def accepted_frame(db, payload):
    """Explicit local fixture evidence; never repair original source clocks."""
    day = payload["committed_at"].date()
    if await db.get(TradeCalendarModel, day) is None:
        db.add(TradeCalendarModel(trade_date=day, is_trade_day=True, session_type="full"))
    existing = await db.scalar(select(QuoteRound).where(QuoteRound.round_id == payload["round_id"]))
    if existing is None:
        db.add(QuoteRound(round_id=payload["round_id"], trade_date=day,
            source="tencent", quality_status=payload["quality_status"],
            expected_count=len(payload["records"]), received_count=len(payload["records"]),
            source_time_count=len(payload["records"]), coverage=1, source_time_coverage=1,
            committed_at=payload["committed_at"], as_of_at=payload["as_of_at"],
            config_version=payload["config_version"], code_version=payload["code_version"]))
    else:
        assert existing.committed_at == payload["committed_at"]
    await db.commit()
