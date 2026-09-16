"""新闻源基类 — 统一接口"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class NewsItem:
    """统一新闻条目"""
    source: str                     # cls/em/cninfo/ths/sina/global
    title: str
    content: str = ""
    url: str = ""
    publish_time: Optional[datetime] = None
    source_id: str = ""             # 源站唯一ID
    category: str = ""              # policy/earnings/industry/macro...
    related_codes: list = field(default_factory=list)
    related_sectors: list = field(default_factory=list)


class NewsSource(ABC):
    """新闻源基类"""

    source_name: str = ""
    source_code: str = ""

    @abstractmethod
    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        """获取最新新闻"""
        ...

    @abstractmethod
    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """获取个股相关新闻"""
        ...
