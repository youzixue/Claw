"""板块相关数据模型 — 6张表

板块生命周期状态机(核心):
- dormant: 休眠(无涨停/无资金)
- emerging: 刚启动(首板出现,资金初进)
- accelerating: 加速(连板梯队形成,资金持续)
- climax: 高潮(批量涨停,龙头高度>5板)
- diverging: 分化(掉队股增多,后排开始跌)
- declining: 退潮(板块达到高点后持续回调,龙头断板/梯队瓦解/K线破位/资金出逃)
- one_day: 一日游(当天涨停,次日无持续)

主线判定标准:
- 连续>=3天处于emerging/accelerating/climax状态
- 有明确的连板梯队(至少3只连板股,高度>=3)
- 有清晰的概念龙头(高度>=3板)
- 近5天涨停总数>=20只

板块口径:
- sector_type: concept(概念)/industry(行业)
- source: pywencai(统一), K线数据用AkShare补充

板块K线数据:
- 概念板块K线: AkShare stock_board_concept_index_ths (含OHLCV)
- 行业板块K线: AkShare stock_board_industry_index_ths (含OHLCV)
- 历史: 概念60天/行业250天
- 涨跌幅/振幅: AkShare不返回, 采集时自行计算
- 均线/趋势: 采集时计算(MA5/MA10/MA20/MA5量/trend_state/vol_ratio/support/resistance)
- 用途: 趋势判断、支撑压力位、突破确认、技术因子计算
"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Text, UniqueConstraint, Index

from app.db.session import Base


class SectorRotation(Base):
    """板块轮动信号 — 记录板块间资金流向"""
    __tablename__ = "sector_rotation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    from_sector = Column(String(30))               # 流出板块代码
    to_sector = Column(String(30))                 # 流入板块代码
    flow_amount = Column(Float)                    # 流动金额(亿)
    rotation_type = Column(String(20))             # 轮动类型: gradual/sudden

    __table_args__ = (
        Index("ix_rotation_date", "trade_date"),
    )


class SectorStrength(Base):
    """板块强弱排名(保留)"""
    __tablename__ = "sector_strength"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30), nullable=False)
    sector_type = Column(String(20), nullable=False)  # concept/industry
    rank = Column(Integer)                      # 当日排名
    rank_change = Column(Integer)               # 排名变化
    strength_score = Column(Float)              # 强弱评分
    change_pct = Column(Float)                  # 板块涨跌幅%
    fund_flow = Column(Float)                   # 资金净流入(亿)
    limit_up_count = Column(Integer)            # 涨停家数
    consecutive_days = Column(Integer)          # 连续活跃天数
    is_hot = Column(Integer, default=0)         # 是否热门(0/1)

    __table_args__ = (
        UniqueConstraint("trade_date", "sector_code", name="uq_strength_date_sector"),
        Index("ix_strength_sector_date", "sector_code", "trade_date"),
    )


class SectorLifecycle(Base):
    """板块生命周期状态(核心重构表)
    
    记录每个板块每天的生命周期状态
    """
    __tablename__ = "sector_lifecycle"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30), nullable=False)
    sector_type = Column(String(20), nullable=False)  # concept/industry
    
    # 生命周期状态
    lifecycle_state = Column(String(20), nullable=False, default="dormant")  
    # dormant/emerging/accelerating/climax/diverging/declining/one_day
    
    # 状态评分(0-100)
    state_score = Column(Float, default=0)      # 状态强度评分
    
    # 涨停数据
    limit_up_count = Column(Integer, default=0)         # 涨停家数
    first_board_count = Column(Integer, default=0)      # 首板数量
    consecutive_board_count = Column(Integer, default=0) # 连板家数
    max_board_height = Column(Integer, default=0)       # 最高连板数
    
    # 龙头股信息(JSON格式存储)
    leader_stocks = Column(Text)                # 龙头股列表 [{code,name,height}]
    ladder_stocks = Column(Text)                # 连板梯队 [{height:[{code,name}]}]
    
    # 资金数据
    fund_flow = Column(Float, default=0)        # 资金净流入(亿)
    fund_flow_3d = Column(Float, default=0)     # 3日累计资金(亿)
    
    # 持续性指标
    active_days = Column(Integer, default=0)    # 当前周期活跃天数
    total_active_5d = Column(Integer, default=0) # 近5天活跃天数
    
    # 质量指标
    quality_score = Column(Float, default=0)    # 板块质量分(梯队完整度)
    is_main_line = Column(Integer, default=0)   # 是否主线(0/1)

    # K线技术因子(从SectorKline汇总)
    kline_trend = Column(String(20))            # K线趋势: up/down/sideways/breakout_up/breakout_down
    kline_vol_ratio = Column(Float)             # 量比(当日/MA5量)
    kline_support = Column(Float)               # 支撑位
    kline_resistance = Column(Float)            # 压力位
    kline_ma5 = Column(Float)                   # 5日均线
    kline_ma20 = Column(Float)                  # 20日均线
    kline_close = Column(Float)                 # 当日收盘指数
    
    __table_args__ = (
        UniqueConstraint("trade_date", "sector_code", name="uq_lifecycle_date_sector"),
        Index("ix_lifecycle_state_date", "lifecycle_state", "trade_date"),
        Index("ix_lifecycle_sector_date", "sector_code", "trade_date"),
    )


class SectorRotationCalendar(Base):
    """板块轮动日历(取代桑基图)
    
    日线级别记录每个板块的状态变化
    用于回溯板块活跃史
    """
    __tablename__ = "sector_rotation_calendar"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30), nullable=False)
    sector_type = Column(String(20), nullable=False)
    
    # 当日状态
    lifecycle_state = Column(String(20), nullable=False)
    state_change = Column(String(20), default="unchanged")  # upgraded/downgraded/unchanged/new
    
    # 状态变化描述
    change_reason = Column(Text)                # 变化原因(涨停数增加/龙头断板等)
    
    # 排名数据
    rank = Column(Integer)                      # 当日排名
    rank_change = Column(Integer)               # 排名变化
    
    # 活跃标记
    is_active = Column(Integer, default=0)      # 当日是否活跃(0/1)
    is_emerging = Column(Integer, default=0)    # 是否刚启动(0/1)
    is_climax = Column(Integer, default=0)      # 是否高潮(0/1)
    is_declining = Column(Integer, default=0)   # 是否退潮(0/1)
    
    __table_args__ = (
        UniqueConstraint("trade_date", "sector_code", name="uq_calendar_date_sector"),
        Index("ix_calendar_active_date", "is_active", "trade_date"),
    )


class SectorMainLine(Base):
    """主线板块追踪
    
    记录识别出的主线板块及其持续时间
    """
    __tablename__ = "sector_main_line"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30), nullable=False)
    sector_type = Column(String(20), nullable=False)
    
    # 主线周期
    start_date = Column(Date, nullable=False)   # 启动日期
    end_date = Column(Date)                      # 结束日期(NULL=进行中)
    duration_days = Column(Integer, default=0)   # 持续天数
    
    # 主线强度
    max_height = Column(Integer, default=0)      # 最高连板高度
    total_limit_up = Column(Integer, default=0)  # 总涨停家数
    avg_fund_flow = Column(Float, default=0)     # 日均资金流
    
    # 龙头股记录
    leader_stock = Column(String(10))            # 龙头股代码
    leader_name = Column(String(20))             # 龙头股名称
    leader_max_height = Column(Integer)          # 龙头最高连板
    
    # 状态
    status = Column(String(10), default="active")  # active/ended
    end_reason = Column(Text)                    # 结束原因
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (
        Index("ix_mainline_status", "status", "start_date"),
    )


class SectorKline(Base):
    """板块K线数据(OHLCV) — 趋势分析核心

    概念板块 vs 行业板块:
    - 概念: 短期热点, K线波动大, 适合短线趋势跟踪
    - 行业: 长期趋势, K线波动小, 适合中期趋势判断

    数据源:
    - 概念: AkShare stock_board_concept_index_ths
    - 行业: AkShare stock_board_industry_index_ths

    采集策略:
    - 概念: 盘后1次(近60天)
    - 行业: 盘后1次(近250天)
    - 初始化: 全量回填, 之后增量更新

    用途:
    - K线图展示(板块营地/个股详情)
    - 技术因子计算(均线/布林/MACD)
    - 趋势判断(支撑/压力/突破)
    - 生命周期辅助(高潮见顶/退潮破位)
    """
    __tablename__ = "sector_kline"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_code = Column(String(20), nullable=False, index=True)
    sector_name = Column(String(30))
    sector_type = Column(String(20), nullable=False)  # concept/industry
    trade_date = Column(Date, nullable=False, index=True)

    # OHLCV
    open = Column(Float)                           # 开盘指数
    high = Column(Float)                           # 最高指数
    low = Column(Float)                            # 最低指数
    close = Column(Float)                          # 收盘指数
    volume = Column(Float)                         # 成交量(手)
    amount = Column(Float)                         # 成交额(元)

    # 涨跌
    change_pct = Column(Float)                     # 涨跌幅%(AkShare不返回则自行计算)
    amplitude = Column(Float)                      # 振幅%(AkShare不返回则自行计算)

    # 均线指标(采集时计算)
    ma5 = Column(Float)                            # 5日均线
    ma10 = Column(Float)                           # 10日均线
    ma20 = Column(Float)                           # 20日均线
    ma5_vol = Column(Float)                        # 5日成交量均线

    # 趋势指标(采集时计算)
    trend_state = Column(String(20))               # 趋势状态: up/down/sideways/breakout_up/breakout_down
    vol_ratio = Column(Float)                      # 量比(当日量/MA5量), >1.5放量, <0.7缩量
    support_price = Column(Float)                  # 近期支撑位(近20日最低价)
    resistance_price = Column(Float)               # 近期压力位(近20日最高价)

    # 数据源标记
    source = Column(String(20), default="akshare") # akshare

    __table_args__ = (
        UniqueConstraint("sector_code", "trade_date", name="uq_sector_kline_code_date"),
        Index("ix_sector_kline_date_type", "trade_date", "sector_type"),
        Index("ix_sector_kline_code_date", "sector_code", "trade_date"),
    )
