"""C3 must reuse canonical stock taxonomy; no relaxed entry/continuity/TTL."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.stock_tagger import stock_tagger
from app.models.paper import PaperShadowEvent
from app.models.stock import StockTag
from app.push import paper_buy_points as points
from test_c3_research_push_20260922 import env, seed, receipt


async def seed_code(maker, at, code, *, stored_type=None):
    key, _ = await seed(maker, at)
    async with maker() as db:
        tag = await db.scalar(select(StockTag))
        tag.code = code
        tag.board_type = stored_type or stock_tagger.get_board_type(code)
        tag.board_tag = "tradeable"  # An erroneous tradeable tag cannot authorize other boards.
        events = (await db.scalars(select(PaperShadowEvent))).all()
        for event in events:
            if event.code == "600001":
                event.code = code
                event.event_key = event.event_key.replace("600001", code)
            event.snapshot_json = event.snapshot_json.replace('"600001"', json.dumps(code))
        await db.commit()
    return key.replace("600001", code)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["600001", "601001", "603001", "605001", "000001", "001001", "002001", "003001"])
async def test_original_mainboard_prefixes_use_canonical_tag_and_full_current_pool(env, code):
    maker, clock, send = env
    key = await seed_code(maker, clock[0], code)
    result = await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert result["status"] == "sent", result
    assert send.call_count == 1
    assert (await receipt(maker, key, clock[0]))["status"] == "sent"
    assert "仅研究观察" in send.call_args.args[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("code,stored_type", [
    ("002001", "main_sz"), ("600001", "main_sz"), ("003001", "main_sh"),
    ("600001", "sme"), ("002001", "unknown"),
])
async def test_conflicting_taxonomy_is_not_made_valid_by_tradeable_label(env, code, stored_type):
    maker, clock, send = env
    key = await seed_code(maker, clock[0], code, stored_type=stored_type)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert not send.called
    r = await receipt(maker, key, clock[0])
    assert r["status"] == "rejected" and r["cause"] == "stock_identity_not_tradeable"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["300001", "301001", "688001", "689001", "920001", "999001", "00201", "302001"])
async def test_no_board_expansion_even_if_stored_tag_says_tradeable(env, code):
    maker, clock, send = env
    key = await seed_code(maker, clock[0], code)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert not send.called
    assert (await receipt(maker, key, clock[0]))["status"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["is_st", "is_suspended", "is_delisting", "is_ipo_recent"])
async def test_canonical_sme_does_not_bypass_security_restrictions(env, flag):
    maker, clock, send = env
    key = await seed_code(maker, clock[0], "002001")
    async with maker() as db:
        tag = await db.get(StockTag, "002001")
        setattr(tag, flag, True)
        await db.commit()
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert not send.called
    assert (await receipt(maker, key, clock[0]))["status"] == "rejected"


@pytest.mark.asyncio
async def test_sme_unknown_pool_waits_then_original_ttl_expires(env):
    maker, clock, send = env
    original = clock[0]
    key = await seed_code(maker, original, "002001")
    async with maker() as db:
        frames = (await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.event_type == "universe_audit"))).all()
        for frame in frames:
            await db.delete(frame)
        await db.commit()
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert not send.called
    assert (await receipt(maker, key, clock[0]))["status"] == "waiting"
    clock[0] += timedelta(seconds=181)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert not send.called
    r = await receipt(maker, key, clock[0])
    assert r["status"] == "expired" and r["signal_observed_at"] == original.isoformat()
