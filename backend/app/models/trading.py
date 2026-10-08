"""交易执行中台模型 — 委托/成交/同步快照"""

from datetime import date, datetime
from sqlalchemy import (Column, Integer, String, Float, Date, DateTime, Text, Index,
                        ForeignKey, CheckConstraint, UniqueConstraint, event)

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
    order_type = Column(String(20), nullable=False)       # limit/market/after_hours_fixed (intent only)
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


class PaperAfterHoursResourceScope(Base):
    """CAS serialization for ONE existing account/security/source/session, not a market pool."""
    __tablename__ = "paper_after_hours_resource_scope"
    scope_key = Column(String(64), primary_key=True)
    account_numeric_id = Column(Integer, ForeignKey("paper_account.id"), nullable=False)
    account_name = Column(String(40), nullable=False)
    code = Column(String(10), nullable=False)
    trade_date = Column(Date, nullable=False)
    source = Column(String(64), nullable=False)
    source_version = Column(String(64), nullable=False)
    session_id = Column(String(160), nullable=False)
    revision = Column(Integer, nullable=False)
    terminal_sequence = Column(Integer, nullable=False)
    lifecycle_prefix_hash = Column(String(64), nullable=False)
    source_quote_at = Column(DateTime, nullable=False)
    source_available_at = Column(DateTime, nullable=False)
    checked_at = Column(DateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("revision >= 0 AND terminal_sequence >= 0", name="ck_af_resource_revision"),
        UniqueConstraint("account_numeric_id", "code", "trade_date", name="uq_af_resource_account_security_day"),
        Index("ix_af_resource_account_date", "account_numeric_id", "trade_date", "code"),
    )


class PaperAfterHoursResourceReceipt(Base):
    """Append-only GROUP of slices bound to one actual book trade and TradeFill."""
    __tablename__ = "paper_after_hours_resource_receipt"
    id = Column(Integer, primary_key=True, autoincrement=True)
    allocation_id = Column(String(64), nullable=False, unique=True)
    scope_key = Column(String(64), ForeignKey("paper_after_hours_resource_scope.scope_key"), nullable=False, index=True)
    trade_fill_id = Column(Integer, ForeignKey("trade_fill.id"), nullable=False, unique=True)
    paper_trade_id = Column(Integer, ForeignKey("paper_trade_log.id"), nullable=False, unique=True)
    order_id = Column(String(40), ForeignKey("trade_order.order_id"), nullable=False)
    protocol_version = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    recorded_at = Column(DateTime, nullable=False)
    __table_args__ = (CheckConstraint("length(payload_json) <= 2097152", name="ck_af_resource_payload"),)


def _reject_af_resource_orm_mutation(mapper, connection, target):
    raise ValueError("fixed-price resource rows require CAS or append-only receipts")


for _af_resource_model in (PaperAfterHoursResourceScope, PaperAfterHoursResourceReceipt):
    event.listen(_af_resource_model, "before_update", _reject_af_resource_orm_mutation)
    event.listen(_af_resource_model, "before_delete", _reject_af_resource_orm_mutation)

from app.trading.paper_after_hours_resource_schema import install_sqlite_guards
event.listen(PaperAfterHoursResourceReceipt.__table__, "after_create", install_sqlite_guards)
