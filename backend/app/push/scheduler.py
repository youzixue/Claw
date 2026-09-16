"""推送调度中心 — 统一管理推送渠道+限频+调度

职责:
1. 管理推送渠道(飞书+WebSocket)
2. 限频过滤
3. 4时段复盘调度
4. 推送历史记录
"""

import time
from datetime import datetime
from loguru import logger

from app.config.settings import settings
from app.push.channels.base import PushMessage
from app.push.channels.feishu import feishu_channel, feishu_category_allowed
from app.push.channels.websocket import ws_channel
from app.push.throttle import push_throttle, PushThrottle


class PushScheduler:
    """推送调度中心"""

    def __init__(self):
        self.channels = [feishu_channel, ws_channel]
        self._history: list[dict] = []
        self._max_history = 1000
        # 网页异动/复盘不能占用模拟买点的飞书额度，持久限频仍由买点worker保留。
        self._paper_buy_point_throttle = PushThrottle()

    def _append_history(self, message: PushMessage, result: dict):
        """记录推送结果，便于治理页排障。"""
        self._history.append({
            "title": message.title,
            "category": message.category,
            "stock_code": message.stock_code,
            "priority": message.priority,
            "sent": result.get("sent", False),
            "channels": result.get("channels", {}),
            "status": result.get("status", ""),
            "throttled": result.get("throttled", False),
            "disabled": result.get("disabled", False),
            "suppressed_channels": result.get("suppressed_channels", []),
            "latency_ms": result.get("latency_ms", 0),
            "time": datetime.now().isoformat(),
        })
        if len(self._history) > self._max_history:
            self._history = self._history[-500:]

    async def push_to_channels(
        self,
        message: PushMessage,
        channels: list,
        *,
        use_throttle: bool = True,
    ) -> dict:
        """推送消息到指定渠道。"""
        started_at = time.monotonic()
        channel_results = {channel.channel_name: False for channel in channels}

        if not settings.PUSH_ENABLED:
            result = {
                "sent": False,
                "channels": channel_results,
                "throttled": False,
                "disabled": True,
                "status": "disabled",
                "latency_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            }
            self._append_history(message, result)
            return result

        suppressed = [ch.channel_name for ch in channels
                      if ch.channel_name == "feishu" and not feishu_category_allowed(message)]
        requested_feishu = "feishu" in channel_results
        channels = [ch for ch in channels if ch.channel_name not in suppressed]
        if not channels:
            result = {"sent": False, "channels": channel_results, "throttled": False,
                      "disabled": True, "status": "category_suppressed",
                      "suppressed_channels": suppressed, "latency_ms": 0}
            self._append_history(message, result)
            return result
        throttle = self._paper_buy_point_throttle if message.category == "paper_buy_point" else push_throttle
        if use_throttle and not throttle.should_send(message):
            result = {
                "sent": False,
                "channels": channel_results,
                "throttled": True,
                "disabled": False,
                "status": "throttled",
                "latency_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            }
            self._append_history(message, result)
            return result

        attempted = False
        available_channels: set[str] = set()
        for channel in channels:
            if not await channel.is_available():
                continue
            attempted = True
            available_channels.add(channel.channel_name)
            try:
                channel_results[channel.channel_name] = await channel.send(message)
            except Exception as e:
                logger.error(f"推送渠道 [{channel.channel_name}] 异常: {e}")
                channel_results[channel.channel_name] = False

        any_channel_sent = any(channel_results.values())
        # 自动异动的送达目标是飞书。若WS成功但飞书失败，不能写入成功记录
        # 和15分钟冷却，否则该信号后续永远没有机会补发到用户真正查看的渠道。
        feishu_required = bool(
            message.category == "anomaly"
            and (requested_feishu if suppressed else "feishu" in available_channels)
        )
        sent = bool(
            channel_results.get("feishu")
            if feishu_required
            else any_channel_sent
        )
        if use_throttle and (sent or (suppressed and any_channel_sent)):
            throttle.mark_sent(message)
        if sent and all(channel_results.values()):
            status = "sent"
        elif any_channel_sent:
            status = "partial"
        elif attempted:
            status = "failed"
        else:
            status = "skipped"

        result = {
            "sent": sent,
            "channels": channel_results,
            "throttled": False,
            "disabled": False,
            "status": status,
            "suppressed_channels": suppressed,
            "latency_ms": max(0, int((time.monotonic() - started_at) * 1000)),
        }
        self._append_history(message, result)
        return result

    async def push(self, message: PushMessage) -> dict:
        """推送消息

        Returns:
            {sent: bool, channels: {channel_name: success}, throttled: bool}
        """
        return await self.push_to_channels(message, self.channels, use_throttle=True)

    async def push_batch(self, messages: list[PushMessage]) -> list[dict]:
        """批量推送：高优先级先发，返回结果仍与原消息顺序对齐。"""
        if not messages:
            return []

        results: list[dict | None] = [None] * len(messages)
        ordered_indexes = sorted(
            range(len(messages)),
            key=lambda index: messages[index].priority,
            reverse=True,
        )
        for index in ordered_indexes:
            results[index] = await self.push(messages[index])
        return [result for result in results if result is not None]

    def get_history(self, limit: int = 50) -> list[dict]:
        """获取推送历史"""
        return self._history[-limit:]

    async def get_stats(self) -> dict:
        """获取推送统计"""
        total = len(self._history)
        sent = sum(1 for h in self._history if h.get("sent"))
        throttled = sum(1 for h in self._history if h.get("status") == "throttled")
        partial = sum(1 for h in self._history if h.get("status") == "partial")
        failed = sum(1 for h in self._history if h.get("status") == "failed")
        disabled = sum(1 for h in self._history if h.get("status") == "disabled")
        skipped = sum(1 for h in self._history if h.get("status") == "skipped")

        channel_status = {}
        for ch in self.channels:
            channel_status[ch.channel_name] = await ch.is_available()

        return {
            "total": total,
            "sent": sent,
            "throttled": throttled,
            "partial": partial,
            "failed": failed,
            "disabled": disabled,
            "skipped": skipped,
            "channels": channel_status,
            "throttle_stats": push_throttle.get_stats(),
            "paper_buy_point_throttle_stats": self._paper_buy_point_throttle.get_stats(),
            "feishu_paper_buy_points_only": settings.FEISHU_PAPER_BUY_POINTS_ONLY,
        }

    async def test_feishu(self) -> dict:
        """测试飞书推送"""
        test_msg = PushMessage(
            title="🦅 Claw 系统测试",
            content="飞书推送渠道测试成功！\nClaw 鹰爪量化交易系统已就绪。",
            msg_type="info",
            category="system",
            priority=5,
        )
        return await self.push_to_channels(test_msg, [feishu_channel], use_throttle=False)


# 全局调度器
push_scheduler = PushScheduler()
