"""事件催化评分与个股直连公告上下文。

该模块只负责判断“消息是否足够硬、是否直接关联个股”，不生成交易信号。
技术位置、板块梯队、可交易标签和次日竞价仍由各自引擎继续把关。
"""

import json
import re
import math
import hashlib
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from datetime import date, datetime, timedelta

from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import FinanceNews, NewsContentVersion, NewsAnalysisVersion
from app.models.stock import StockSpot
from app.news.roles import NEWS_ROLE_PROTOCOL, assess_news_roles


NEWS_EVIDENCE_PROTOCOL = "news_pit_v1"


async def verify_news_entities(db: AsyncSession, title: str, content: str) -> list[dict]:
    """Freeze exact, unambiguous local name->code matches in ORIGINAL text.

    No model codes, source related_codes, fuzzy aliases, sector expansion, or
    naked six-digit numbers qualify. StockSpot is read now, never consulted
    during historical replay; the mapping and its validation clock are frozen.
    """
    names = {}
    rows = (await db.execute(select(StockSpot.code, StockSpot.name))).all()
    for code, name in rows:
        code, name = str(code or "").strip(), str(name or "").strip()
        if not re.fullmatch(r"[0-9]{6}", code) or len(name) < 3:
            continue
        if name.upper().lstrip("*").startswith("ST") or name in {
            "中国", "银行", "证券", "科技", "股份", "集团", "能源", "控股", "发展",
        }:
            continue
        names.setdefault(name, set()).add(code)
    text = f"{title or ''}\n{content or ''}"
    matches = []
    for name, codes in sorted(names.items()):
        if name not in text or len(codes) != 1:
            continue
        # Check only matched names, not an O(universe^2) scan for every article.
        if any(name != longer and name in longer and longer in text for longer in names):
            continue
        code = next(iter(codes))
        # Numeric IDs elsewhere in an article (or another entity's code) do
        # not invalidate an exact title name. Only adjacent identity conflicts
        # are evidence against THIS mapping.
        adjacent = re.findall(re.escape(name) + r"[（(\s]*(?:(?:股票|证券)代码[：:]?\s*)?([0-9]{6})(?![0-9])", text)
        adjacent += re.findall(r"(?<![0-9])([0-9]{6})[）)\s]*" + re.escape(name), text)
        if any(value != code for value in adjacent):
            continue
        matches.append({"code": code, "name": name, "method": "exact_unique_stock_name_v1"})
    return matches


def _shanghai_naive(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("news as-of must be a datetime")
    return value.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None) if value.tzinfo else value


async def load_news_evidence_as_of(
    db: AsyncSession, *, as_of_at: datetime,
    start_time: datetime | None = None, limit: int = 1500,
) -> list[SimpleNamespace]:
    """Shared read-only PIT input for direct/sector consumers.

    At most one current content revision per article and its latest completed
    attempt AVAILABLE at as_of_at. Failed/unavailable NLP yields raw neutral
    fields, never a future/older-revision result. Legacy FinanceNews is never
    queried. Empty output means no qualifying evidence, NOT verified zero news.
    Naive clocks are Asia/Shanghai; future cutoff is capped at actual now.
    """
    cutoff = min(_shanghai_naive(as_of_at),
                 datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None))
    if limit <= 0:
        return []
    latest = select(func.max(NewsContentVersion.id).label("id")).where(
        NewsContentVersion.recorded_at <= cutoff,
        or_(
            and_(NewsContentVersion.content_available_at <= cutoff,
                 NewsContentVersion.received_at <= cutoff,
                 NewsContentVersion.origin == "observed"),
            # A newly encountered unproven revision invalidates the older
            # view, but is not itself promoted to time-travel evidence.
            NewsContentVersion.origin == "legacy_unknown",
        ),
    ).group_by(NewsContentVersion.news_id).subquery()
    stmt = select(NewsContentVersion).join(latest, NewsContentVersion.id == latest.c.id).where(
        NewsContentVersion.origin == "observed",
    )
    # Apply publication window only AFTER selecting the visible revision.
    if start_time is not None:
        stmt = stmt.where(NewsContentVersion.publish_time >= _shanghai_naive(start_time))
    stmt = stmt.where(NewsContentVersion.publish_time <= cutoff).order_by(
        NewsContentVersion.publish_time.desc(), NewsContentVersion.id.desc(),
    ).limit(limit)
    versions = list((await db.execute(stmt)).scalars().all())
    if not versions:
        return []
    attempts = (await db.execute(select(NewsAnalysisVersion).where(
        NewsAnalysisVersion.content_version_id.in_([v.id for v in versions]),
        NewsAnalysisVersion.available_at <= cutoff,
        NewsAnalysisVersion.analysis_completed_at <= cutoff,
    ).order_by(NewsAnalysisVersion.available_at.desc(), NewsAnalysisVersion.id.desc()))).scalars().all()
    by_content = {}
    for attempt in attempts:
        by_content.setdefault(attempt.content_version_id, attempt)
    output = []
    for version in versions:
        if (version.protocol_version != NEWS_EVIDENCE_PROTOCOL
                or version.entity_verified_at > cutoff
                or version.received_at > version.content_available_at
                or version.content_available_at < version.recorded_at
                or (version.first_received_at is not None and version.first_received_at > version.received_at)):
            continue
        try:
            raw = json.loads(version.payload_json)
            entities = json.loads(version.entity_evidence_json)
            digest = hashlib.sha256(json.dumps(
                raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode()).hexdigest()
            if (digest != version.content_hash or not isinstance(entities, list)
                    or not isinstance(raw, dict)
                    or not all(isinstance(raw.get(k, ""), str) for k in ("source", "title", "content", "url"))
                    or raw.get("source") != version.source
                    or raw.get("publish_time") != version.publish_time.isoformat()):
                continue
            direct_codes = sorted({e["code"] for e in entities
                if isinstance(e, dict) and re.fullmatch(r"[0-9]{6}", str(e.get("code", "")))
                and e.get("method") == "exact_unique_stock_name_v1"
                and e.get("name") and e["name"] in f"{raw.get('title', '')}\n{raw.get('content', '')}"})
        except (ValueError, TypeError, KeyError):
            continue
        values = {
            "title": raw.get("title", ""), "content": raw.get("content", ""),
            "source": version.source, "publish_time": version.publish_time,
            "related_codes": direct_codes, "related_sectors": [],
            "importance": 5, "sentiment": "neutral", "bull_bear": "neutral",
            "bull_bear_confidence": 0.0, "impact_scope": "stock",
            "summary": "", "nlp_status": "raw",
        }
        attempt = by_content.get(version.id)
        used_attempt = None
        available_at = max(version.content_available_at, version.entity_verified_at)
        if attempt is not None:
            if (attempt.status in {"analyzed", "fallback"}
                    and attempt.protocol_version == NEWS_EVIDENCE_PROTOCOL):
                try:
                    result = json.loads(attempt.result_json)
                    valid_hash = hashlib.sha256(attempt.result_json.encode()).hexdigest() == attempt.result_hash
                    confidence = float(result.get("confidence", 0))
                    if (not valid_hash or type(result.get("confidence", 0)) not in (int, float)
                            or not math.isfinite(confidence) or not 0 <= confidence <= 1
                            or result.get("sentiment") not in {"bullish", "bearish", "positive", "negative", "neutral"}
                            or type(result.get("importance")) is not int or not 1 <= result["importance"] <= 10
                            or not isinstance(result.get("related_sectors", []), list)
                            or not all(isinstance(s, str) for s in result.get("related_sectors", []))
                            or attempt.analysis_completed_at < version.recorded_at
                            or attempt.available_at < attempt.analysis_completed_at):
                        raise ValueError("invalid analysis evidence")
                    # Validate the whole analysis before publishing any fields.
                    # A malformed role payload must not leave copied bullish prose
                    # behind after the exception handler rejects the attempt.
                    analysis_values = {key: result[key] for key in (
                        "importance", "sentiment", "impact_scope", "summary", "related_sectors",
                    ) if key in result}
                    analysis_values["bull_bear"] = "bull" if result.get("sentiment") == "bullish" else (
                        "bear" if result.get("sentiment") == "bearish" else "neutral")
                    analysis_values["bull_bear_confidence"] = confidence
                    analysis_values["nlp_status"] = attempt.status
                    role = result.get("role_evidence")
                    analysis_entities = entities
                    if isinstance(role, dict) and role.get("protocol") == NEWS_ROLE_PROTOCOL:
                        if not isinstance(role.get("entities"), list):
                            raise ValueError("invalid role entities")
                        # Mapping repair is available only with this analysis;
                        # no current dictionary query occurs during replay.
                        analysis_entities = assess_news_roles(
                            raw.get("title"), raw.get("content"), role["entities"],
                        )["entities"]
                        analysis_values["related_codes"] = sorted({e["code"] for e in analysis_entities})
                    values.update(analysis_values)
                    entities = analysis_entities
                    used_attempt = attempt
                    available_at = max(available_at, attempt.available_at)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    pass  # corrupt result cannot supply sentiment/codes/sectors
        # A veto based solely on the already visible original is safe even for
        # old analyses. It supplies no new positive fact and never edits history.
        role_evidence = assess_news_roles(values["title"], values["content"], entities)
        values["role_evidence"] = role_evidence
        values["research_only"] = role_evidence["block_positive_catalyst"]
        values["candidate_codes"] = list(values["related_codes"])
        values["positive_beneficiary_codes"] = []
        if values["research_only"]:
            values.update(sentiment="neutral", bull_bear="neutral",
                          bull_bear_confidence=0.0, related_sectors=[],
                          summary=(values["title"] + "\n" + values["content"]).strip()[:500])
        output.append(SimpleNamespace(
            **values, news_id=version.news_id, content_version_id=version.id,
            analysis_version_id=used_attempt.id if used_attempt else None,
            first_received_at=version.first_received_at, received_at=version.received_at,
            content_available_at=version.content_available_at,
            analysis_completed_at=used_attempt.analysis_completed_at if used_attempt else None,
            available_at=available_at, entity_evidence=entities,
            evidence_protocol=NEWS_EVIDENCE_PROTOCOL, as_of_at=cutoff,
        ))
    return output


# 正式重组/控制权事件从披露到交易所、国资和证监会节点通常跨越数日；
# 保留一周窗口，并依靠下方“普通进展降权 + 时效分”避免旧传闻长期占池。
NEWS_CATALYST_LOOKBACK_DAYS = 7
NEWS_CATALYST_MIN_SCORE = 46.0

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_RISK_TITLE_KEYWORDS = (
    "风险提示",
    "异常波动",
    "严重异常波动",
    "立案",
    "处罚",
    "终止",
    "减持",
    "解除质押",
    "股份冻结",
    "诉讼",
    "亏损",
    "预减",
    "退市风险",
    "暂停上市",
    "限售股上市流通",
    "解除限售上市流通",
)
_SPECIAL_SITUATION_RISK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "risk_warning_review",
        (
            "申请撤销其他风险警示",
            "申请撤销公司股票其他风险警示",
            "申请撤销对公司股票交易实施其他风险警示",
        ),
    ),
    (
        "distress_restructuring",
        (
            "被债权人申请重整",
            "被债权人申请预重整",
            "申请预重整及重整",
            "申请预重整与重整",
            "启动预重整",
            "预重整投资协议",
            "重整投资协议",
        ),
    ),
)
_ROUTINE_TITLE_KEYWORDS = (
    "股东大会",
    "董事会会议",
    "监事会会议",
    "法律意见书",
    "独立董事",
    "公司章程",
    "制度",
    "半年度报告摘要",
    "权益变动报告书",
    "收购报告书摘要",
)
_ROUTINE_PROGRESS_KEYWORDS = (
    "延期回复",
    "延长公司发行股份",
    "延长有效期",
    "暂不召开股东会",
)
_REGULATORY_MILESTONE_RULES: tuple[tuple[str, tuple[str, ...], float], ...] = (
    (
        "m&a_review_approved",
        ("并购重组审核委员会审核通过", "并购重组委审核通过"),
        24.0,
    ),
    (
        "regulatory_approval",
        ("国资委批复", "同意注册", "获准注册", "取得注册批复"),
        22.0,
    ),
    (
        "m&a_review_scheduled",
        ("并购重组审核委员会审核会议安排", "并购重组委审核会议安排"),
        18.0,
    ),
)
_HARD_EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "major_restructuring",
        (
            "重大资产重组",
            "发行股份及支付现金购买资产",
            "发行股份购买资产",
            "重大资产购买报告书",
            "重组报告书",
        ),
    ),
    ("earnings_reversal", ("业绩预增", "业绩大幅增长", "扭亏为盈")),
    ("regulatory_approval", ("获得批复", "获准注册", "获批", "取得注册证")),
    (
        "control_change",
        ("控制权变更", "控制权拟发生变更", "实际控制人变更", "控股股东变更"),
    ),
    ("major_project", ("万千瓦", "重大项目")),
)
_MEDIUM_EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("major_project", ("投资项目", "项目投资")),
    ("major_order", ("中标", "签订重大合同", "订单")),
    ("asset_transaction", ("收购", "出售资产", "资产置换")),
    ("buyback", ("回购股份", "回购公司股份", "首次回购", "增持")),
    ("performance_compensation", ("业绩承诺补偿",)),
)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        number = float(value if value is not None else default)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError, OverflowError):
        return default


def _json_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def normalize_news_codes(values: list) -> list[str]:
    codes: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        for match in _CODE_RE.findall(text):
            codes.add(match)
        if text.isdigit() and len(text) <= 6:
            codes.add(text.zfill(6))
    return sorted(codes)


def news_source_weight(source: str | None) -> float:
    return {
        "cninfo": 1.25,
        "cls": 1.15,
        "em": 1.05,
        "ths": 1.0,
        "sina": 0.95,
        "global": 0.85,
    }.get(str(source or "").strip(), 0.9)


def classify_news_event(title: str | None) -> tuple[str, str, float]:
    """返回 (事件等级, 事件类型, 标题修正分)；风险公告一票否决。"""
    text = str(title or "").strip()
    if not text:
        return "none", "", 0.0
    # 摘帽审核和破产（预）重整是高波动特殊情形，不等同于普通并购重组利好。
    # 即使情绪模型给出 bullish，也不能让它们给非 ST 个股的技术候选加分；
    # 相关标的应留在独立风险观察链路，等待交易所/法院正式决定。
    for event_type, keywords in _SPECIAL_SITUATION_RISK_RULES:
        if any(keyword in text for keyword in keywords):
            return "risk", event_type, -100.0
    profit_decline_match = re.search(
        r"(?:归母)?净利润[^%]{0,18}同比(?:下降|减少)\s*([0-9]+(?:\.[0-9]+)?)%",
        text,
    )
    if profit_decline_match:
        return "risk", "earnings_decline", -100.0
    if any(keyword in text for keyword in _RISK_TITLE_KEYWORDS):
        return "risk", "risk_disclosure", -100.0
    role_guard = assess_news_roles(text, "")
    if role_guard["block_positive_catalyst"]:
        return "routine", role_guard["reasons"][0], -100.0
    profit_growth_match = re.search(
        r"(?:归母)?净利润[^%]{0,18}同比(?:增长|增加)\s*([0-9]+(?:\.[0-9]+)?)%",
        text,
    )
    if profit_growth_match:
        growth_pct = _safe_float(profit_growth_match.group(1))
        if growth_pct >= 50.0:
            return "hard", "earnings_surge", 16.0
        if growth_pct >= 20.0:
            return "medium", "earnings_growth", 8.0
    if "并购重组审核委员会" in text and "审核通过" in text:
        return "hard", "m&a_review_approved", 24.0
    if "并购重组审核委员会" in text and "会议安排" in text:
        return "hard", "m&a_review_scheduled", 18.0
    for event_type, keywords, adjustment in _REGULATORY_MILESTONE_RULES:
        if any(keyword in text for keyword in keywords):
            return "hard", event_type, adjustment
    if any(keyword in text for keyword in _ROUTINE_PROGRESS_KEYWORDS):
        return "routine", "restructuring_progress", -12.0
    if (
        any(keyword in text for keyword in ("重大资产重组", "发行股份", "购买资产"))
        and ("进展公告" in text or "进展的提示性公告" in text)
    ):
        return "routine", "restructuring_progress", -12.0
    if any(keyword in text for keyword in _ROUTINE_TITLE_KEYWORDS):
        return "routine", "routine_disclosure", -12.0
    for event_type, keywords in _HARD_EVENT_RULES:
        if any(keyword in text for keyword in keywords):
            return "hard", event_type, 16.0
    for event_type, keywords in _MEDIUM_EVENT_RULES:
        if any(keyword in text for keyword in keywords):
            return "medium", event_type, 8.0
    return "none", "", 0.0


def score_news_catalyst(news: FinanceNews, trade_date: date) -> float:
    """统一消息评分；兼容已完成 NLP 的新闻，也能保守识别刚入库的公告标题。"""
    event_grade, _event_type, title_adjustment = classify_news_event(getattr(news, "title", ""))
    if (event_grade == "risk" or getattr(news, "research_only", False)
            or assess_news_roles(getattr(news, "title", ""), getattr(news, "content", ""),
                                 getattr(news, "entity_evidence", ()))["block_positive_catalyst"]):
        return 0.0
    bull_bear = str(getattr(news, "bull_bear", "") or "").lower()
    sentiment = str(getattr(news, "sentiment", "") or "").lower()
    if bull_bear == "bear" or sentiment in {"negative", "bearish"}:
        return 0.0
    confidence = _safe_float(getattr(news, "bull_bear_confidence", 0.0))
    importance = _safe_float(getattr(news, "importance", 5), 5.0)
    publish_time = getattr(news, "publish_time", None)
    age_hours = 72.0
    if isinstance(publish_time, datetime):
        next_session_boundary = datetime.combine(trade_date + timedelta(days=1), datetime.min.time())
        age_hours = max(0.0, (next_session_boundary - publish_time).total_seconds() / 3600.0)
    recency_bonus = 14.0 if age_hours <= 12 else 10.0 if age_hours <= 24 else 6.0 if age_hours <= 72 else 2.0
    direction_bonus = 12.0 if bull_bear == "bull" else 6.0 if sentiment in {"positive", "bullish"} else 0.0
    impact_scope = str(getattr(news, "impact_scope", "") or "")
    scope_bonus = 8.0 if impact_scope == "stock" else 5.0 if impact_scope == "sector" else 0.0
    score = (
        importance * 5.0
        + confidence * 22.0
        + direction_bonus
        + scope_bonus
        + recency_bonus
        + news_source_weight(getattr(news, "source", None)) * 6.0
        + title_adjustment
    )
    return max(0.0, min(100.0, score))


async def load_direct_stock_catalyst_map(
    db: AsyncSession,
    trade_date: date,
    *,
    candidate_codes: set[str] | None = None,
    limit: int = 1500,
    min_score: float = NEWS_CATALYST_MIN_SCORE,
    news_end_time: datetime | None = None,
) -> dict[str, dict]:
    """只读截止点已可用且实体已验证的版本；不从可变页面行或板块做代码扩散。"""
    start_dt = datetime.combine(
        trade_date - timedelta(days=NEWS_CATALYST_LOOKBACK_DAYS),
        datetime.min.time(),
    )
    default_end_dt = datetime.combine(trade_date + timedelta(days=1), datetime.min.time())
    # 历史回放默认仍严格截止到交易日 24:00；实时次日预案可以显式传入
    # 下一交易日竞价前的 as-of 时间，以纳入交易所午夜批量披露且不穿越开盘。
    # 显式 as-of 必须是硬截止；不能为了兼容默认日界而用 max() 把截止点
    # 推到未来。上一收盘回放和竞价回放据此保持严格无未来函数。
    end_dt = _shanghai_naive(news_end_time) if news_end_time else default_end_dt
    end_dt = min(end_dt, datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None))
    rows = await load_news_evidence_as_of(db, as_of_at=end_dt, start_time=start_dt, limit=limit)
    normalized_candidates = {
        str(code or "").strip()
        for code in (candidate_codes or set())
        if str(code or "").strip()
    }
    context_map: dict[str, dict] = {}
    aggregate_map: dict[str, dict] = {}
    anchor_close = datetime.combine(trade_date, datetime.min.time()).replace(hour=15)
    for news in rows:
        if news.publish_time >= end_dt:
            continue
        if (getattr(news, "research_only", False)
                or assess_news_roles(news.title, getattr(news, "content", ""),
                                     getattr(news, "entity_evidence", ()))["block_positive_catalyst"]):
            continue
        codes = normalize_news_codes(_json_list(getattr(news, "related_codes", None)))
        if normalized_candidates:
            codes = [code for code in codes if code in normalized_candidates]
        if not codes:
            continue
        event_grade, event_type, _adjustment = classify_news_event(getattr(news, "title", ""))
        score = score_news_catalyst(news, trade_date)
        nlp_positive = str(getattr(news, "bull_bear", "") or "").lower() == "bull" or str(
            getattr(news, "sentiment", "") or ""
        ).lower() in {"positive", "bullish"}
        if score < min_score or event_grade in {"risk", "routine"}:
            continue
        # 中性原始公告必须命中确定性事件词；避免把治理公告、风险提示包装成利好。
        if event_grade == "none" and not nlp_positive:
            continue
        publish_time = getattr(news, "publish_time", None)
        after_anchor_close = bool(
            isinstance(publish_time, datetime)
            and anchor_close <= news.available_at <= end_dt
        )
        high_impact = bool(
            str(getattr(news, "impact_scope", "") or "") == "stock"
            and (
                _safe_int(getattr(news, "importance", 0)) >= 6
                or event_grade == "hard"
            )
        )
        for code in codes:
            aggregate = aggregate_map.setdefault(
                code,
                {
                    "news_count": 0,
                    "news_before_close_count": 0,
                    "news_after_close_count": 0,
                    "news_high_impact_count": 0,
                    "news_hard_event_count": 0,
                    "news_latest_publish_time": "",
                    "news_titles": [],
                    "news_evidence": {
                        "protocol": NEWS_EVIDENCE_PROTOCOL,
                        "as_of_at": end_dt.isoformat(),
                        "availability_basis": "content_and_used_analysis",
                        "versions": [],
                    },
                },
            )
            aggregate["news_count"] += 1
            aggregate["news_titles"].append(str(news.title or "")[:160])
            aggregate["news_evidence"]["versions"].append({
                "news_id": news.news_id,
                "content_version_id": news.content_version_id,
                "analysis_version_id": news.analysis_version_id,
                "first_received_at": news.first_received_at.isoformat() if news.first_received_at else None,
                "content_available_at": news.content_available_at.isoformat(),
                "analysis_completed_at": news.analysis_completed_at.isoformat() if news.analysis_completed_at else None,
                "available_at": news.available_at.isoformat(),
                "publish_time": publish_time.isoformat() if publish_time else None,
                "entity_evidence": [dict(e) for e in news.entity_evidence if e.get("code") == code],
            })
            aggregate[
                "news_after_close_count" if after_anchor_close else "news_before_close_count"
            ] += 1
            if high_impact:
                aggregate["news_high_impact_count"] += 1
            if event_grade == "hard":
                aggregate["news_hard_event_count"] += 1
            if isinstance(publish_time, datetime):
                current_latest = str(aggregate.get("news_latest_publish_time") or "")
                publish_iso = publish_time.isoformat()
                if not current_latest or publish_iso > current_latest:
                    aggregate["news_latest_publish_time"] = publish_iso

            current = context_map.get(code)
            selection_key = (
                {"hard": 3, "medium": 2}.get(event_grade, 1 if nlp_positive else 0),
                1 if str(getattr(news, "source", "") or "") == "cninfo" else 0,
                score,
                publish_time.timestamp() if isinstance(publish_time, datetime) else 0.0,
            )
            current_key = tuple(current.get("_selection_key") or ()) if current else ()
            if current and current_key >= selection_key:
                continue
            context_map[code] = {
                "news_catalyst_score": round(score, 2),
                "news_evidence_protocol": news.evidence_protocol,
                "news_content_version_id": news.content_version_id,
                "news_analysis_version_id": news.analysis_version_id,
                "news_first_received_at": news.first_received_at.isoformat() if news.first_received_at else None,
                "news_content_available_at": news.content_available_at.isoformat(),
                "news_analysis_completed_at": news.analysis_completed_at.isoformat() if news.analysis_completed_at else None,
                "news_available_at": news.available_at.isoformat(),
                "news_as_of_at": news.as_of_at.isoformat(),
                "news_entity_evidence": news.entity_evidence,
                "news_title": str(getattr(news, "title", "") or "")[:160],
                "news_source": str(getattr(news, "source", "") or ""),
                "news_publish_time": publish_time.isoformat() if isinstance(publish_time, datetime) else "",
                "news_importance": _safe_int(getattr(news, "importance", 0)),
                "news_confidence": round(_safe_float(getattr(news, "bull_bear_confidence", 0.0)), 3),
                "news_impact_scope": str(getattr(news, "impact_scope", "") or ""),
                "news_impact_reason": str(
                    getattr(news, "impact_reason", "") or getattr(news, "summary", "") or ""
                )[:160],
                "news_related_sectors": [
                    str(item or "").strip()
                    for item in _json_list(getattr(news, "related_sectors", None))
                    if str(item or "").strip()
                ][:10],
                "news_event_grade": event_grade if event_grade != "none" else "nlp_positive",
                "news_event_type": event_type or "nlp_positive",
                "news_after_close": news.available_at.hour >= 15,
                "news_fresh_after_trade_close": after_anchor_close,
                "news_count": 1,
                # 同股多条消息先选正式公告硬节点，再比较来源、评分和时效，
                # 避免错误板块映射的高分资讯覆盖并购委审核通过公告。
                "_selection_key": selection_key,
            }
    for code, context in context_map.items():
        aggregate = aggregate_map.get(code) or {}
        context.update(aggregate)
        context["news_repeated_direct"] = _safe_int(aggregate.get("news_count")) >= 3
        context["news_direct_high_impact"] = _safe_int(
            aggregate.get("news_high_impact_count")
        ) > 0
        context["news_information_phase"] = (
            "overnight"
            if _safe_int(aggregate.get("news_after_close_count")) > 0
            else "previous_close"
        )
        context.pop("_selection_key", None)
    return context_map
