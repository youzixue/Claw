"""新闻源 — 6源互补"""

from app.news.sources.base import NewsItem, NewsSource
from app.news.sources.cls import ClsSource
from app.news.sources.em import EmSource
from app.news.sources.cninfo import CninfoSource
from app.news.sources.ths import ThsSource
from app.news.sources.sina import SinaSource
from app.news.sources.global_market import GlobalMarketSource

__all__ = [
    "NewsItem", "NewsSource",
    "ClsSource", "EmSource", "CninfoSource",
    "ThsSource", "SinaSource", "GlobalMarketSource",
]
