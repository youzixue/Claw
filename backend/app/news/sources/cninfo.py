"""巨潮资讯新闻源 — 公告权威。

使用巨潮官方披露接口对应的 AkShare 封装
``stock_zh_a_disclosure_report_cninfo``。旧的 ``stock_notice_report``
默认日期固定在 2022-05-11，且 ``symbol`` 表示公告类型而非股票代码，
不能用于“最新公告”或按代码查询。
"""

import asyncio
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from loguru import logger

from app.news.sources.base import NewsItem, NewsSource
from app.news.sources.cls import pd_to_datetime


class CninfoSource(NewsSource):
    """巨潮资讯新闻源"""

    source_name = "巨潮资讯"
    source_code = "cninfo"
    # 全市场披露密集日，最新100条通常会被半年报、会议决议和治理制度占满。
    # 重大合同/重组/控制权/回购/业绩预告等必须走独立保留流，否则公告虽已
    # 在官方源发布，却在进入催化评分前就被 head(limit) 截掉。
    _MATERIAL_TITLE_PATTERN = re.compile(
        r"中标|重大合同|订单|战略合作|投资建设|对外投资|重大项目|"
        r"重大资产重组|发行股份|购买资产|收购|资产注入|控制权|实际控制人|"
        r"股权转让|要约收购|停牌|回购|增持|业绩预告|预增|扭亏|"
        r"撤销.*风险警示|摘帽|预重整|重整投资|债权人申请重整|"
        r"审核通过|获得批复|行政许可|注册批复"
    )
    _MATERIAL_SEARCH_KEYWORDS = (
        "中标",
        "重大合同",
        "订单",
        "重大资产重组",
        "购买资产",
        "收购",
        "控制权",
        "实际控制人",
        "重整",
        "撤销风险警示",
        "业绩预告",
        "增持",
    )
    _GLOBAL_LATEST_MAX_PAGES = 4
    _GLOBAL_MATERIAL_MAX_PAGES = 2

    @staticmethod
    def _date_text(value: datetime) -> str:
        return value.strftime("%Y%m%d")

    @staticmethod
    def _normalize_code(value) -> str:
        text = str(value or "").strip()
        if text.endswith(".0"):
            text = text[:-2]
        return text.zfill(6) if text.isdigit() and len(text) <= 6 else text

    @classmethod
    def _fetch_global_page_set(
        cls,
        *,
        start_date: str,
        end_date: str,
        keyword: str = "",
        max_pages: int = 1,
    ) -> pd.DataFrame:
        """直接读取巨潮官方查询接口，并限制翻页数。

        AkShare 的全市场封装会把命中区间的全部页面下载完；半年报密集期
        近7天可超过400页，导致盘前快照阻塞。这里仍使用同一官方接口和
        字段，只限制“最新流”页数，并由多个重大事件关键词补足召回。
        """
        url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
        payload = {
            "pageNum": 1,
            "pageSize": 30,
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": keyword,
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": (
                f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}~"
                f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            ),
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        }
        frames: list[pd.DataFrame] = []
        with requests.Session() as client:
            client.headers.update(headers)
            for page in range(1, max(1, int(max_pages)) + 1):
                payload["pageNum"] = page
                response = client.post(url, data=payload, timeout=10)
                response.raise_for_status()
                body = response.json()
                announcements = body.get("announcements") or []
                if not announcements:
                    break
                frames.append(pd.DataFrame(announcements))
                total = int(body.get("totalAnnouncement") or 0)
                if page * int(payload["pageSize"]) >= total:
                    break
        if not frames:
            return pd.DataFrame()
        frame = pd.concat(frames, ignore_index=True)
        frame.rename(
            columns={
                "secCode": "代码",
                "secName": "简称",
                "announcementTitle": "公告标题",
                "announcementTime": "公告时间",
            },
            inplace=True,
        )
        for column in ("代码", "简称", "公告标题", "公告时间"):
            if column not in frame.columns:
                frame[column] = ""
        frame["公告标题"] = frame["公告标题"].fillna("").astype(str).map(
            lambda value: re.sub(r"</?em>", "", value).strip()
        )
        frame["公告时间"] = pd.to_datetime(
            frame["公告时间"], unit="ms", utc=True, errors="coerce"
        ).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        frame["公告链接"] = frame.apply(
            lambda row: (
                "https://www.cninfo.com.cn/new/disclosure/detail?"
                f"stockCode={row.get('代码', '')}&"
                f"announcementId={row.get('announcementId', '')}&"
                f"orgId={row.get('orgId', '')}&"
                f"announcementTime={row.get('公告时间', '')}"
            ),
            axis=1,
        )
        return frame[["代码", "简称", "公告标题", "公告时间", "公告链接"]]

    async def _fetch_disclosures(self, *, code: str = "", lookback_days: int = 7):
        import akshare as ak

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        start = now - timedelta(days=max(1, lookback_days))
        if not code:
            semaphore = asyncio.Semaphore(4)

            async def fetch_keyword(keyword: str, max_pages: int) -> pd.DataFrame:
                async with semaphore:
                    return await asyncio.to_thread(
                        self._fetch_global_page_set,
                        start_date=self._date_text(start),
                        end_date=self._date_text(now),
                        keyword=keyword,
                        max_pages=max_pages,
                    )

            frames = await asyncio.gather(
                fetch_keyword("", self._GLOBAL_LATEST_MAX_PAGES),
                *(
                    fetch_keyword(keyword, self._GLOBAL_MATERIAL_MAX_PAGES)
                    for keyword in self._MATERIAL_SEARCH_KEYWORDS
                ),
            )
            non_empty = [frame for frame in frames if not frame.empty]
            if not non_empty:
                return pd.DataFrame()
            combined = pd.concat(non_empty, ignore_index=True)
            return combined.drop_duplicates(
                subset=["代码", "公告标题", "公告时间", "公告链接"],
                keep="first",
            )
        return await asyncio.to_thread(
            ak.stock_zh_a_disclosure_report_cninfo,
            symbol=code,
            market="沪深京",
            keyword="",
            category="",
            start_date=self._date_text(start),
            end_date=self._date_text(now),
        )

    def _to_items(self, df, *, limit: int, fallback_code: str = "") -> list[NewsItem]:
        if df is None or getattr(df, "empty", True):
            return []
        if "公告时间" in df.columns:
            df = df.sort_values("公告时间", ascending=False)
        items: list[NewsItem] = []
        for _, row in df.head(max(1, limit)).iterrows():
            code = self._normalize_code(row.get("代码", fallback_code))
            title = str(row.get("公告标题", row.get("标题", "")) or "").strip()
            if not title:
                continue
            url = str(row.get("公告链接", row.get("链接", row.get("网址", ""))) or "")
            announcement_match = re.search(r"announcementId=(\d+)", url)
            items.append(NewsItem(
                source=self.source_code,
                title=title,
                content="",
                url=url,
                publish_time=pd_to_datetime(
                    row.get("公告时间", row.get("公告日期", row.get("日期")))
                ),
                category=str(row.get("公告类型", "announcement") or "announcement"),
                related_codes=[code] if code.isdigit() and len(code) == 6 else [],
                source_id=announcement_match.group(1) if announcement_match else url[-100:],
            ))
        return items

    @classmethod
    def _select_latest_with_material_reserve(cls, df, *, limit: int):
        """保留最新公告，同时补回时间稍早但具备明确事件身份的公告。"""
        if df is None or getattr(df, "empty", True):
            return df
        ordered = df.sort_values("公告时间", ascending=False) if "公告时间" in df.columns else df
        latest = ordered.head(max(1, limit))
        title_column = "公告标题" if "公告标题" in ordered.columns else "标题" if "标题" in ordered.columns else ""
        if not title_column:
            return latest
        material_mask = ordered[title_column].fillna("").astype(str).str.contains(
            cls._MATERIAL_TITLE_PATTERN,
            regex=True,
        )
        # 上游查询已按“最新4页 + 每个重大事件关键词2页”做了硬上限，
        # 这里不应再按全局时间截断。半年报密集期，较早几天的中标/重组
        # 会被大量当日业绩公告挤出 ``limit * 3``，造成已经抓到却未入库。
        # 保留这个有界结果集中的全部重大事件公告，再统一去重。
        material = ordered[material_mask]
        combined = pd.concat([latest, material], ignore_index=True)
        dedup_columns = [
            column for column in ("代码", title_column, "公告时间", "公告链接")
            if column in combined.columns
        ]
        if dedup_columns:
            combined = combined.drop_duplicates(subset=dedup_columns, keep="first")
        if "公告时间" in combined.columns:
            combined = combined.sort_values("公告时间", ascending=False)
        return combined.head(max(1, limit * 4))

    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        """获取最新公告"""
        try:
            df = await self._fetch_disclosures(lookback_days=7)
            selected = self._select_latest_with_material_reserve(df, limit=limit)
            return self._to_items(selected, limit=max(1, len(selected)))
        except Exception as e:
            logger.error(f"巨潮公告抓取失败: {e}")
            return []

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """获取个股公告"""
        try:
            normalized_code = self._normalize_code(code)
            df = await self._fetch_disclosures(code=normalized_code, lookback_days=45)
            return self._to_items(df, limit=limit, fallback_code=normalized_code)
        except Exception as e:
            logger.error(f"巨潮个股公告抓取失败 [{code}]: {e}")
            return []
