"""外盘指数的前向采集观察；不冒充完整隔夜新闻/美股期货或 session 证据。"""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import math
import json
from loguru import logger

from app.config.settings import settings
from app.news.sources.base import NewsItem, NewsSource


def _finite(value, positive=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and (not positive or number > 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


def validate_index_observation(data):
    """Pure typed-original validation shared by producer and SELECT-only reader."""
    if (not isinstance(data, dict) or data.get("kind") != "index_quote_observation"
            or data.get("protocol") != "global_index_observation_v2_20261008"
            or data.get("provider") != "akshare:index_global_spot_em"
            or data.get("parser_version") != "eastmoney_global_index_fields_v2"
            or not isinstance(data.get("library_version"), str) or not data["library_version"].strip()
            or not isinstance(data.get("code"), str) or not data["code"].strip()
            or not isinstance(data.get("name"), str) or not data["name"].strip()
            or data.get("source_timezone") != "index_local_unknown"
            or data.get("session") != "unknown" or data.get("session_date") is not None
            or data.get("quote_kind") != "index_latest_observation"
            or data.get("quote_clock_certified") is not False
            or data.get("us_index_futures_available") is not False
            or not isinstance(data.get("raw_source_time"), str)):
        raise ValueError("invalid external observation metadata")
    targets = data.get("expected_codes")
    if (not isinstance(targets, list) or len(targets) > 100 or len(set(targets)) != len(targets)
            or any(not isinstance(code, str) or not code.strip() for code in targets)
            or data["code"] not in targets):
        raise ValueError("invalid frozen index targets")
    for key in ("price", "previous_close", "change_pct"):
        value = data.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                  or key != "change_pct" and value <= 0):
            raise ValueError("invalid external number")
    missing = [key for key in ("price", "previous_close", "change_pct") if data.get(key) is None]
    if data.get("missing_fields") != missing:
        raise ValueError("invalid external missing denominator")
    values = {key: data[key] for key in ("code", "name", "price", "previous_close", "change_pct", "raw_source_time")}
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(raw.encode()).hexdigest() != data.get("response_row_hash"):
        raise ValueError("invalid external row hash")
    return True


class GlobalMarketSource(NewsSource):
    """Explicit index target codes, including small/zero moves; not US futures."""
    source_name = "外盘指数观察"
    source_code = "global"

    async def fetch_latest(self, limit: int = 50) -> list[NewsItem]:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        if limit == 0:
            return []
        targets = list(dict.fromkeys(code.strip() for code in settings.NEWS_GLOBAL_INDEX_CODES.split(",") if code.strip()))
        try:
            import akshare as ak
            frame = ak.index_global_spot_em()
        except Exception as exc:
            self.last_observation = {"status": "failed", "error_type": type(exc).__name__}
            logger.warning("外盘指数获取失败: {}", type(exc).__name__)
            return []
        now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        by_code, conflicts = {}, set()
        for _, row in frame.iterrows():
            code, name = row.get("代码"), row.get("名称")
            if not isinstance(code, str) or not isinstance(name, str) or not name.strip() or code not in targets:
                continue
            values = {"code": code, "name": name.strip(), "price": _finite(row.get("最新价"), True),
                      "previous_close": _finite(row.get("昨收价"), True),
                      "change_pct": _finite(row.get("涨跌幅")),
                      "raw_source_time": str(row.get("最新行情时间", ""))}
            if code in by_code and by_code[code] != values:
                conflicts.add(code)
            by_code[code] = values
        selected = [code for code in targets if code in by_code and code not in conflicts][:limit]
        items = []
        for code in selected:
            values = by_code[code]
            raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            payload = {**values, "kind": "index_quote_observation",
                       "protocol": "global_index_observation_v2_20261008",
                       "provider": "akshare:index_global_spot_em", "library_version": getattr(ak, "__version__", "unknown"),
                       "parser_version": "eastmoney_global_index_fields_v2",
                       "response_row_hash": hashlib.sha256(raw.encode()).hexdigest(),
                       "collector_observed_at": now.isoformat(), "source_timezone": "index_local_unknown",
                       "expected_codes": targets, "returned_codes": selected,
                       "missing_codes": sorted(set(targets) - set(selected)), "conflicting_codes": sorted(conflicts),
                       "target_coverage_certified": False,
                       "session": "unknown", "session_date": None, "quote_kind": "index_latest_observation",
                       "quote_clock_certified": False, "us_index_futures_available": False,
                       "missing_fields": [key for key in ("price", "previous_close", "change_pct") if values[key] is None]}
            validate_index_observation(payload)
            item = NewsItem(source=self.source_code, title=f"外盘指数观察（非新闻）: {values['name']}",
                            content=json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                            publish_time=now, source_id=f"global_{code}", category="global")
            item._news_received_at = now
            items.append(item)
        self.last_observation = {"status": "observed" if items else "no_data", "expected_codes": targets,
                                 "returned_codes": selected, "missing_codes": sorted(set(targets) - set(selected)),
                                 "conflicting_codes": sorted(conflicts), "truncated": len(selected) < len(by_code) - len(conflicts),
                                 "observed_at": now.isoformat(), "session_certified": False}
        return items

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        return []
