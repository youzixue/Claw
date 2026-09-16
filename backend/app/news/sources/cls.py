"""财联社新闻源 — 最快资讯

使用财联社电报 nodeapi
"""

from datetime import datetime
import hashlib
import re
import time
from typing import Optional
from loguru import logger

from app.news.sources.base import NewsItem, NewsSource


class ClsSource(NewsSource):
    """财联社新闻源"""

    source_name = "财联社"
    source_code = "cls"

    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        """获取最新财联社电报"""
        try:
            import requests

            app = "CailianpressWeb"
            os = "web"
            rn = 80
            sv = "7.7.5"
            last_time = int(time.time())
            rows = []
            seen_ids = set()
            target = max(limit, 80)
            for _ in range(4):
                sign_text = f"app={app}&last_time={last_time}&os={os}&rn={rn}&sv={sv}"
                sign = hashlib.md5(hashlib.sha1(sign_text.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()
                url = f"https://www.cls.cn/nodeapi/telegraphList?{sign_text}&sign={sign}"
                response = requests.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://www.cls.cn/telegraph",
                        "Accept": "application/json, text/plain, */*",
                    },
                    timeout=10,
                )
                response.raise_for_status()
                page_rows = response.json().get("data", {}).get("roll_data", [])
                if not page_rows:
                    break
                for row in page_rows:
                    row_id = row.get("id") or row.get("ctime") or row.get("content")
                    if row_id in seen_ids:
                        continue
                    seen_ids.add(row_id)
                    rows.append(row)
                if len(rows) >= target:
                    break
                last_ctime = min(int(row.get("ctime") or last_time) for row in page_rows)
                if last_ctime >= last_time:
                    break
                last_time = last_ctime
            items = []
            for row in rows[:limit]:
                content = str(row.get("content") or row.get("brief") or "")
                title = str(row.get("title") or content[:60])
                items.append(NewsItem(
                    source=self.source_code,
                    title=title,
                    content=content,
                    url=str(row.get("shareurl", "")),
                    publish_time=datetime.fromtimestamp(int(row.get("ctime") or time.time())),
                    source_id=str(row.get("id") or hashlib.md5(content.encode("utf-8")).hexdigest())[:100],
                    category="telegraph",
                    related_codes=_extract_cls_codes(row),
                    related_sectors=_extract_cls_sectors(row),
                ))
            return items
        except Exception as e:
            logger.error(f"财联社抓取失败: {e}")
            return []

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """获取个股新闻"""
        try:
            import akshare as ak
            df = ak.stock_individual_info_em(symbol=code)
            # 个股新闻接口
            items = []
            return items[:limit]
        except Exception as e:
            logger.error(f"财联社个股新闻抓取失败 [{code}]: {e}")
            return []


def _extract_cls_codes(row: dict) -> list[str]:
    codes = set()
    for stock in row.get("stock_list") or []:
        stock_id = str(stock.get("StockID") or stock.get("stock_id") or "")
        match = re.search(r"(\d{6})", stock_id)
        if match:
            codes.add(match.group(1))
    text = f"{row.get('title') or ''} {row.get('content') or ''}"
    codes.update(re.findall(r"(?<!\d)[0368]\d{5}(?!\d)", text))
    return sorted(codes)


def _extract_cls_sectors(row: dict) -> list[str]:
    sectors = set()
    for key in ("plate_list", "subjects"):
        for item in row.get(key) or []:
            name = item.get("name") or item.get("plate_name") or item.get("subject_name")
            if name:
                sectors.add(str(name))
    return sorted(sectors)


def pd_to_datetime(val) -> Optional[datetime]:
    """pandas Timestamp → datetime"""
    if val is None:
        return None
    try:
        if hasattr(val, "to_pydatetime"):
            dt = val.to_pydatetime()
        elif len(str(val).strip()) == 10:
            dt = datetime.strptime(str(val).strip(), "%Y-%m-%d")
        else:
            dt = datetime.strptime(str(val)[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=None)
    except Exception:
        return None
