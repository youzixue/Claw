"""新闻 NLP 处理器 — 统一调度 AI 模块

流程:
1. 情感分析 → ai.sentiment
2. 事件提取 → ai.event_extractor
3. 摘要生成 → ai.summary
4. 关联个股/板块识别
"""

import asyncio
from loguru import logger

from app.news.sources.base import NewsItem
from app.ai.sentiment import analyze_sentiment
from app.ai.event_extractor import extract_events
from app.ai.summary import generate_summary_fallback
from app.news.roles import guard_news_analysis


class NewsProcessor:
    """新闻 NLP 处理器"""

    async def process(self, item: NewsItem) -> dict:
        """处理单条新闻

        Returns:
            {
                sentiment, confidence, impact_scope,
                events: [...],
                summary,
                related_codes, related_sectors,
            }
        """
        sentiment, events = await asyncio.gather(
            analyze_sentiment(item.title, item.content),
            extract_events(item.title, item.content),
        )
        summary = sentiment.get("summary") or generate_summary_fallback(item.title, item.content)

        # 4. 合并关联信息
        related_codes = list(item.related_codes)
        related_sectors = list(item.related_sectors)
        related_codes.extend(sentiment.get("related_codes", []))
        related_sectors.extend(sentiment.get("related_sectors", []))

        # 从事件提取结果补充
        if events and events.get("events"):
            for evt in events["events"]:
                related_codes.extend(evt.get("related_codes", []))
                related_sectors.extend(evt.get("related_sectors", []))

        # 去重
        related_codes = list(set(related_codes))
        related_sectors = list(set(related_sectors))

        result = {
            "sentiment": sentiment.get("sentiment", "neutral"),
            "confidence": sentiment.get("confidence", 0),
            "impact_scope": sentiment.get("impact_scope", "stock"),
            "sentiment_method": sentiment.get("method", "keyword"),
            "events": events.get("events", []),
            "events_method": events.get("method", "keyword"),
            "summary": summary,
            "related_codes": related_codes,
            "related_sectors": related_sectors,
            "key_points": sentiment.get("key_points", []),
        }

        result = guard_news_analysis(
            item.title, item.content, result, getattr(item, "_news_entity_evidence", ()),
        )

        logger.debug(
            f"新闻处理: {item.title[:30]} → "
            f"情感={result['sentiment']} 事件={len(result['events'])} 关联={len(related_codes)}股"
        )

        return result

    async def process_batch(self, items: list[NewsItem]) -> list[dict]:
        """批量处理"""
        results = []
        for item in items:
            try:
                result = await self.process(item)
                results.append(result)
            except Exception as e:
                logger.error(f"新闻处理异常 [{item.title[:20]}]: {e}")
        return results


# 全局处理器
news_processor = NewsProcessor()
