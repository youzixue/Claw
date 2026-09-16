"""新闻页面投影与追加式原文/分析证据（历史投影不是 PIT 证据）。"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, UniqueConstraint, ForeignKey, event, DDL

from app.db.session import Base


class FinanceNews(Base):
    """重大财经新闻"""
    __tablename__ = "finance_news"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False, index=True)  # cls/em/cninfo/ths/sina/global
    title = Column(Text, nullable=False)
    content = Column(Text)
    url = Column(Text)
    publish_time = Column(DateTime, nullable=False, index=True)
    crawl_time = Column(DateTime, default=datetime.now)

    # NLP分析结果
    sentiment = Column(String(10))              # positive/negative/neutral
    importance = Column(Integer, default=5)      # 1-10
    category = Column(String(50))               # policy/earnings/industry/macro...
    summary = Column(Text)
    events_json = Column(Text)                  # JSON: NLP提取事件列表
    nlp_status = Column(String(20), default="raw", index=True)  # raw/analyzing/analyzed/failed/fallback
    sentiment_method = Column(String(20))       # ai/keyword/fallback
    events_method = Column(String(20))          # ai/keyword/fallback
    nlp_error = Column(Text)
    nlp_analyzed_at = Column(DateTime)

    # 关联分析
    related_codes = Column(Text)                # JSON: ["300xxx", "600yyy"]
    related_sectors = Column(Text)              # JSON: ["半导体", "华为汽车"]
    impact_scope = Column(String(20))           # stock/sector/market/global

    # 利好利空
    bull_bear = Column(String(10))              # bull/bear/neutral
    bull_bear_confidence = Column(Float)        # 0-1 置信度
    impact_reason = Column(Text)

    # 去重
    source_id = Column(String(100))
    digest = Column(String(32))                 # 内容hash

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_news_source_id"),
    )


class NewsContentVersion(Base):
    """One observed revision, never an inferred historical arrival.

    Naive datetimes use the application's Asia/Shanghai clock. Legacy imports
    retain null first_received_at/content_available_at and cannot enter PIT.
    A->B->A creates three revisions; identical consecutive polls create one.
    """
    __tablename__ = "news_content_version"

    id = Column(Integer, primary_key=True, autoincrement=True)
    news_id = Column(Integer, ForeignKey("finance_news.id"), nullable=False, index=True)
    content_hash = Column(String(64), nullable=False)
    source = Column(String(20), nullable=False)
    publish_time = Column(DateTime, nullable=True, index=True)
    first_received_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, nullable=True)
    content_available_at = Column(DateTime, nullable=True, index=True)
    recorded_at = Column(DateTime, nullable=False)
    origin = Column(String(30), nullable=False)  # observed / legacy_unknown
    payload_json = Column(Text, nullable=False)
    entity_evidence_json = Column(Text, nullable=False)
    entity_verified_at = Column(DateTime, nullable=False)
    protocol_version = Column(String(40), nullable=False)


class NewsAnalysisVersion(Base):
    """Append one completed attempt, bound to the exact immutable input."""
    __tablename__ = "news_analysis_version"

    id = Column(Integer, primary_key=True, autoincrement=True)
    content_version_id = Column(Integer, ForeignKey("news_content_version.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False)  # analyzed / fallback / failed
    analysis_completed_at = Column(DateTime, nullable=False)
    available_at = Column(DateTime, nullable=False, index=True)
    result_json = Column(Text, nullable=False)
    result_hash = Column(String(64), nullable=False)
    protocol_version = Column(String(40), nullable=False)


def _reject_evidence_mutation(mapper, connection, target):
    raise ValueError("news evidence is append-only")


for _model in (NewsContentVersion, NewsAnalysisVersion):
    event.listen(_model, "before_update", _reject_evidence_mutation)
    event.listen(_model, "before_delete", _reject_evidence_mutation)
    for _action in ("UPDATE", "DELETE"):
        event.listen(_model.__table__, "after_create", DDL(
            f"CREATE TRIGGER IF NOT EXISTS {_model.__tablename__}_no_{_action.lower()} "
            f"BEFORE {_action} ON {_model.__tablename__} "
            "BEGIN SELECT RAISE(ABORT, 'news evidence is append-only'); END"
        ).execute_if(dialect="sqlite"))

