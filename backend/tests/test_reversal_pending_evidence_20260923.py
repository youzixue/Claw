"""F live invalidation must dominate a separate missing historical input."""
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import paper
from app.models.stock import LimitUpPool, StockTag
from test_paper_deferred_exit_provenance import memory_session
from test_pending_buy_validity import AT, quote


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_only","high_change","blocked_identity","near_limit",
    "missing_prev_high_change","missing_change_st","old_date_unknown"])
async def test_history_unknown_does_not_mask_independent_live_failure(memory_session,monkeypatch,case):
    db=memory_session
    day=AT.date()-timedelta(days=1)
    monkeypatch.setattr(paper.settings,"PAPER_REVERSAL_ENABLED",True)
    monkeypatch.setattr(paper.trade_calendar,"previous_trade_day",AsyncMock(return_value=day))
    fields={}
    if case in {"high_change","missing_prev_high_change"}:
        fields["change_pct"]=paper.settings.PAPER_REVERSAL_MAX_INTRADAY_CONFIRM_CHANGE_PCT+1
    if case in {"missing_prev_high_change","old_date_unknown"}: fields["prev_close"]=None
    if case=="missing_change_st": fields["change_pct"]=None
    if case=="near_limit": fields.update(price=32.96,high=32.97,avg_price=32.8,limit_up=33.)
    current=quote(AT,**fields)
    monkeypatch.setattr(paper,"_spot_by_code",AsyncMock(return_value=current))
    db.add(LimitUpPool(code="002988",name="isolated",trade_date=day,quarantined=False))
    if case in {"blocked_identity","missing_change_st"}:
        db.add(StockTag(code="002988",board_type="main_sz",board_tag="blocked",is_st=True))
    await db.commit()
    status,reason=await paper._pending_primary_buy_confirmation(db,account_name="reversal",
        source="reversal_pullback",candidate={"code":"002988","_source":"reversal_pullback","signal_date":(day-timedelta(days=1) if case=="old_date_unknown" else day).isoformat()},
        spot=current,limit_price=current.price,now=AT)
    assert status==("waiting" if case=="missing_only" else "canceled"), (status,reason)
