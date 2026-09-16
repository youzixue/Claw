"""New position observations are excluded by actual existing signal consumers."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.models.paper import PaperShadowEvaluation, PaperShadowEvent
from app.paper.position_observation import append_position_frames, ROUTE_ID, append_deferred_execution_frames, EXECUTION_ROUTE_ID
from test_deferred_execution_observation import capture as execution_capture
from app.paper.strategy_iteration_shadow import settle_strategy_iteration_shadow
from app.paper.momentum_retest_shadow import settle_momentum_retest_shadow
from test_paper_api import paper_client
from test_paper_position_observation import capture, AT, wall_clock


@pytest.mark.asyncio
async def test_existing_settlement_readers_do_not_turn_position_frames_into_signals(paper_client,wall_clock):
    _,maker=paper_client
    async with maker() as db:
        await append_position_frames(db,[(capture(),{"available_sell_amount":100,
            "exit_execution_status":"filled","exit_order_id":"not-a-signal"})])
        await append_deferred_execution_frames(db,[execution_capture()])
        await db.commit()
        for route in (ROUTE_ID,EXECUTION_ROUTE_ID):
            assert await db.scalar(select(PaperShadowEvent.id).where(PaperShadowEvent.route_id==route)) is not None
        for settle in (settle_strategy_iteration_shadow,settle_momentum_retest_shadow):
            assert await settle(db,as_of_date=AT.date()+timedelta(days=4))=={"signals":0,"evaluations_added":0}
        assert (await db.scalars(select(PaperShadowEvaluation))).all()==[]
