"""回测结果模型 — 独立于模拟盘账户."""

from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text, Index

from app.db.session import Base


class BacktestRun(Base):
    """一次回测运行."""

    __tablename__ = "backtest_run"

    run_id = Column(String(36), primary_key=True)
    run_type = Column(String(20), nullable=False, index=True)  # signal/strategy
    status = Column(String(20), nullable=False, default="completed")
    start_date = Column(Date)
    end_date = Column(Date)
    initial_capital = Column(Float)
    final_capital = Column(Float)
    total_return_pct = Column(Float)
    max_drawdown_pct = Column(Float)
    sharpe_ratio = Column(Float)
    win_rate_pct = Column(Float)
    total_trades = Column(Integer, default=0)
    config_json = Column(Text)
    metrics_json = Column(Text)
    message = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BacktestDailyValue(Base):
    """回测每日权益."""

    __tablename__ = "backtest_daily_value"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    nav = Column(Float, nullable=False)
    cash = Column(Float)
    position_value = Column(Float)
    total_value = Column(Float, nullable=False)
    position_count = Column(Integer, default=0)

    __table_args__ = (
        Index("ix_backtest_daily_run_date", "run_id", "trade_date"),
    )


class BacktestTrade(Base):
    """回测交易记录."""

    __tablename__ = "backtest_trade"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    trade_type = Column(String(5), nullable=False)  # buy/sell
    trade_date = Column(Date, nullable=False, index=True)
    price = Column(Float, nullable=False)
    shares = Column(Integer, default=0)
    amount = Column(Float, default=0)
    pnl = Column(Float)
    commission = Column(Float, default=0)
    stamp_tax = Column(Float, default=0)


class BacktestSignalResult(Base):
    """信号回测逐信号收益."""

    __tablename__ = "backtest_signal_result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    signal_date = Column(Date, nullable=False, index=True)
    buy_price = Column(Float)
    score = Column(Float)
    signal_type = Column(String(30))
    returns_json = Column(Text)

