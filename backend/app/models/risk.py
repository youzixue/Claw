"""风控相关数据模型 — 2张表"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, BigInteger, Boolean, Date, DateTime, Text, UniqueConstraint

from app.db.session import Base


class LockupExpiry(Base):
    """限售股解禁"""
    __tablename__ = "lockup_expiry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    unlock_date = Column(Date, nullable=False, index=True)
    unlock_volume = Column(BigInteger)          # 解禁数量(股)
    unlock_ratio = Column(Float)                # 占流通比%
    unlock_type = Column(String(20))            # 首发原股东/定向增发/股权激励
    risk_level = Column(String(10))             # high/medium/low

    __table_args__ = (
        UniqueConstraint("code", "unlock_date", name="uq_lockup_code_date"),
    )


class DataSourceHealth(Base):
    """数据源健康状态"""
    __tablename__ = "data_source_health"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False, index=True)
    api_name = Column(String(100))
    status = Column(String(10), default="up")   # up/down/degraded
    last_success = Column(DateTime)
    last_failure = Column(DateTime)
    fail_streak = Column(Integer, default=0)
    latency_ms = Column(Integer)
    completeness = Column(Float, default=1.0)
    error_msg = Column(Text)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
