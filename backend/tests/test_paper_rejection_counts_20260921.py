"""Repeated diagnostic reasons must never abort B/C/D candidate filtering."""
import json
from datetime import date, datetime, timedelta

import pytest
from app.api.v1 import paper
from app.models.stock import StockSpot
from test_paper_api import (
    paper_client, _governed_promotion_run, _governed_promotion_snapshot,
)

DAY = date(2026, 9, 21)
AT = datetime(2026, 9, 21, 9, 40)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["promotion", "mainline", "auction"])
@pytest.mark.parametrize("reason", ["行情缺失", "缩量"])
async def test_repeated_rejections_keep_counts_and_later_valid_candidate(
    paper_client, monkeypatch, account, reason,
):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        DAY - timedelta(days=i): (DAY - timedelta(days=i)).weekday() < 5
        for i in range(10)
    })
    cfg = paper.PAPER_PROMOTION_ACCOUNTS[account]
    async with maker() as db:
        run = _governed_promotion_run(
            run_key=f"repeated-{account}-{reason}", reference_trade_date=DAY,
            snapshot_context="promotion_0935", as_of_at=AT,
        )
        db.add(run)
        await db.flush()
        for i in range(4):
            code = f"6000{10+i:02d}"
            db.add(_governed_promotion_snapshot(
                run_id=run.id, record_key=code, code=code,
                prediction_trade_date=DAY, target_board=cfg["target_board"],
                route=cfg["route"], probability=0.95, created_at=AT,
            ))
            if i < 2 and reason == "行情缺失":
                continue
            db.add(StockSpot(
                code=code, name=code, price=10.15, prev_close=10,
                limit_up=11, change_pct=1.5,
                volume_ratio=0.3 if i < 2 else 1.6,
            ))
        await db.commit()
        candidates, notes = await paper._promotion_route_buy_candidates(
            db, limit=10, trade_date=DAY, account_name=account,
        )
    assert {c["code"] for c in candidates} == {"600012", "600013"}, notes
    assert all(c["execution_confirmation"] is True for c in candidates)
    summary = next(note for note in notes if "已过滤" in note)
    assert "已过滤2只" in summary and f"{reason}×2" in summary
    assert not any("候选均未通过" in note for note in notes)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,include_valid", [(2, False), (5, True)])
async def test_mainline_repeated_structure_rejections_preserve_real_gate(
    paper_client, monkeypatch, count, include_valid,
):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        DAY - timedelta(days=i): (DAY - timedelta(days=i)).weekday() < 5
        for i in range(10)
    })
    # Freeze this regression's existing policy; do not replace the decision function.
    monkeypatch.setattr(paper.settings, "PAPER_MAINLINE_LIVE_CONFIRM_ENABLED", True)
    monkeypatch.setattr(paper.settings, "PAPER_MAINLINE_LIVE_CONFIRM_MIN_STRICT_CONFIRMATIONS", 2)
    async with maker() as db:
        run = _governed_promotion_run(
            run_key=f"repeated-mainline-{count}", reference_trade_date=DAY,
            snapshot_context="promotion_0935", as_of_at=AT,
        )
        db.add(run)
        await db.flush()
        for i in range(count + int(include_valid)):
            code = f"6001{i:02d}"
            snapshot = _governed_promotion_snapshot(
                run_id=run.id, record_key=code, code=code, prediction_trade_date=DAY,
                target_board=1, route="mainline_spread_start", probability=0.95,
                actionable=(i == count), created_at=AT,
            )
            snapshot.rank_scope = "recall_ranked"
            snapshot.features_json = json.dumps({
                "strict_confirmation_count": 1,
                "broad_rotation_member_setup": True, "sector_catalyst_spread": True,
                "sector_strength_score": 62,
            })
            db.add_all([snapshot, StockSpot(
                code=code, name=code, price=10.15, prev_close=10, limit_up=11,
                change_pct=1.5, volume_ratio=1.6,
            )])
        await db.commit()
        candidates, notes = await paper._promotion_route_buy_candidates(
            db, limit=10, trade_date=DAY, account_name="mainline",
        )
    assert [c["code"] for c in candidates] == ([f"6001{count:02d}"] if include_valid else []), notes
    summary = next(note for note in notes if "已过滤" in note)
    assert f"已过滤{count}只" in summary
    assert f"主线独立确认:结构确认1项，低于2项×{count}" in summary
    assert len([note for note in notes if note.startswith("600")]) == min(count, 3)
    assert any("候选均未通过" in note for note in notes) is (not include_valid)
