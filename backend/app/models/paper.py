"""模拟盘账户、成交、前向实验与每日审计数据模型。"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, Date, DateTime, Text, Index, UniqueConstraint, ForeignKey, DDL, event

from app.db.session import Base


class PaperAccount(Base):
    """模拟盘账户"""
    __tablename__ = "paper_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_name = Column(String(30), default="default", nullable=False, index=True)
    strategy = Column(String(30), default="default")  # default/promotion 策略标识
    initial_capital = Column(Float, nullable=False)
    current_capital = Column(Float)
    total_assets = Column(Float)                # 资金+持仓市值
    total_return = Column(Float)                # 累计收益率%
    max_drawdown = Column(Float)                # 最大回撤%
    sharpe_ratio = Column(Float)
    win_rate = Column(Float)
    status = Column(String(10), default="active")  # active/closed


class PaperPosition(Base):
    """模拟盘持仓"""
    __tablename__ = "paper_position"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    buy_price = Column(Float, nullable=False)
    buy_amount = Column(Integer, nullable=False)
    buy_time = Column(DateTime, nullable=False)
    buy_reason = Column(Text)                   # 基于哪个信号
    strategy_version = Column(String(64))       # 建仓时策略版本，后续加仓不覆盖
    entry_sector_code = Column(String(20))      # 首次建仓时的主驱动板块，不随加仓覆盖
    entry_sector_name = Column(String(30))
    current_price = Column(Float)
    profit_loss = Column(Float)                 # 浮动盈亏
    profit_pct = Column(Float)                  # 盈亏比例%
    hold_days = Column(Integer)
    stop_loss_price = Column(Float)
    is_closed = Column(Boolean, default=False)


class PaperTradeLog(Base):
    """模拟盘交易日志"""
    __tablename__ = "paper_trade_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    trade_type = Column(String(5), nullable=False)  # buy/sell
    price = Column(Float, nullable=False)
    amount = Column(Integer, nullable=False)
    trade_time = Column(DateTime, nullable=False, index=True)
    commission = Column(Float)                  # 佣金（不再混入印花税）
    tax = Column(Float, default=0)               # 卖出印花税
    signal_id = Column(String(80))              # 关联信号
    reason = Column(Text)
    realized_pnl = Column(Float)                # 卖出已实现盈亏
    strategy_version = Column(String(64))       # 交易发生时策略版本
    decision_round_id = Column(String(64), index=True)
    fill_round_id = Column(String(64), index=True)
    forced_probe = Column(Boolean, nullable=False, default=False)
    excluded_from_performance = Column(Boolean, nullable=False, default=False)


class PaperSaleAccounting(Base):
    """新卖出的不可变费用分摊证据；绝不为旧成交补写/追认。"""
    __tablename__ = "paper_sale_accounting"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, ForeignKey("paper_trade_log.id"), nullable=False, unique=True)
    account_id = Column(Integer, nullable=False)
    code = Column(String(10), nullable=False)
    version = Column(String(40), nullable=False)
    recorded_at = Column(DateTime, nullable=False)
    payload_json = Column(Text, nullable=False)
    __table_args__ = (Index("ix_paper_sale_accounting_account_code", "account_id", "code"),)


for _action in ("UPDATE", "DELETE"):
    event.listen(PaperSaleAccounting.__table__, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS paper_sale_accounting_no_{_action.lower()} "
        f"BEFORE {_action} ON paper_sale_accounting "
        "BEGIN SELECT RAISE(ABORT, 'sale accounting evidence is append-only'); END"
    ).execute_if(dialect="sqlite"))

event.listen(PaperSaleAccounting.__table__, "after_create", DDL(
    "CREATE TRIGGER IF NOT EXISTS paper_sale_accounting_no_replace "
    "BEFORE INSERT ON paper_sale_accounting WHEN EXISTS "
    "(SELECT 1 FROM paper_sale_accounting WHERE trade_id=NEW.trade_id OR id=NEW.id) "
    "BEGIN SELECT RAISE(ABORT, 'sale accounting evidence is append-only'); END"
).execute_if(dialect="sqlite"))


class PaperAutoTradeLog(Base):
    """模拟盘自动执行日志"""
    __tablename__ = "paper_auto_trade_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, index=True)        # 多账户并行: 策略A=default账户 / 策略B=promotion账户
    run_id = Column(String(40), nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    trigger = Column(String(30), default="manual")
    source = Column(String(30), default="radar")
    code = Column(String(10), index=True)
    name = Column(String(20))
    action = Column(String(20), nullable=False)       # buy/sell/hold/skip/empty
    decision = Column(String(20), nullable=False)     # executed/skipped/blocked/wait
    reason = Column(Text)
    price = Column(Float)
    amount = Column(Integer)
    candidate_score = Column(Float)
    risk_level = Column(String(10))
    risk_json = Column(Text)
    candidate_json = Column(Text)
    executed_trade_id = Column(Integer)
    strategy_version = Column(String(64))       # 生成决策时策略版本
    quote_round_id = Column(String(64), index=True)
    as_of_at = Column(DateTime)
    stage_code = Column(String(32), index=True)
    reason_code = Column(String(48), index=True)
    metric_value = Column(Float)
    threshold_value = Column(Float)
    config_version = Column(String(64))
    code_version = Column(String(64))


class PaperNav(Base):
    """模拟盘净值曲线"""
    __tablename__ = "paper_nav"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    nav = Column(Float, nullable=False)         # 单位净值
    daily_return = Column(Float)                # 日收益率%

    __table_args__ = (
        UniqueConstraint("account_id", "trade_date", name="uq_nav_account_date"),
    )


class PaperShadowEvent(Base):
    """前向影子策略的不可变时点事件。"""

    __tablename__ = "paper_shadow_event"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_paper_shadow_event_key"),
        Index(
            "ix_paper_shadow_event_route_date",
            "route_id",
            "route_version",
            "trade_date",
            "event_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(96), nullable=False)
    route_id = Column(String(40), nullable=False)
    route_version = Column(String(40), nullable=False)
    trade_date = Column(Date, nullable=False, index=True)
    observed_at = Column(DateTime, nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    event_type = Column(String(24), nullable=False, index=True)
    status = Column(String(24), nullable=False)
    price = Column(Float)
    assumed_fill_price = Column(Float)
    change_pct = Column(Float)
    snapshot_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class MomentumRetestConsumerWatermark(Base):
    """A 路由影子引擎的「消费者水位」——每个交易日一行。

    为什么需要它（2026-09-16 复盘，P0）
    ----------------------------------
    `MomentumRetestState.last_at` 在每次 `observe_quote` 都更新，但**只有发生
    状态迁移时**才会 emit 事件、才会被持久化。一只安静待在 `armed` 的股票
    20 分钟不迁移状态 ⇒ 落库的 `last_at` 就停在 20 分钟前。

    进程重启时 `restore()` 恢复的是这个**陈旧**的 `last_at`，首个重启后帧把它
    当连续性基线 ⇒ `gap = now - 陈旧last_at` 远超阈值 ⇒ 该股被 `coverage_blocked`
    **全日终态出局**。2026-09-16 09:51 一次打下 2,844 只（占 A 路由可用池
    89%~99%，近 10 日有 8 日如此），而同期 `quote_round` 实测逐分钟都在正常
    提交、行情从未中断 —— 即报告的是**幻影缺口**。

    本表记录「消费者最后一次成功吃完一轮行情」的时点。它反映的是**真实的消费
    连续性**，与「某只股票有没有换过状态」解耦：

    * 重启耗时几秒 ⇒ 水位就在重启前 ⇒ gap 很小 ⇒ **不再误伤**
    * 真正停机 21 分钟 ⇒ 水位也停在 21 分钟前 ⇒ gap 大 ⇒ **照常失败关闭**

    即严格更准确，且不放松任何 fail-closed 保证。
    """

    __tablename__ = "momentum_retest_consumer_watermark"

    trade_date = Column(Date, primary_key=True)
    route_id = Column(String(40), nullable=False)
    route_version = Column(String(40), nullable=False)
    observed_at = Column(DateTime, nullable=False)   # 最后成功消费的轮次时点
    round_id = Column(String(64))
    updated_at = Column(DateTime, nullable=False, default=datetime.now)


class PaperShadowEvaluation(Base):
    """影子确认信号的追加式多周期结算，不改写原始信号事件。"""

    __tablename__ = "paper_shadow_evaluation"
    __table_args__ = (
        UniqueConstraint(
            "signal_event_key",
            "horizon_days",
            name="uq_paper_shadow_evaluation_signal_horizon",
        ),
        Index(
            "ix_paper_shadow_evaluation_route_horizon",
            "route_id",
            "route_version",
            "horizon_days",
            "signal_trade_date",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_event_key = Column(String(96), nullable=False, index=True)
    route_id = Column(String(40), nullable=False)
    route_version = Column(String(40), nullable=False)
    code = Column(String(10), nullable=False, index=True)
    signal_trade_date = Column(Date, nullable=False, index=True)
    signal_time = Column(DateTime, nullable=False)
    horizon_days = Column(Integer, nullable=False)
    exit_trade_date = Column(Date, nullable=False)
    signal_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    gross_return_pct = Column(Float)
    net_return_pct = Column(Float)
    benchmark_return_pct = Column(Float)
    excess_return_pct = Column(Float)
    max_favorable_pct = Column(Float)
    max_adverse_pct = Column(Float)
    is_positive = Column(Boolean)
    details_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class PaperControlSample(Base):
    """每策略每日一个前向控制样本；不计入 Champion/Challenger 绩效。"""

    __tablename__ = "paper_control_sample"
    __table_args__ = (
        UniqueConstraint("sample_key", name="uq_paper_control_sample_key"),
        Index("ix_paper_control_sample_account_date", "account_id", "trade_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sample_key = Column(String(128), nullable=False)
    account_id = Column(Integer, nullable=False, index=True)
    challenger_account_id = Column(Integer, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    strategy_version = Column(String(64), nullable=False)
    quote_round_id = Column(String(64), index=True)
    observed_at = Column(DateTime, nullable=False)
    source = Column(String(40))
    code = Column(String(10), index=True)
    name = Column(String(20))
    price = Column(Float)
    candidate_score = Column(Float)
    decision = Column(String(24), nullable=False)
    reason_code = Column(String(48), nullable=False)
    reason = Column(Text)
    candidate_json = Column(Text, nullable=False, default="{}")
    next_round_id = Column(String(64))
    next_round_price = Column(Float)
    next_round_fillable_amount = Column(Integer)
    close_price = Column(Float)
    return_pct = Column(Float)
    forced_probe = Column(Boolean, nullable=False, default=False)
    excluded_from_performance = Column(Boolean, nullable=False, default=True)
    finalized_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class PaperDailyOutcome(Base):
    """每账户每日运行/决策终态 SLA，成交不是强制目标。"""

    __tablename__ = "paper_daily_outcome"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "trade_date", "strategy_version",
            name="uq_paper_daily_outcome_account_date_version",
        ),
        Index("ix_paper_daily_outcome_date_status", "trade_date", "terminal_status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    strategy_version = Column(String(64), nullable=False)
    terminal_status = Column(String(32), nullable=False, default="running")
    reason_code = Column(String(48), nullable=False, default="not_finalized")
    reason = Column(Text)
    scan_count = Column(Integer, nullable=False, default=0)
    decision_count = Column(Integer, nullable=False, default=0)
    submitted_order_count = Column(Integer, nullable=False, default=0)
    fill_count = Column(Integer, nullable=False, default=0)
    blocked_count = Column(Integer, nullable=False, default=0)
    first_round_id = Column(String(64))
    last_round_id = Column(String(64))
    control_sample_id = Column(Integer)
    is_terminal = Column(Boolean, nullable=False, default=False)
    details_json = Column(Text, nullable=False, default="{}")
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    finalized_at = Column(DateTime)
