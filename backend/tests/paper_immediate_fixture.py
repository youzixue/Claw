"""Explicit local quote fixture for non-quote tests that actually book a fill.

Never autouse: individual positive cases opt in. No production connection, network,
identity/risk override or execution validator mock is provided here.
"""
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from app.api.v1 import paper
from app.models.governance import TradeCalendarModel
from app.models.stock import QuoteRound, StockSpot


async def seed_immediate_quote(db, monkeypatch, *, code, at, price, side="buy", name=None):
    monkeypatch.setattr(paper, "_public_order_clock", lambda:at)
    monkeypatch.setattr(paper, "_paper_now", lambda:at)
    if await db.get(TradeCalendarModel, at.date()) is None:
        db.add(TradeCalendarModel(trade_date=at.date(),is_trade_day=True,session_type="full"))
    round_id=f"explicit-immediate:{code}:{at.isoformat()}"
    if await db.scalar(select(QuoteRound).where(QuoteRound.round_id==round_id)) is None:
        db.add(QuoteRound(round_id=round_id,source="tencent",trade_date=at.date(),
            as_of_at=at-timedelta(seconds=3),committed_at=at-timedelta(seconds=1),
            expected_count=1,received_count=1,source_time_count=1,
            coverage=1,source_time_coverage=1,quality_status="ok",
            config_version="isolated-fixture",code_version="isolated-fixture"))
    spot=await db.get(StockSpot,code)
    if spot is None:
        spot=StockSpot(code=code,name=name)
        db.add(spot)
    spot.price=price
    spot.limit_down=round(price*.8,2)
    spot.limit_up=round(price*1.2,2)
    spot.ask1_price=price if side=="buy" else round(price+.01,2)
    spot.bid1_price=round(price-.01,2) if side=="buy" else price
    spot.ask1_volume=spot.bid1_volume=100
    spot.source_quote_at=at-timedelta(seconds=3)
    spot.received_at=at-timedelta(seconds=2)
    spot.updated_at=at-timedelta(seconds=1)
    # Equal fixture timestamps must still be sent in UPDATE, not ORM onupdate=now.
    flag_modified(spot,"updated_at")
    spot.quote_round_id=round_id
    await db.commit()
    return round_id
