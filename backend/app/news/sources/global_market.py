"""外盘/期货新闻源 — 隔夜信号

使用 AkShare: 外盘指数+期货
"""

from datetime import datetime
from typing import Optional
from loguru import logger

from app.news.sources.base import NewsItem, NewsSource


class GlobalMarketSource(NewsSource):
    """外盘/期货新闻源"""

    source_name = "外盘期货"
    source_code = "global"

    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        """获取外盘期货信号(非新闻,是数据异动)"""
        items = []

        try:
            import akshare as ak

            # 外盘指数
            try:
                df = ak.index_global_em()
                for _, row in df.head(20).iterrows():
                    name = str(row.get("名称", ""))
                    change = row.get("涨跌幅", 0)
                    if abs(float(change if change else 0)) > 1.0:  # 只关注波动>1%
                        items.append(NewsItem(
                            source=self.source_code,
                            title=f"外盘异动: {name} 涨跌幅{change}%",
                            content=str(row.to_dict()),
                            publish_time=datetime.now(),
                            source_id=f"global_{name}",
                            category="global",
                        ))
            except Exception as e:
                logger.warning(f"外盘指数获取失败: {e}")

            # 期货
            try:
                df = ak.futures_main_sina()
                for _, row in df.head(20).iterrows():
                    name = str(row.get("品种", row.get("名称", "")))
                    change = row.get("涨跌幅", 0)
                    if abs(float(change if change else 0)) > 2.0:
                        items.append(NewsItem(
                            source=self.source_code,
                            title=f"期货异动: {name} 涨跌幅{change}%",
                            content=str(row.to_dict()),
                            publish_time=datetime.now(),
                            source_id=f"global_fut_{name}",
                            category="futures",
                        ))
            except Exception as e:
                logger.warning(f"期货数据获取失败: {e}")

        except ImportError:
            logger.warning("akshare 未安装，外盘数据跳过")

        return items[:limit]

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """个股不适用"""
        return []
