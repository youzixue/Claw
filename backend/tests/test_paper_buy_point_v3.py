"""只保留买点的发送政策、逐股真实快照和卡片字节分批；全程假渠道。"""
import copy
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.config.settings import settings
from app.push import paper_buy_points as points
from app.push.channels.base import PushMessage
from app.push.channels.feishu import FeishuChannel, FEISHU_CARD_BUDGET_BYTES
from app.push.scheduler import PushScheduler
import app.push.scheduler as scheduler_module
from app.push.throttle import PushThrottle
from app.paper.account_policy import ACCOUNT_NAMES
from test_paper_buy_points import setup, record, logs
from test_paper_buy_point_template import item
from test_push_scheduler import DummyChannel


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["anomaly", "risk", "review", "system", "general", "tenbagger"])
async def test_non_buy_categories_are_suppressed_but_code_can_be_reenabled(monkeypatch, category):
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "FEISHU_PAPER_BUY_POINTS_ONLY", True)
    scheduler = PushScheduler()
    channel = DummyChannel("feishu")
    message = PushMessage(title="不发", content="保留内容", category=category, priority=10)
    result = await scheduler.push_to_channels(message, [channel], use_throttle=False)
    assert result["status"] == "category_suppressed"
    assert result["suppressed_channels"] == ["feishu"] and not result["sent"]
    assert not channel.sent_messages
    assert scheduler.get_history()[0]["suppressed_channels"] == ["feishu"]
    monkeypatch.setattr(settings, "FEISHU_PAPER_BUY_POINTS_ONLY", False)
    assert (await scheduler.push_to_channels(message, [channel], use_throttle=False))["sent"]


@pytest.mark.asyncio
async def test_direct_feishu_sender_cannot_bypass_only_mode(monkeypatch):
    monkeypatch.setattr(settings, "FEISHU_PAPER_BUY_POINTS_ONLY", True)
    channel = FeishuChannel()
    channel.webhook_url = "https://example.invalid"
    client = AsyncMock()
    monkeypatch.setattr(channel, "_get_client", lambda: client)
    assert not await channel.send(PushMessage(title="禁止", content="内容", category="anomaly"))
    assert not client.post.called


@pytest.mark.asyncio
async def test_websocket_anomalies_cannot_use_paper_feishu_quota(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "FEISHU_PAPER_BUY_POINTS_ONLY", True)
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 1)
    monkeypatch.setattr(scheduler_module, "push_throttle", PushThrottle())
    scheduler = PushScheduler()
    feishu, ws = DummyChannel("feishu"), DummyChannel("websocket")
    result = await scheduler.push_to_channels(
        PushMessage(title="网页异动", content="保留", category="anomaly"), [feishu, ws])
    assert ws.sent_messages and not feishu.sent_messages
    assert result["channels"] == {"feishu": False, "websocket": True} and not result["sent"]
    paper = PushMessage(title="真实买点1", content="内容", category="paper_buy_point")
    assert (await scheduler.push_to_channels(paper, [feishu]))["sent"]
    assert scheduler_module.push_throttle.get_stats()["hourly_count"] == 1
    assert scheduler._paper_buy_point_throttle.get_stats()["hourly_count"] == 1
    assert (await scheduler.push_to_channels(
        PushMessage(title="真实买点2", content="内容", category="paper_buy_point"), [feishu]))["throttled"]


def context(now):
    return {"source_quote_at": (now - timedelta(seconds=10)).isoformat(),
            "quote_round_id": "round-current", "price": 10.5,
            "change_pct": 3.5, "avg_price": 10.4, "volume_ratio": 2.8,
            "turnover_rate": 6.3, "amount": 234000000, "low": 9.8, "high": 10.6,
            "stop_loss_price": 9.9, "limit_up": 11.0, "private_extra": "不可复制",
            "circ_market_cap": float("nan")}


@pytest.mark.asyncio
async def test_market_snapshot_is_owned_frozen_and_user_readable(setup):
    maker, now, send = setup
    quote = context(now)
    row = await record(maker, now, market_context=quote)
    frozen = json.loads(row.candidate_json)["market_context"]
    assert "private_extra" not in frozen and "circ_market_cap" not in frozen
    quote["avg_price"] = 99
    await points.dispatch_buy_points(now=now, session_factory=maker)
    message = send.call_args.args[0]
    assert "分时均价(VWAP) ¥**10.40**" in message.content
    assert "成交额 **2.34亿元**" in message.content
    assert "换手率 **6.30%**" in message.content
    assert "当日区间 **¥9.80–10.60**" in message.content
    assert "策略风控参考价 **¥9.90**" in message.content
    assert "涨停价 **¥11.00**" in message.content
    assert (now - timedelta(seconds=10)).strftime("%H:%M:%S") in message.content
    assert "执行边界" not in message.content and "信号追溯" not in message.content
    assert "test-version" not in message.content
    assert message.extra["signal_audits"][0]["run_id"] == row.run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"source_quote_at": "not-time"}, {"quote_round_id": "other-round"},
    {"price": 11}, {"source_quote_at": "2020-01-01T09:40:00"},
    {"source_quote_at": "2026-09-08T09:40:00+08:00"},
])
async def test_incoherent_context_never_becomes_user_signal(setup, patch):
    maker, now, send = setup
    quote = context(now) | patch
    assert await record(maker, now, market_context=quote) is None
    assert not send.called


@pytest.mark.asyncio
async def test_retry_does_not_renew_individual_stock_quote_lifetime(setup):
    maker, now, send = setup
    quote = context(now)
    quote["source_quote_at"] = (now - timedelta(seconds=170)).isoformat()
    await record(maker, now, market_context=quote)
    send.return_value = {"channels": {"feishu": False}, "status": "failed"}
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "failed"
    await points.dispatch_buy_points(now=now + timedelta(seconds=16), session_factory=maker)
    assert send.call_count == 1
    assert (await logs(maker))[-1].decision == "expired"


def test_fit_batch_measures_utf8_not_character_count_and_never_loses_reason():
    samples = [item(account, n, reason="买点依据测试" * 150) for n, account in enumerate(ACCOUNT_NAMES, 1)]
    for sample in samples:
        sample["payload"]["market_context"] = context(sample["as_of_at"])
        sample["payload"]["strategy_label"] = "完整策略名称" * 20
        sample["execution_note"] = "执行约束说明" * 40
    before = copy.deepcopy(samples)
    batch = points._fit_batch(samples)
    assert samples == before
    assert 0 < len(batch) < 6
    card = FeishuChannel()._build_card(points.build_message(batch))
    assert len(json.dumps(card, ensure_ascii=False).encode()) <= FEISHU_CARD_BUDGET_BYTES
    for sample in batch:
        assert sample["reason"][:800] in points.build_message(batch).content


@pytest.mark.asyncio
async def test_byte_split_leaves_remaining_signals_unattempted_then_delivers(setup, monkeypatch):
    maker, now, send = setup
    for account in ACCOUNT_NAMES[:6]:
        await record(maker, now, account=account, reason="买点依据测试" * 150,
                     market_context=context(now), label="完整策略名称" * 20)
    result = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert 0 < result["count"] < 6
    first_count = result["count"]
    first_logs = await logs(maker)
    assert sum(x.action == points.DELIVERY and x.decision == "attempting" for x in first_logs) == first_count
    total = first_count
    for _ in range(6):
        result = await points.dispatch_buy_points(now=now, session_factory=maker)
        total += result["count"]
        if result["count"] == 0:
            break
    assert total == 6
    for call in send.call_args_list:
        card = FeishuChannel()._build_card(call.args[0])
        assert len(json.dumps(card, ensure_ascii=False).encode()) <= FEISHU_CARD_BUDGET_BYTES


@pytest.mark.asyncio
async def test_sender_rejects_oversize_card_without_network(monkeypatch):
    monkeypatch.setattr(settings, "FEISHU_PAPER_BUY_POINTS_ONLY", True)
    channel = FeishuChannel()
    channel.webhook_url = "https://example.invalid"
    client = AsyncMock()
    monkeypatch.setattr(channel, "_get_client", lambda: client)
    message = PushMessage(title="过大卡片", content="中文" * 10000, category="paper_buy_point")
    assert not await channel.send(message)
    assert not client.post.called
