"""SHMET快讯新闻源 — 商品与财经快讯

使用 AkShare: futures_news_shmet
"""

import hashlib
import re
from typing import Optional
from loguru import logger

from app.news.sources.base import NewsItem, NewsSource
from app.news.sources.cls import pd_to_datetime


class ThsSource(NewsSource):
    """SHMET快讯新闻源"""

    source_name = "SHMET快讯"
    source_code = "ths"

    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        """获取商品与财经快讯"""
        try:
            import akshare as ak
            df = ak.futures_news_shmet(symbol="全部")
            items = []
            for _, row in df.head(limit).iterrows():
                content = str(row.get("内容", "")).strip()
                publish_time = pd_to_datetime(row.get("发布时间"))
                items.append(NewsItem(
                    source=self.source_code,
                    title=_content_to_title(content),
                    content=content,
                    publish_time=publish_time,
                    source_id=hashlib.md5(f"{publish_time}{content}".encode("utf-8")).hexdigest(),
                    category="flash",
                    related_codes=sorted(set(re.findall(r"(?<!\d)[0368]\d{5}(?!\d)", content))),
                ))
            return items
        except Exception as e:
            logger.error(f"SHMET快讯抓取失败: {e}")
            return []

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """获取个股新闻"""
        try:
            import akshare as ak
            df = ak.stock_individual_info_em(symbol=code)
            items = []
            return items[:limit]
        except Exception as e:
            logger.error(f"同花顺个股新闻抓取失败 [{code}]: {e}")
            return []


def _content_to_title(content: str) -> str:
    match = re.search(r"【([^】]+)】", content)
    if match:
        return match.group(1)[:80]
    return content[:80] or "SHMET快讯"
