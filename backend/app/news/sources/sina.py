"""央视宏观新闻源 — 政策与宏观要闻

使用 AkShare: news_cctv
"""

import hashlib
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

from app.news.sources.base import NewsItem, NewsSource
from app.news.sources.cls import pd_to_datetime


class SinaSource(NewsSource):
    """央视宏观新闻源"""

    source_name = "央视宏观"
    source_code = "sina"

    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        """获取新闻联播宏观政策要闻"""
        try:
            import akshare as ak
            items = []
            for offset in range(3):
                query_date = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
                df = ak.news_cctv(date=query_date)
                if df.empty:
                    continue
                for _, row in df.head(limit - len(items)).iterrows():
                    title = str(row.get("title", "")).strip()
                    content = str(row.get("content", "")).strip()
                    publish_date = str(row.get("date", query_date))
                    items.append(NewsItem(
                        source=self.source_code,
                        title=title,
                        content=content,
                        publish_time=pd_to_datetime(f"{publish_date[:4]}-{publish_date[4:6]}-{publish_date[6:8]} 20:00:00"),
                        source_id=hashlib.md5(f"{publish_date}{title}".encode("utf-8")).hexdigest(),
                        category="macro",
                    ))
                if len(items) >= limit:
                    break
            return items
        except Exception as e:
            logger.error(f"央视宏观抓取失败: {e}")
            return []

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """获取个股新闻"""
        return []
