"""交易执行中台模型 — 委托/成交/同步快照"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Text, Index

from app.db.session import Base


class TradeOrder(Base):
    """交易委托"""
    __tablename__ = "trade_order"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(40), nullable=False, unique=True, index=True)
    broker = Column(String(30), nullable=False, default="paper")
    account_id = Column(String(40), default="default")
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    side = Column(String(4), nullable=False)              # buy/sell
    order_type = Column(String(20), nullable=False)       # limit/market
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    filled_quantity = Column(Integer, default=0)
    avg_fill_price = Column(Float)
    status = Column(String(20), nullable=False)           # pending/submitted/filled/canceled/rejected/risk_blocked
    strategy_id = Column(String(80))
    strategy_version = Column(String(64))
    signal_id = Column(String(80))
    source = Column(String(40))
    reason = Column(Text)
    idempotency_key = Column(String(160), unique=True, index=True)
    decision_round_id = Column(String(64), index=True)
    last_fill_round_id = Column(String(64), index=True)
    decision_at = Column(DateTime)
    as_of_at = Column(DateTime)
    config_version = Column(String(64))
    code_version = Column(String(64))
    risk_level = Column(String(10))
    risk_json = Column(Text)
    external_order_id = Column(String(80), index=True)
    error_message = Column(Text)
    trade_date = Column(Date, nullable=False, default=date.today, index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_trade_order_status_date", "status", "trade_date"),
        Index("ix_trade_order_code_date", "code", "trade_date"),
    )


class TradeFill(Base):
    """成交回报"""
    __tablename__ = "trade_fill"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fill_id = Column(String(40), nullable=False, unique=True, index=True)
    order_id = Column(String(40), nullable=False, index=True)
    broker = Column(String(30), nullable=False, default="paper")
    external_order_id = Column(String(80), index=True)
    code = Column(String(10), nullable=False, index=True)
    side = Column(String(4), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    commission = Column(Float, default=0)
    tax = Column(Float, default=0)
    realized_pnl = Column(Float)
    broker_trade_id = Column(String(80), index=True)
    raw_json = Column(Text)
    decision_round_id = Column(String(64), index=True)
    fill_round_id = Column(String(64), index=True)
    trade_date = Column(Date, nullable=False, default=date.today, index=True)
    filled_at = Column(DateTime, default=datetime.now, nullable=False, index=True)


class BrokerSyncSnapshot(Base):
    """券商账户/持仓同步快照"""
    __tablename__ = "broker_sync_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String(30), nullable=False, default="paper", index=True)
    account_id = Column(String(40), default="default")
    snapshot_type = Column(String(20), nullable=False)    # account/positions
    payload_json = Column(Text, nullable=False)
    status = Column(String(20), default="ok")
    error_message = Column(Text)
    synced_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
