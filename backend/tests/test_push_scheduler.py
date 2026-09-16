import pytest

import app.push.scheduler as scheduler_module
from app.config.settings import settings
from app.push.channels.base import PushChannel, PushMessage
from app.push.channels.feishu import FeishuChannel
from app.push.scheduler import PushScheduler
from app.push.throttle import PushThrottle, push_throttle


@pytest.fixture(autouse=True)
def legacy_all_categories(monkeypatch):
    # 本文件原有回归覆盖保留的通用模式；only模式另外逐项验证。
    monkeypatch.setattr(settings, "FEISHU_PAPER_BUY_POINTS_ONLY", False)


class DummyChannel(PushChannel):
    def __init__(self, channel_name: str, *, available: bool = True, success: bool = True):
        self.channel_name = channel_name
        self.available = available
        self.success = success
        self.sent_messages: list[PushMessage] = []

    async def send(self, message: PushMessage) -> bool:
        self.sent_messages.append(message)
        return self.success

    async def is_available(self) -> bool:
        return self.available


class PriorityProbeChannel(DummyChannel):
    async def send(self, message: PushMessage) -> bool:
        self.sent_messages.append(message)
        return message.title == "高优先级"


@pytest.mark.asyncio
async def test_feishu_channel_reuses_http_connection_pool():
    channel = FeishuChannel()
    first = channel._get_client()
    second = channel._get_client()

    assert first is second
    await channel.close()
    assert first.is_closed is True


@pytest.mark.asyncio
async def test_push_scheduler_marks_partial_when_some_channels_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)

    feishu = DummyChannel("feishu", available=True, success=True)
    websocket = DummyChannel("websocket", available=False, success=True)
    scheduler = PushScheduler()
    scheduler.channels = [feishu, websocket]

    result = await scheduler.push_to_channels(
        PushMessage(title="测试消息", content="内容"),
        [feishu, websocket],
        use_throttle=False,
    )

    assert result["sent"] is True
    assert result["status"] == "partial"
    assert result["channels"] == {"feishu": True, "websocket": False}

    stats = await scheduler.get_stats()
    assert stats["sent"] == 1
    assert stats["partial"] == 1


@pytest.mark.asyncio
async def test_anomaly_requires_feishu_delivery_even_when_websocket_succeeds(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)

    feishu = DummyChannel("feishu", available=True, success=False)
    websocket = DummyChannel("websocket", available=True, success=True)
    scheduler = PushScheduler()

    result = await scheduler.push_to_channels(
        PushMessage(title="异动", content="内容", category="anomaly", stock_code="600186"),
        [feishu, websocket],
        use_throttle=False,
    )

    assert result["sent"] is False
    assert result["status"] == "partial"
    assert result["channels"] == {"feishu": False, "websocket": True}


@pytest.mark.asyncio
async def test_push_batch_sends_urgent_first_but_keeps_result_alignment(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)

    channel = PriorityProbeChannel("feishu")
    scheduler = PushScheduler()
    scheduler.channels = [channel]
    low = PushMessage(title="低优先级", content="内容", priority=3)
    high = PushMessage(title="高优先级", content="内容", priority=10)

    results = await scheduler.push_batch([low, high])

    assert [message.title for message in channel.sent_messages] == ["高优先级", "低优先级"]
    assert results[0]["sent"] is False
    assert results[1]["sent"] is True
    assert all(result["latency_ms"] >= 0 for result in results)


@pytest.mark.asyncio
async def test_push_scheduler_honors_push_enabled(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", False)

    feishu = DummyChannel("feishu", available=True, success=True)
    scheduler = PushScheduler()
    scheduler.channels = [feishu]

    result = await scheduler.push(PushMessage(title="总开关关闭", content="内容"))

    assert result["sent"] is False
    assert result["disabled"] is True
    assert result["status"] == "disabled"
    assert feishu.sent_messages == []

    stats = await scheduler.get_stats()
    assert stats["disabled"] == 1


@pytest.mark.asyncio
async def test_push_scheduler_records_throttled_history(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    monkeypatch.setattr(push_throttle, "should_send", lambda message: False)

    feishu = DummyChannel("feishu", available=True, success=True)
    scheduler = PushScheduler()
    scheduler.channels = [feishu]

    result = await scheduler.push(PushMessage(title="限频消息", content="内容"))

    assert result["sent"] is False
    assert result["throttled"] is True
    assert result["status"] == "throttled"
    assert feishu.sent_messages == []

    stats = await scheduler.get_stats()
    assert stats["throttled"] == 1


@pytest.mark.asyncio
async def test_test_feishu_targets_only_feishu_channel(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)

    feishu = DummyChannel("feishu", available=True, success=True)
    websocket = DummyChannel("websocket", available=True, success=True)
    monkeypatch.setattr(scheduler_module, "feishu_channel", feishu)

    scheduler = PushScheduler()
    scheduler.channels = [websocket]

    result = await scheduler.test_feishu()

    assert result["sent"] is True
    assert result["channels"] == {"feishu": True}
    assert len(feishu.sent_messages) == 1
    assert websocket.sent_messages == []


def test_push_throttle_does_not_mark_stock_cooldown_when_title_dedup_blocks(monkeypatch):
    throttle = PushThrottle()
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 99)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 600)
    monkeypatch.setattr(settings, "URGENT_SCORE_THRESHOLD", 100)

    now = 1_700_000_000.0
    duplicate_message = PushMessage(title="重复标题", content="内容", stock_code="000001", priority=1)
    fresh_message = PushMessage(title="新标题", content="内容", stock_code="000001", priority=1)

    throttle._hourly_reset = now
    throttle._title_last_push[hash(duplicate_message.title)] = now - 60

    monkeypatch.setattr("time.time", lambda: now)

    assert throttle.should_send(duplicate_message) is False
    assert throttle.should_send(fresh_message) is True


def test_urgent_anomaly_still_honors_stock_cooldown(monkeypatch):
    throttle = PushThrottle()
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 99)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 300)
    monkeypatch.setattr(settings, "ANOMALY_PUSH_STOCK_COOLDOWN", 900)
    monkeypatch.setattr(settings, "URGENT_SCORE_THRESHOLD", 90)
    now = 1_700_000_000.0
    monkeypatch.setattr("time.time", lambda: now)
    message = PushMessage(
        title="A1突破",
        content="内容",
        stock_code="600351",
        priority=10,
        category="anomaly",
    )

    assert throttle.should_send(message) is True
    throttle.mark_sent(message)
    assert throttle.should_send(message) is False


def test_anomaly_a1_upgrade_can_pass_a2_stock_cooldown_once(monkeypatch):
    throttle = PushThrottle()
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 99)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 300)
    monkeypatch.setattr(settings, "ANOMALY_PUSH_STOCK_COOLDOWN", 900)
    now = 1_700_000_000.0
    monkeypatch.setattr("time.time", lambda: now)
    touch = PushMessage(
        title="趋势支撑到达预警",
        content="内容",
        stock_code="003032",
        category="anomaly",
        extra={
            "setup_grade": "A2 盘口确认后执行",
            "signal_identity": "003032|low_absorb|trend_support_touch",
        },
    )
    reclaim = PushMessage(
        title="趋势支撑回收买点",
        content="内容",
        stock_code="003032",
        category="anomaly",
        extra={
            "setup_grade": "A1 可直接执行",
            "signal_identity": "003032|low_absorb|trend_driver_support_reclaim",
        },
    )

    assert throttle.should_send(touch) is True
    throttle.mark_sent(touch)
    assert throttle.should_send(reclaim) is True
    throttle.mark_sent(reclaim)
    assert throttle.should_send(reclaim) is False


def test_same_signal_a1_upgrade_can_pass_a2_stock_cooldown_once(monkeypatch):
    throttle = PushThrottle()
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 99)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 300)
    monkeypatch.setattr(settings, "ANOMALY_PUSH_STOCK_COOLDOWN", 900)
    now = 1_700_000_000.0
    monkeypatch.setattr("time.time", lambda: now)
    identity = "600645|breakthrough|positive_acceleration|rolling_strong"
    a2 = PushMessage(
        title="A2急拉",
        content="内容",
        stock_code="600645",
        category="anomaly",
        extra={"setup_grade": "A2 盘口确认后执行", "signal_identity": identity},
    )
    a1 = PushMessage(
        title="A1急拉升级",
        content="内容",
        stock_code="600645",
        category="anomaly",
        extra={"setup_grade": "A1 可直接执行", "signal_identity": identity},
    )

    assert throttle.should_send(a2) is True
    throttle.mark_sent(a2)
    assert throttle.should_send(a1) is True
    throttle.mark_sent(a1)
    assert throttle.should_send(a1) is False


def test_rapid_rise_medium_to_strong_can_pass_stock_cooldown_once(monkeypatch):
    throttle = PushThrottle()
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 99)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 300)
    monkeypatch.setattr(settings, "ANOMALY_PUSH_STOCK_COOLDOWN", 900)
    now = 1_700_000_000.0
    monkeypatch.setattr("time.time", lambda: now)
    prefix = "002580|breakthrough|positive_acceleration|rolling_"
    medium = PushMessage(
        title="60秒急拉",
        content="内容",
        stock_code="002580",
        category="anomaly",
        extra={
            "setup_grade": "A2 盘口确认后执行",
            "signal_identity": prefix + "medium",
        },
    )
    strong = PushMessage(
        title="60秒强急拉",
        content="内容",
        stock_code="002580",
        category="anomaly",
        extra={
            "setup_grade": "A2 盘口确认后执行",
            "signal_identity": prefix + "strong",
        },
    )

    assert throttle.should_send(medium) is True
    throttle.mark_sent(medium)
    assert throttle.should_send(strong) is True
    throttle.mark_sent(strong)
    assert throttle.should_send(strong) is False


def test_observation_suffix_does_not_hide_rapid_strength_upgrade(monkeypatch):
    throttle = PushThrottle()
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 99)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 300)
    monkeypatch.setattr(settings, "ANOMALY_PUSH_STOCK_COOLDOWN", 900)
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.0)
    prefix = "600186|breakthrough|positive_acceleration|rolling_"
    medium = PushMessage(
        title="急拉观察",
        content="内容",
        stock_code="600186",
        category="anomaly",
        extra={"signal_identity": prefix + "medium|observation"},
    )
    strong = PushMessage(
        title="强急拉观察",
        content="内容",
        stock_code="600186",
        category="anomaly",
        extra={"signal_identity": prefix + "strong|observation"},
    )

    assert throttle.should_send(medium) is True
    throttle.mark_sent(medium)
    assert throttle.should_send(strong) is True


def test_failed_attempt_does_not_consume_throttle_quota(monkeypatch):
    throttle = PushThrottle()
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 99)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 300)
    monkeypatch.setattr(settings, "ANOMALY_PUSH_STOCK_COOLDOWN", 900)
    now = 1_700_000_000.0
    monkeypatch.setattr("time.time", lambda: now)
    message = PushMessage(
        title="发送失败可重试",
        content="内容",
        stock_code="600351",
        category="anomaly",
    )

    assert throttle.should_send(message) is True
    assert throttle.should_send(message) is True
    assert throttle.get_stats()["hourly_count"] == 0
