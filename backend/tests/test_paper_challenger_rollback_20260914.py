"""Caller rollback tests only: mocked strategy gates/submit, real async ORM expiry.

No strategy/risk acceptance or fillability claim. Actual book/CAS tests are separate.
"""
from datetime import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, inspect

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog, PaperShadowEvent, PaperTradeLog
from app.paper import strategy_iteration_challenger as challenger
from app.push import paper_buy_points
from app.trading import service, paper_authorization as authorization
from test_strategy_iteration_challenger import challenger_env, _seed_confirmed, ROUTE_B
from test_paper_atomic_execution_20260914 import isolated_transaction_policy_and_calendar, AT


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_challenger_distinguishes_definite_rejection_from_uncertain_error(
    challenger_env, monkeypatch, uncertain,
):
    factory = challenger_env
    monkeypatch.setattr(settings, "PAPER_CHALLENGER_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(paper, "PAPER_CHALLENGER_ACCOUNT_BY_ROUTE",
                        {ROUTE_B: paper.PAPER_ACCOUNT_CHALLENGER_B})
    monkeypatch.setattr(paper, "_paper_order_window_status", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_execution_quote_status", lambda *a, **k: (True, ""))
    monkeypatch.setattr(challenger, "_route_entry_not_before", lambda *a: time(9, 30))
    monkeypatch.setattr(challenger, "_route_auto_order_enabled", lambda *a, **k: True)
    monkeypatch.setattr(challenger, "_conservative_entry_price", lambda **k: (10.06, ""))
    monkeypatch.setattr(challenger, "_live_route_confirmation_valid", lambda *a, **k: (True, ""))
    monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={
        "final_level": "pass", "warnings": [], "block_reasons": []}))
    monkeypatch.setattr(paper_buy_points, "record_buy_point", AsyncMock())
    retained_exit_logs = []
    async def exits(db, *, account, **kwargs):
        row = await paper._add_auto_log(
            db, account_id=account.id, run_id="fixture-prior-exit",
            trade_date=AT.date(), trigger="fixture-only", source=ROUTE_B,
            action="skip_sell", decision="blocked", reason="fixture definite prior exit rejection")
        await db.commit()
        retained_exit_logs.append(row)
        return [row]
    monkeypatch.setattr(paper, "_run_auto_sells", exits)
    attempts = []
    original_error = ValueError("fixture submit outcome requires reconciliation")
    authorization.mark_paper_execution_uncertain(original_error)
    async def submit(db, cmd):
        assert cmd.broker == "paper" and cmd.side == "buy" and cmd.quantity >= 100
        attempts.append(cmd.code)
        await db.commit()
        await db.rollback()
        # Explicit expiration is deterministic even if the preceding commit
        # ended the transaction before rollback. Equivalent to failed-book rollback.
        db.expire_all()
        assert all(inspect(row).expired for row in retained_exit_logs)
        if uncertain:
            raise original_error
        return {"order": {"status": "rejected", "error_message": "fixture definite rejection"},
                "fills": [], "risk": {"final_level": "pass"}}
    monkeypatch.setattr(service, "submit_order", submit)
    async with factory() as db:
        for code in ("600100", "600101"):
            await _seed_confirmed(db, code=code, route_id=ROUTE_B, now=AT)
        frozen = [tuple(row) for row in (await db.execute(
            select(*PaperShadowEvent.__table__.columns).order_by(PaperShadowEvent.id))).all()]
        if uncertain:
            with pytest.raises(ValueError) as caught:
                await challenger.run_strategy_iteration_challenger_accounts(db, now=AT)
            assert caught.value is original_error
            assert len(attempts) == 1  # no next event / fabricated rejection
        else:
            result = await challenger.run_strategy_iteration_challenger_accounts(db, now=AT)
            assert result["blocked"] == 2 and result["entries"] == result["sells"] == 0
            assert set(attempts) == {"600100", "600101"}
            assert all(not inspect(row).expired for row in retained_exit_logs)
    async with factory() as db:
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.trigger == "challenger-shadow-confirmed"))).all())
        assert len(logs) == (0 if uncertain else 2)
        assert all(row.action == "wait_buy" and row.decision == "wait"
                   and row.reason == "fixture definite rejection" for row in logs)
        assert not (await db.scalars(select(PaperTradeLog))).all()
        assert [tuple(row) for row in (await db.execute(
            select(*PaperShadowEvent.__table__.columns).order_by(PaperShadowEvent.id))).all()] == frozen
