"""Immutable daily-review snapshots, attributions, and append-only analyst notes."""

from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event

from app.db.session import Base


class DailyReviewSnapshot(Base):
    __tablename__ = "daily_review_snapshot"
    __table_args__ = (
        Index("ix_daily_review_snapshot_lookup", "review_date", "phase", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_key = Column(String(64), nullable=False, unique=True, index=True)
    review_date = Column(Date, nullable=False, index=True)
    analysis_trade_date = Column(Date, nullable=False, index=True)
    phase = Column(String(20), nullable=False, index=True)
    as_of_at = Column(DateTime, nullable=False)
    schema_version = Column(String(80), nullable=False)
    data_version = Column(String(80), nullable=False)
    quality_status = Column(String(20), nullable=False, default="partial")
    quality_score = Column(Float, nullable=False, default=0.0)
    market_regime = Column(String(40))
    payload_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class PromotionReviewAttribution(Base):
    __tablename__ = "promotion_review_attribution"
    __table_args__ = (
        Index(
            "ix_promotion_review_attribution_lookup",
            "outcome_trade_date",
            "target_board",
            "attribution_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    attribution_key = Column(String(64), nullable=False, unique=True, index=True)
    review_snapshot_id = Column(
        Integer,
        ForeignKey("daily_review_snapshot.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    prediction_run_id = Column(Integer, ForeignKey("promotion_prediction_run.id"))
    prediction_snapshot_id = Column(Integer, ForeignKey("promotion_prediction_snapshot.id"))
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(30))
    target_board = Column(Integer, nullable=False)
    prediction_trade_date = Column(Date)
    outcome_trade_date = Column(Date, nullable=False, index=True)
    model_version = Column(String(80))
    market_regime = Column(String(40))
    attribution_type = Column(String(40), nullable=False)
    primary_reason = Column(String(80), nullable=False)
    causal_status = Column(String(30), nullable=False, default="observed_not_causal")
    predicted_probability = Column(Float)
    rank_position = Column(Integer)
    actionable = Column(Integer, nullable=False, default=0)
    evidence_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class ReviewAutomationRun(Base):
    __tablename__ = "review_automation_run"
    __table_args__ = (
        Index("ix_review_automation_run_lookup", "review_date", "job_name", "started_at"),
        Index("ix_review_automation_run_logical", "logical_key", "attempt"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_key = Column(String(64), nullable=False, unique=True, index=True)
    logical_key = Column(String(64), nullable=False, index=True)
    job_name = Column(String(50), nullable=False, index=True)
    review_date = Column(Date, nullable=False, index=True)
    phase = Column(String(20))
    trigger = Column(String(20), nullable=False, default="schedule")
    attempt = Column(Integer, nullable=False, default=1)
    status = Column(String(30), nullable=False, default="running")
    review_snapshot_id = Column(Integer, ForeignKey("daily_review_snapshot.id"))
    regime_snapshot_id = Column(Integer, ForeignKey("market_regime_snapshot.id"))
    quality_status = Column(String(20))
    details_json = Column(Text, nullable=False, default="{}")
    error_message = Column(Text)
    started_at = Column(DateTime, nullable=False, default=datetime.now)
    completed_at = Column(DateTime)


class ReviewAutomationAlert(Base):
    __tablename__ = "review_automation_alert"
    __table_args__ = (
        Index("ix_review_automation_alert_lookup", "review_date", "severity", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_key = Column(String(64), nullable=False, unique=True, index=True)
    automation_run_id = Column(Integer, ForeignKey("review_automation_run.id"), index=True)
    review_date = Column(Date, nullable=False, index=True)
    phase = Column(String(20))
    severity = Column(String(20), nullable=False)
    alert_type = Column(String(50), nullable=False, index=True)
    message = Column(Text, nullable=False)
    evidence_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class DailyReviewNote(Base):
    __tablename__ = "daily_review_note"
    __table_args__ = (
        Index("ix_daily_review_note_lookup", "review_date", "phase", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_date = Column(Date, nullable=False, index=True)
    phase = Column(String(20), nullable=False, index=True)
    category = Column(String(30), nullable=False, default="observation")
    content = Column(Text, nullable=False)
    tags_json = Column(Text, nullable=False, default="[]")
    author = Column(String(40), nullable=False, default="user")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class ReviewGptReport(Base):
    """GPT 自动复盘报告，按 review_date+phase 幂等追加。

    报告由确定性快照生成，不参与任何模型/权重/阈值修改；未配置提供程序时
    生成接口 fail closed。同一 review_date+phase 只保留最新一份。
    """

    __tablename__ = "review_gpt_report"
    __table_args__ = (
        UniqueConstraint("review_date", "phase", name="uq_review_gpt_report_date_phase"),
        Index("ix_review_gpt_report_lookup", "review_date", "phase", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_key = Column(String(64), nullable=False, unique=True, index=True)
    review_date = Column(Date, nullable=False, index=True)
    review_snapshot_id = Column(Integer, ForeignKey("daily_review_snapshot.id"))
    phase = Column(String(20), nullable=False, index=True)
    provider = Column(String(40), nullable=False, default="")
    model = Column(String(80), nullable=False, default="")
    status = Column(String(20), nullable=False, default="generated")
    content_json = Column(Text, nullable=False, default="{}")
    summary = Column(Text, nullable=False, default="")
    error_message = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


def _reject_review_mutation(_mapper, _connection, target) -> None:
    raise RuntimeError(
        f"{target.__class__.__name__} is append-only; create a new record instead"
    )


for _immutable_model in (
    DailyReviewSnapshot,
    PromotionReviewAttribution,
    ReviewAutomationAlert,
    DailyReviewNote,
):
    event.listen(_immutable_model, "before_update", _reject_review_mutation)
    event.listen(_immutable_model, "before_delete", _reject_review_mutation)
