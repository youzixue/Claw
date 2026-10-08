"""Impact-map keeps the original holding snapshot across news-owner rollback."""
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import news as api
from app.models.news import FinanceNews, NewsAnalysisVersion, NewsContentVersion
from app.models.paper import PaperAccount, PaperPosition
from app.models.stock import StockSpot
from app.news.engine import NewsEngine
from test_news_evidence_versions import item


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "impact.db"))
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        for model in (FinanceNews, NewsContentVersion, NewsAnalysisVersion,
                      StockSpot, PaperAccount, PaperPosition):
            await conn.run_sync(model.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
        db.add(StockSpot(code="600001", name="测试股份"))
        db.add(PaperPosition(account_id=1, code="600001", name="持仓原名",
                             buy_price=10, buy_amount=100, buy_time=datetime(2026, 9, 21, 10),
                             current_price=11, profit_pct=10, is_closed=False))
        await db.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_article", [True, False])
async def test_impact_map_preserves_original_holdings_through_raw_refresh(sessions, monkeypatch, bad_article):
    engine = NewsEngine()
    original_save = engine._save_raw_to_db
    seen = []

    async def save(db, raw, **kwargs):
        seen.append(raw.source_id)
        version = await original_save(db, raw, **kwargs)
        if raw.source_id == "bad" and bad_article:
            raise ValueError("article failure after real flush")
        return version

    async def fetch(**kwargs):
        # Called after impact-map has loaded holdings; a later database change
        # must not silently change the response to a newer holding snapshot.
        async with sessions() as writer:
            await writer.execute(update(PaperPosition).values(current_price=12, profit_pct=20))
            await writer.commit()
        return [item(source_id=name) for name in ("first", "bad", "last")]

    async def window(db):
        return {"latest_trade_date": datetime(2026, 9, 21).date(), "since": None,
                "is_gap_window": False, "label": "isolated"}

    monkeypatch.setattr(engine, "_save_raw_to_db", save)
    monkeypatch.setattr(engine, "fetch_all", fetch)
    monkeypatch.setattr(api, "news_engine", engine)
    monkeypatch.setattr(api, "_news_decision_window", window)
    async with sessions() as db:
        result = await api.news_impact_map(limit=120, major_only=False, refresh=True, db=db)
    assert seen == ["first", "bad", "last"]
    holding = result["holdings"][0]
    assert {key: holding[key] for key in ("code", "name", "buy_price", "current_price", "profit_pct")} == {
        "code": "600001", "name": "持仓原名", "buy_price": 10,
        "current_price": 11, "profit_pct": 10,
    }
    assert len(result["holdings"]) == 1
    async with sessions() as db:
        expected = {"first", "last"} if bad_article else {"first", "bad", "last"}
        assert set(await db.scalars(select(FinanceNews.source_id))) == expected
        assert await db.scalar(select(func.count()).select_from(NewsContentVersion)) == len(expected)
        assert await db.scalar(select(func.count()).select_from(NewsAnalysisVersion)) == 0
        assert await db.scalar(select(PaperPosition.current_price)) == 12
