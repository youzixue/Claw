"""数据治理相关数据模型 — 4张表"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, Date, DateTime, Text

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_key = Column(String(50), nullable=False)
    trade_date = Column(Date)
    snapshot_time = Column(DateTime, default=datetime.now, nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(20), default="ok")
