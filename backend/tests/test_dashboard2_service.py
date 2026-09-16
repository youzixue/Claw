import asyncio
import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("DEBUG", "false")

from app.dashboard2.schemas import ExternalFactorItem, MappingInsightItem
from app.dashboard2.service import dashboard2_service


@pytest.mark.asyncio
async def test_external_factor_collection_does_not_block_event_loop(monkeypatch):
    def slow_collect():
        time.sleep(0.15)
        return []

    monkeypatch.setattr(
        "app.dashboard2.service.external_factor_collector.collect",
        slow_collect,
    )

    started = time.monotonic()
    collection = asyncio.create_task(dashboard2_service._collect_external_factors())
    await asyncio.sleep(0.02)
    heartbeat_elapsed = time.monotonic() - started
    await collection

    assert heartbeat_elapsed < 0.10


def test_build_conclusions_uses_chinese_cycle_note():
    factors = [
        ExternalFactorItem(key="us_nasdaq", label="纳斯达克", price=1, change_pct=1.2, trade_time="2026-04-14 10:00:00", market="US"),
        ExternalFactorItem(key="us_sp500", label="标普500", price=1, change_pct=0.8, trade_time="2026-04-14 10:00:00", market="US"),
        ExternalFactorItem(key="china_adr", label="中概/金龙", price=1, change_pct=0.6, trade_time="2026-04-14 10:00:00", market="US_CN"),
        ExternalFactorItem(key="a50", label="A50", price=1, change_pct=0.5, trade_time="2026-04-14 10:00:00", market="A50"),
    ]
    mappings = []
    ctx = {
        "sentiment": SimpleNamespace(sentiment_cycle="divergence"),
        "sentiment_state": SimpleNamespace(phase="divergence"),
    }

    conclusions = dashboard2_service._build_conclusions(factors, mappings, ctx)
    mood = next(x for x in conclusions if x.key == "a_share_mood")

    assert mood.note == "情绪周期: 分歧"


def test_build_conclusions_prefers_positive_synced_as_strongest_mapping():
    factors = [
        ExternalFactorItem(key="us_ai_semiconductor", label="美股AI/半导体", price=100, change_pct=1.8, trade_time="2026-04-14 10:00:00", market="US"),
        ExternalFactorItem(key="oil", label="原油", price=90, change_pct=-2.8, trade_time="2026-04-14 10:00:00", market="CMDTY"),
        ExternalFactorItem(key="us_nasdaq", label="纳斯达克", price=1, change_pct=1.2, trade_time="2026-04-14 10:00:00", market="US"),
        ExternalFactorItem(key="us_sp500", label="标普500", price=1, change_pct=0.8, trade_time="2026-04-14 10:00:00", market="US"),
        ExternalFactorItem(key="china_adr", label="中概/金龙", price=1, change_pct=0.6, trade_time="2026-04-14 10:00:00", market="US_CN"),
        ExternalFactorItem(key="a50", label="A50", price=1, change_pct=0.5, trade_time="2026-04-14 10:00:00", market="A50"),
    ]
    mappings = [
        MappingInsightItem(source_key="us_ai_semiconductor", source_label="美股AI/半导体", a_share_themes=["算力"], status="synced", note=""),
        MappingInsightItem(source_key="oil", source_label="原油", a_share_themes=["油气"], status="synced", note=""),
    ]
    ctx = {
        "sentiment": SimpleNamespace(sentiment_cycle="recovery"),
        "sentiment_state": SimpleNamespace(phase="recovery"),
    }

    conclusions = dashboard2_service._build_conclusions(factors, mappings, ctx)
    strongest = next(x for x in conclusions if x.key == "strongest_mapping")
    divergence = next(x for x in conclusions if x.key == "biggest_divergence")

    assert strongest.value == "美股AI/半导体"
    assert strongest.tone == "positive"
    assert divergence.value == "原油"
