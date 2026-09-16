"""数据治理相关数据模型。"""

from datetime import date, datetime
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.db.session import Base


class TradeCalendarModel(Base):
    """交易日历"""
    __tablename__ = "trade_calendar"

    trade_date = Column(Date, primary_key=True)
    is_trade_day = Column(Boolean, nullable=False)
    session_type = Column(String(20))           # full/morning_only/half_day
    note = Column(String(50))                   # 调休/节假日说明


class BackfillTask(Base):
    """数据回填任务"""
    __tablename__ = "backfill_task"

    id = Column(Integer, primary_key=True, autoincrement=True)
    data_type = Column(String(30), nullable=False)  # quote/fund_flow/sector/news
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(10), default="pending")  # pending/running/done/failed
    progress = Column(Float, default=0.0)
    total_count = Column(Integer)
    done_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime)


class DashboardSnapshot(Base):
    """总览快照缓存"""
    __tablename__ = "dashboard_snapshot"
    __table_args__ = (
        Index(
            "ix_dashboard_snapshot_lookup",
            "snapshot_key",
            "trade_date",
            "status",
            "snapshot_time",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_key = Column(String(50), nullable=False)
    trade_date = Column(Date)
    snapshot_time = Column(DateTime, default=datetime.now, nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(20), default="ok")


class DataQualityRun(Base):
    """One immutable prediction-data audit execution."""

    __tablename__ = "data_quality_run"
    __table_args__ = (
        Index("ix_data_quality_run_lookup", "trade_date", "run_type", "started_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_type = Column(String(30), nullable=False, default="prediction_gate")
    trade_date = Column(Date, index=True)
    snapshot_context = Column(String(40), nullable=False, default="")
    status = Column(String(20), nullable=False, default="running")
    gate_passed = Column(Boolean, nullable=False, default=False)
    issue_count = Column(Integer, nullable=False, default=0)
    blocking_count = Column(Integer, nullable=False, default=0)
    summary_json = Column(Text, nullable=False, default="{}")
    started_at = Column(DateTime, nullable=False, default=datetime.now)
    completed_at = Column(DateTime)


class DataQualityIssue(Base):
    """Auditable data defect; source rows are quarantined rather than deleted."""

    __tablename__ = "data_quality_issue"
    __table_args__ = (
        Index("ix_data_quality_issue_lookup", "issue_type", "trade_date", "severity"),
        Index("ix_data_quality_issue_run", "run_id", "severity"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("data_quality_run.id"), nullable=False)
    severity = Column(String(20), nullable=False, default="warning")
    dataset = Column(String(40), nullable=False)
    issue_type = Column(String(60), nullable=False)
    trade_date = Column(Date, index=True)
    code = Column(String(10), index=True)
    message = Column(Text, nullable=False)
    evidence_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    resolved_at = Column(DateTime)


class DataWatermark(Base):
    """Latest completeness watermark for a dataset/trade-date pair."""

    __tablename__ = "data_watermark"
    __table_args__ = (
        UniqueConstraint("dataset", "trade_date", name="uq_data_watermark_dataset_date"),
        Index("ix_data_watermark_status", "status", "trade_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset = Column(String(40), nullable=False)
    trade_date = Column(Date, nullable=False)
    observed_at = Column(DateTime, nullable=False, default=datetime.now)
    max_available_at = Column(DateTime)
    record_count = Column(Integer, nullable=False, default=0)
    expected_count = Column(Integer)
    completeness = Column(Float, nullable=False, default=0.0)
    status = Column(String(20), nullable=False, default="missing")
    details_json = Column(Text, nullable=False, default="{}")
