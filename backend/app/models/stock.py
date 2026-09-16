"""股票相关数据模型 — 12张表"""

from datetime import date, datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Date, DateTime, Text, BigInteger, Index, UniqueConstraint, event, DDL,
)
from sqlalchemy.orm import relationship
from app.db.session import Base


# ========== 行情数据 ==========

class StockDaily(Base):
    """日线行情"""
    __tablename__ = "stock_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(BigInteger)
    amount = Column(Float)          # 成交额(元)
    turnover = Column(Float)        # 换手率%
    amplitude = Column(Float)       # 振幅%
    change_pct = Column(Float)      # 涨跌幅%
    prev_close = Column(Float)      # 昨收

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_stock_daily_code_date"),
        Index("ix_stock_daily_date", "trade_date"),
    )


class FundFlow(Base):
    """资金流向"""
    __tablename__ = "fund_flow"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    trade_date = Column(Date, nullable=False, index=True)
    main_net_inflow = Column(Float)         # 主力净流入(元)
    main_net_inflow_pct = Column(Float)     # 主力净流入占比%
    # 来源直接测量的细分；缺失保留NULL，不能从主净额/成交额反算。
    super_net_inflow = Column(Float, nullable=True)      # 超大单净流入(元)
    super_net_inflow_pct = Column(Float, nullable=True)  # 超大单净流入占比%
    big_net_inflow = Column(Float)          # 大单净流入(元)
    big_net_inflow_pct = Column(Float, nullable=True)
    mid_net_inflow = Column(Float)          # 中单净流入(元)
    mid_net_inflow_pct = Column(Float, nullable=True)
    small_net_inflow = Column(Float)        # 小单净流入(元)
    small_net_inflow_pct = Column(Float, nullable=True)
    source = Column(String(20))             # eastmoney/akshare/...
    source_version = Column(String(40))     # 采集/字段映射版本
    observed_at = Column(DateTime)          # 采集观测时点（不改写为源时间）
    source_quote_at = Column(DateTime)      # 供应商报价更新时间；历史未知保留NULL
    received_at = Column(DateTime)          # 该个股所在响应页的实际接收时点

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_fund_flow_code_date"),
    )


# ========== 板块数据 ==========

class SectorInfo(Base):
    """板块信息"""
    __tablename__ = "sector_info"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_code = Column(String(20), nullable=False, unique=True)
    sector_name = Column(String(30), nullable=False)
    sector_type = Column(String(20), nullable=False)  # industry/concept/sw_l1/sw_l2/sw_l3
    source = Column(String(20), nullable=False)        # akshare/eastmoney/sw/sina
    parent_code = Column(String(20))                   # 父板块(申万二级→一级)
    stock_count = Column(Integer)                      # 成分股数
    avg_pe = Column(Float)
    avg_pb = Column(Float)
    avg_dividend = Column(Float)                       # 平均股息率
    kline_name = Column(String(30))                    # AkShare/THS K线接口名称(与sector_name不同时填)
    is_excluded = Column(Integer, default=0)           # 1=无业务关联板块(融资融券/沪股通等), 不展示


class StockSectorMapping(Base):
    """个股→板块映射"""
    __tablename__ = "stock_sector_mapping"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30))
    sector_type = Column(String(20))       # industry/concept
    source = Column(String(20))            # akshare/pywencai/sw/sina
    source_version = Column(String(40))    # 查询与字段映射版本
    observed_at = Column(DateTime)         # 本次映射观测时点
    weight = Column(Float)                 # 权重(申万专用)

    __table_args__ = (
        UniqueConstraint("code", "sector_code", "source", name="uq_mapping_code_sector_source"),
    )


class BoardCons(Base):
    """板块成分股"""
    __tablename__ = "board_cons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30))
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    source = Column(String(20))            # sw/sina/pywencai
    weight = Column(Float)                 # 权重(申万)
    pe = Column(Float)
    pb = Column(Float)
    market_cap = Column(Float)             # 总市值(亿)
    turnover = Column(Float)               # 换手率%

    __table_args__ = (
        UniqueConstraint("sector_code", "code", "source", name="uq_cons_sector_code_source"),
    )


# ========== 涨停/跌停/炸板 ==========

class LimitUpPool(Base):
    """涨停池"""
    __tablename__ = "limit_up_pool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    trade_date = Column(Date, nullable=False, index=True)
    limit_up_time = Column(String(8))          # 涨停时间 HH:MM:SS
    limit_up_price = Column(Float)             # 涨停价
    seal_amount = Column(Float)                # 封板资金(元)
    break_count = Column(Integer, default=0)   # 炸板次数
    consecutive_days = Column(Integer, default=1)  # 连板天数
    limit_up_reason = Column(Text)             # 涨停原因
    turnover = Column(Float)                   # 换手率%
    source = Column(String(20))                # eastmoney/pywencai
    # 软隔离：交易日不在权威K线日历的脏记录标记为隔离，质量审计与预测加载不再使用。
    quarantined = Column(Boolean, nullable=False, default=False, server_default="0", index=True)

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_limit_up_code_date"),
    )


class LimitDownPool(Base):
    """跌停池"""
    __tablename__ = "limit_down_pool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    trade_date = Column(Date, nullable=False, index=True)
    limit_down_time = Column(String(8))
    break_count = Column(Integer, default=0)
    consecutive_days = Column(Integer, default=1)
    reason = Column(Text)
    source = Column(String(20))

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_limit_down_code_date"),
    )


class BrokenLimitPool(Base):
    """炸板池"""
    __tablename__ = "broken_limit_pool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    trade_date = Column(Date, nullable=False, index=True)
    limit_up_time = Column(String(8))
    break_time = Column(String(8))
    seal_duration = Column(Integer)             # 封板时长(秒)
    seal_amount = Column(Float)
    source = Column(String(20))
    limit_up_price = Column(Float)            # 当日涨停价
    close_price = Column(Float)               # 最新/收盘价
    close_at_limit = Column(Boolean)           # 收盘是否重新封住涨停
    final_state = Column(String(24))           # broken/reclosed/unknown

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_broken_limit_code_date"),
    )


# ========== 市场情绪 ==========

class MarketSentiment(Base):
    """市场情绪"""
    __tablename__ = "market_sentiment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, unique=True)
    sentiment_cycle = Column(String(10))        # climax/divergence/freezing/recovery
    limit_up_count = Column(Integer)
    limit_down_count = Column(Integer)
    broken_limit_count = Column(Integer)
    seal_rate = Column(Float)                   # 封板率%
    board_height = Column(Integer)              # 最高连板
    advance_decline_ratio = Column(Float)       # 涨跌比
    turnover_total = Column(Float)              # 沪深成交额(万亿元，避免与换手率混淆)
    main_net_inflow = Column(Float)             # 主力净流入(亿)
    sentiment_score = Column(Float)             # 统一情绪评分
    quality_status = Column(String(16))         # ok/degraded/missing
    quality_reason = Column(Text)               # 降级原因
    breadth_sample_count = Column(Integer)      # 同日有效涨跌家数样本
    breadth_coverage = Column(Float)             # 可交易池宽度覆盖率
    index_avg_change_pct = Column(Float)         # 三大指数平均涨跌幅%
    calculation_version = Column(String(32))     # 评分口径版本
    observed_at = Column(DateTime)               # 观测时点

    __table_args__ = (
        Index("ix_sentiment_date", "trade_date"),
    )


class SectorPersistence(Base):
    """板块持续性"""
    __tablename__ = "sector_persistence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30))
    trade_date = Column(Date, nullable=False, index=True)
    consecutive_days = Column(Integer)          # 连续活跃天数
    limit_up_count = Column(Integer)            # 板块内涨停数
    fund_flow = Column(Float)                   # 板块资金净流入(亿)
    change_pct = Column(Float)                  # 板块涨跌幅%
    strength_score = Column(Float)              # 板块强度评分

    __table_args__ = (
        UniqueConstraint("sector_code", "trade_date", name="uq_persistence_sector_date"),
    )


# ========== 股票标记(核心) ==========

class StockTag(Base):
    """股票分级标记"""
    __tablename__ = "stock_tags"

    code = Column(String(10), primary_key=True)
    name = Column(String(20))
    board_type = Column(String(20), nullable=False)  # main_sh/main_sz/sme/gem/star/bse
    board_tag = Column(String(20), nullable=False)   # tradeable/observe_only/blocked/suspended

    # 动态标记
    is_st = Column(Boolean, default=False)
    is_suspended = Column(Boolean, default=False)
    is_limit_up = Column(Boolean, default=False)
    is_limit_down = Column(Boolean, default=False)
    is_ipo_recent = Column(Boolean, default=False)
    is_delisting = Column(Boolean, default=False)

    ipo_date = Column(Date)
    suspend_reason = Column(Text)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class StockBlacklist(Base):
    """股票黑名单"""
    __tablename__ = "stock_blacklist"

    code = Column(String(10), primary_key=True)
    reason = Column(String(20), nullable=False)   # st/delisting/suspended/ipo_recent
    start_date = Column(Date, nullable=False)
    end_date = Column(Date)                        # NULL=持续
    auto_expire = Column(Boolean, default=False)
    source = Column(String(20), default="auto")    # auto/manual


# ========== 竞价数据 ==========

class AuctionData(Base):
    """集合竞价数据"""
    __tablename__ = "auction_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    auction_time = Column(String(8), nullable=False)  # HH:MM:SS
    auction_price = Column(Float)
    auction_volume = Column(BigInteger)
    auction_amount = Column(Float)
    prev_close = Column(Float)
    open_change = Column(Float)                       # 竞价涨跌幅%
    volume_ratio = Column(Float)                      # 量比
    is_cancelled = Column(Boolean, default=False)     # 来源显式撤单标记，不由价格变化推断
    # 026: 历史NULL保持unknown；原始量不改单位，由volume_unit解释。
    source = Column(String(20))
    source_version = Column(String(64))
    source_quote_at = Column(DateTime)
    received_at = Column(DateTime)
    observed_at = Column(DateTime)
    price_basis = Column(String(32))
    volume_basis = Column(String(32))
    volume_unit = Column(String(16))
    amount_unit = Column(String(16))

    __table_args__ = (
        UniqueConstraint("code", "trade_date", "auction_time", name="uq_auction_code_date_time"),
    )


# ========== 融资融券 ==========

class MarginData(Base):
    """融资融券数据"""
    __tablename__ = "margin_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    margin_buy = Column(Float)                  # 融资买入额
    margin_balance = Column(Float)              # 融资余额
    margin_change = Column(Float)               # 余额环比变化%
    short_sell = Column(Float)                  # 融券卖出量
    short_balance = Column(Float)               # 融券余额
    total_balance = Column(Float)               # 融资融券余额

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_margin_code_date"),
    )


# ========== 实时行情快照 ==========

class StockSpot(Base):
    """个股实时行情快照 — 腾讯qt.gtimg.cn 23字段, 单行覆盖"""
    __tablename__ = "stock_spot"

    code = Column(String(10), primary_key=True)       # 6位代码 如 000001
    name = Column(String(20))                          # 股票名称

    # 价格(6)
    price = Column(Float)                              # [3] 最新价
    prev_close = Column(Float)                         # [4] 昨收
    open = Column(Float)                               # [5] 开盘价
    high = Column(Float)                               # [33] 最高价
    low = Column(Float)                                # [34] 最低价
    limit_up = Column(Float)                           # [47] 涨停价
    limit_down = Column(Float)                         # [48] 跌停价

    # 涨跌(4)
    change_pct = Column(Float)                         # [32] 涨跌幅%
    change_amt = Column(Float)                         # 涨跌额
    amplitude = Column(Float)                          # [43] 振幅%
    min5_change = Column(Float)                        # [62] 5分钟涨跌%

    # 量能(4)
    volume = Column(BigInteger)                        # [6] 成交量(手)
    amount = Column(Float)                             # 成交额(元) — 从[6](手)*100股*VWAP近似
    turnover = Column(Float)                           # [38] 换手率%
    volume_ratio = Column(Float)                       # [49] 量比

    # 资金(3)
    main_net_inflow = Column(Float)                    # 兼容旧列；腾讯[50]是五档委差(手)，新采集NULL，资金取FundFlow
    avg_price = Column(Float)                          # [51] 均价VWAP
    bid_ratio = Column(Float)                          # [74] 委比%

    # 五档盘口(20)
    bid1_price = Column(Float)
    bid1_volume = Column(BigInteger)
    bid2_price = Column(Float)
    bid2_volume = Column(BigInteger)
    bid3_price = Column(Float)
    bid3_volume = Column(BigInteger)
    bid4_price = Column(Float)
    bid4_volume = Column(BigInteger)
    bid5_price = Column(Float)
    bid5_volume = Column(BigInteger)

    ask1_price = Column(Float)
    ask1_volume = Column(BigInteger)
    ask2_price = Column(Float)
    ask2_volume = Column(BigInteger)
    ask3_price = Column(Float)
    ask3_volume = Column(BigInteger)
    ask4_price = Column(Float)
    ask4_volume = Column(BigInteger)
    ask5_price = Column(Float)
    ask5_volume = Column(BigInteger)

    # 盘口汇总
    bid_depth_5 = Column(BigInteger)                   # 买1-5总量
    ask_depth_5 = Column(BigInteger)                   # 卖1-5总量
    orderbook_imbalance = Column(Float)                # 五档买卖盘失衡(-1~1)
    bid_ask_spread = Column(Float)                     # 买一卖一价差
    seal_quality_score = Column(Float)                 # 涨停封单质量(0-100)
    support_strength_score = Column(Float)             # 盘口承接强度(0-100)
    withdrawal_ratio = Column(Float)                   # 相对上一快照买盘撤单比(0-1)

    # 估值(4)
    circ_market_cap = Column(Float)                    # [44] 流通市值(亿) — 入库时已除1e8
    pe_ttm = Column(Float)                             # [39] PE(TTM)
    pb = Column(Float)                                 # [46] PB(高估值股有偏差)
    dividend_yield = Column(Float)                     # [64] 股息率TTM%

    # 增长(2)
    net_profit_growth = Column(Float)                  # [79] 净利润增速%

    # 元数据：源行情时点、接收时点与数据库提交时点分离，旧行允许为空并回退 updated_at。
    source_quote_at = Column(DateTime)
    received_at = Column(DateTime)
    quote_round_id = Column(String(64), index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class QuoteRound(Base):
    """一次全市场行情提交的不可变清单；明细保存在 Parquet，不灌入 SQLite。"""

    __tablename__ = "quote_round"
    __table_args__ = (
        Index("ix_quote_round_date_commit", "trade_date", "committed_at"),
        Index("ix_quote_round_quality_date", "quality_status", "trade_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(String(64), nullable=False, unique=True, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    source = Column(String(24), nullable=False, default="tencent")
    source_min_at = Column(DateTime)
    source_max_at = Column(DateTime)
    received_min_at = Column(DateTime)
    received_max_at = Column(DateTime)
    committed_at = Column(DateTime, nullable=False)
    # 所有下游组件共同可见的数据水位，使用最保守的源时间而非任务开始时间。
    as_of_at = Column(DateTime, nullable=False, index=True)
    expected_count = Column(Integer, nullable=False)
    received_count = Column(Integer, nullable=False)
    source_time_count = Column(Integer, nullable=False, default=0)
    coverage = Column(Float, nullable=False, default=0)
    source_time_coverage = Column(Float, nullable=False, default=0)
    quality_status = Column(String(16), nullable=False, default="degraded")
    quality_reason = Column(Text)
    component_watermarks_json = Column(Text, nullable=False, default="{}")
    config_version = Column(String(64), nullable=False)
    code_version = Column(String(64), nullable=False)
    archive_status = Column(String(16), nullable=False, default="pending")
    archive_path = Column(Text)
    minute_archive_path = Column(Text)
    focus_path = Column(Text)
    focus_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


# ========== 每日基本面快照（前向积累，用于历史复盘） ==========

class StockFundamentalDaily(Base):
    """每个交易日截面基本面快照。

    只在前向逐日采集，绝不回填历史；复盘遇到缺失日期时明确标记
    ``unavailable_for_historical_snapshot``，避免用当前截面冒充历史真值。
    """
    __tablename__ = "stock_fundamental_daily"
    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_fundamental_daily_code_date"),
        Index("ix_fundamental_daily_date", "trade_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    trade_date = Column(Date, nullable=False, index=True)
    pe_ttm = Column(Float)
    pb = Column(Float)
    net_profit_growth = Column(Float)
    circ_market_cap = Column(Float)
    source = Column(String(20), default="tencent_spot")
    created_at = Column(DateTime, default=datetime.now)


# ========== 日K线(前复权) ==========

class StockKline(Base):
    """兼容日K投影：THS前复权快照与腾讯当日未复权报价，不是同口径PIT历史。"""
    __tablename__ = "stock_kline"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)

    # OHLCV
    open = Column(Float)                               # 开盘价(前复权)
    close = Column(Float)                              # 收盘价(前复权)
    high = Column(Float)                               # 最高价
    low = Column(Float)                                # 最低价
    volume = Column(BigInteger)                        # 成交量(股, 前复权单位)
    amount = Column(Float)                             # 成交额(元)

    # 派生字段
    turnover = Column(Float)                           # 换手率%
    change_pct = Column(Float)                         # 涨跌幅%
    prev_close = Column(Float)                         # 昨收(前复权)

    source = Column(String(20), default="ths")         # 数据源 ths/akshare

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_kline_code_date"),
    )


class StockKlineObservation(Base):
    """Unreviewed content versions, never backdated availability or automatic truth.

    recorded_at is the local pre-commit recording clock. available_at stays NULL:
    this catalog is not authorized for historical PIT features. Repeated identical
    content/disposition is deduplicated; it is not a full poll-event history.
    """
    __tablename__ = "stock_kline_observation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    trade_date = Column(Date, nullable=False)
    origin = Column(String(30), nullable=False)
    disposition = Column(String(30), nullable=False)
    payload_json = Column(Text, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    recorded_at = Column(DateTime, nullable=False)
    available_at = Column(DateTime, nullable=True)
    source_version = Column(String(50), nullable=False)
    price_basis = Column(String(40), nullable=False)
    quality_issues_json = Column(Text, nullable=False)
    protocol_version = Column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("code", "trade_date", "origin", "disposition", "payload_hash",
                         name="uq_kline_observation_content"),
        Index("ix_kline_observation_code_date", "code", "trade_date"),
        Index("ix_kline_observation_recorded_at", "recorded_at"),
    )


def _reject_kline_observation_mutation(mapper, connection, target):
    raise ValueError("K-line evidence is append-only")


event.listen(StockKlineObservation, "before_update", _reject_kline_observation_mutation)
event.listen(StockKlineObservation, "before_delete", _reject_kline_observation_mutation)
for _kline_action in ("UPDATE", "DELETE"):
    event.listen(StockKlineObservation.__table__, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS stock_kline_observation_no_{_kline_action.lower()} "
        f"BEFORE {_kline_action} ON stock_kline_observation "
        "BEGIN SELECT RAISE(ABORT, 'K-line evidence is append-only'); END"
    ).execute_if(dialect="sqlite"))
