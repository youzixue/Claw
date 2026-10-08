"""飞书 Webhook 推送渠道

独立实现，不依赖 WorkBuddy 环境
使用飞书自定义机器人 Webhook
"""

import httpx
import json
from datetime import datetime
from typing import Optional
from loguru import logger

from app.config.settings import settings
from app.push.channels.base import PushChannel, PushMessage

# 保守的本地卡片预算，按UTF-8 JSON实测分批，不依赖字符数估计。
FEISHU_CARD_BUDGET_BYTES = 18_000


def feishu_category_allowed(message: PushMessage) -> bool:
    return not settings.FEISHU_PAPER_BUY_POINTS_ONLY or message.category in {"paper_buy_point", "c3_research_signal"}


class FeishuChannel(PushChannel):
    """飞书 Webhook 推送"""

    channel_name = "feishu"

    def __init__(self):
        self.webhook_url = settings.FEISHU_WEBHOOK_URL
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """复用连接池，避免同一轮多条买点消息重复建立 TLS 连接。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=3.0),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def send(self, message: PushMessage) -> bool:
        """发送飞书消息(富文本卡片)"""
        # 只暂停发送，不删除异动/复盘/风险模板；直调渠道也不能绕过用户选择。
        if not feishu_category_allowed(message):
            return False
        if not self.webhook_url:
            logger.warning("飞书 Webhook URL 未配置")
            return False

        try:
            # 构建飞书互动卡片
            card = self._build_card(message)
            if message.category in {"paper_buy_point", "c3_research_signal"} and len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > FEISHU_CARD_BUDGET_BYTES:
                logger.error("买点卡片超过本地字节预算，拒绝发送，等待通知消费者分批")
                return False

            resp = await self._get_client().post(
                self.webhook_url,
                json=card,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == 0 or data.get("StatusCode") == 0:
                logger.info(f"飞书推送成功: {message.title[:30]}")
                return True
            else:
                logger.error("飞书推送失败 code={}", data.get("code", data.get("StatusCode", "unknown")))
                return False

        except httpx.HTTPStatusError as e:
            logger.error(f"飞书推送HTTP错误: {e.response.status_code}")
            return False
        except Exception as e:
            logger.error("飞书推送异常: {}", type(e).__name__)
            return False

    async def is_available(self) -> bool:
        """检查飞书渠道是否可用"""
        return bool(self.webhook_url)

    def _build_card(self, message: PushMessage) -> dict:
        """构建飞书互动卡片消息"""
        # 颜色映射
        color_map = {
            "danger": "red",
            "warning": "orange",
            "signal": "blue",
            "info": "green",
        }
        color = color_map.get(message.msg_type, "green")

        # 图标映射
        icon_map = {
            "danger": "🚨",
            "warning": "⚠️",
            "signal": "📡",
            "info": "📊",
        }
        icon = icon_map.get(message.msg_type, "📊")

        # 优先级
        priority_map = {range(8, 11): "🔴 紧急", range(5, 8): "🟡 重要", range(0, 5): "🟢 一般"}
        priority_text = "🟢 一般"
        for rng, text in priority_map.items():
            if message.priority in rng:
                priority_text = text
                break

        # 策略买点复用异动卡片样式，但采用逐股票分区和可读标题。
        # 内部message.title仍供限频去重使用，其他类别不受此展示分支影响。
        if message.category in {"paper_buy_point", "c3_research_signal"}:
            extra = message.extra or {}
            sections = extra.get("feishu_sections")
            if not isinstance(sections, list) or not sections or not all(isinstance(x, str) for x in sections):
                sections = [message.content]
            return {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": f"📡 {extra.get('display_title') or message.title}"},
                        "template": "blue",
                    },
                    "elements": [
                        {"tag": "markdown", "content": section} for section in sections
                    ],
                },
            }

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{icon} {message.title}",
                    },
                    "template": color,
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": message.content,
                    },
                    {
                        "tag": "markdown",
                        "content": f"**优先级**: {priority_text} | **时间**: {datetime.now().strftime('%H:%M:%S')}",
                    },
                ],
            },
        }

        return card


# 全局实例
feishu_channel = FeishuChannel()
