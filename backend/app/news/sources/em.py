"""东财全球资讯 + 财新精选新闻源。

优先使用 AkShare ``stock_info_global_em`` 获取盘前可见的全球 7×24
产业/市场快讯，再用 ``stock_news_main_cx`` 补充深度摘要。旧实现只抓
财新精选，容易漏掉海外临床、科技产品和大宗商品等隔夜催化。
"""

import hashlib
import re
from loguru import logger

from app.news.sources.base import NewsItem, NewsSource
from app.news.sources.cls import pd_to_datetime


class EmSource(NewsSource):
    """东财全球资讯与财新精选互补源。"""

    source_name = "东财全球资讯"
    source_code = "em"

    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        """先取全球 7×24 快讯，再用财新摘要补足条数。"""
        items: list[NewsItem] = []
        try:
            import akshare as ak

            global_df = ak.stock_info_global_em()
            for _, row in global_df.head(max(1, limit)).iterrows():
                title = str(row.get("标题", "")).strip()
                summary = str(row.get("摘要", "")).strip()
                url = str(row.get("链接", "")).strip()
                if not title and not summary:
                    continue
                text = f"{title} {summary}"
                items.append(NewsItem(
                    source=self.source_code,
                    title=title or _summary_to_title(summary),
                    content=summary,
                    url=url,
                    publish_time=pd_to_datetime(row.get("发布时间")),
                    source_id=(url or hashlib.md5(text.encode("utf-8")).hexdigest())[-100:],
                    category="global_flash",
                    related_codes=sorted(set(re.findall(r"(?<!\d)[0368]\d{5}(?!\d)", text))),
                ))
        except Exception as e:
            logger.error(f"东财全球资讯抓取失败: {e}")

        if len(items) < limit:
            try:
                import akshare as ak

                df = ak.stock_news_main_cx()
                for _, row in df.head(limit - len(items)).iterrows():
                    summary = str(row.get("summary", "")).strip()
                    url = str(row.get("url", ""))
                    tag = str(row.get("tag", "市场动态")).strip() or "市场动态"
                    items.append(NewsItem(
                        source=self.source_code,
                        title=_summary_to_title(summary),
                        content=summary,
                        url=url,
                        publish_time=_date_from_url(url),
                        source_id=(url or hashlib.md5(summary.encode("utf-8")).hexdigest())[:100],
                        category=tag,
                    ))
            except Exception as e:
                logger.error(f"财新精选补充抓取失败: {e}")

        items.sort(key=lambda item: item.publish_time or pd_to_datetime("1970-01-01"), reverse=True)
        return items[:limit]

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """获取个股相关新闻"""
        try:
            import akshare as ak
            df = ak.stock_individual_info_em(symbol=code)
            items = []
            return items[:limit]
        except Exception as e:
            logger.error(f"东财个股新闻抓取失败 [{code}]: {e}")
            return []


def _summary_to_title(summary: str) -> str:
    title = re.split(r"[。；;]", summary, maxsplit=1)[0].strip()
    return title[:80] or summary[:80] or "财新精选"


def _date_from_url(url: str):
    match = re.search(r"/(20\d{2})-(\d{2})-(\d{2})/", url or "")
    if not match:
        return None
    return pd_to_datetime("-".join(match.groups()) + " 00:00:00")
