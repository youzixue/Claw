"""新闻引擎 — 6源抓取 + AI增强NLP + 去重 + 入库

6源: 财联社(最快) + 东财(个股) + 巨潮(公告) + 同花顺(概念) + 新浪(宏观) + 外盘(隔夜)
"""

from app.news.engine import news_engine
from app.news.sources.base import NewsItem
from app.news.dedup import news_dedup
from app.news.nlp.processor import news_processor

__all__ = ["news_engine", "NewsItem", "news_dedup", "news_processor"]
