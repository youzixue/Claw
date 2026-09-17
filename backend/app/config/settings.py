"""Claw 鹰爪量化交易系统 — 全局配置"""

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings
from pathlib import Path
from typing import Optional


_BACKEND_DIR = Path(__file__).resolve().parents[2]


class PaperConfirmationPolicy(BaseModel):
    """独立账户的策略确认，不是交易所或全局撮合约束。"""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    min_samples: int = Field(default=2, ge=1)
    min_persistence_sec: int = Field(default=60, ge=0)
    max_sample_gap_sec: int = Field(default=90, ge=1)
    clock_jitter_sec: float = Field(default=1.0, ge=0, le=1.0, allow_inf_nan=False)
    max_pullback_from_high_pct: float = Field(default=2.0, ge=0, le=100, allow_inf_nan=False)


class PaperRouteSignalPolicy(PaperConfirmationPolicy):
    min_samples: int = Field(default=3, ge=1)
    max_sample_gap_sec: int = Field(default=75, ge=1)
    min_volume_ratio: float = Field(default=0.8, ge=0, allow_inf_nan=False)
    min_orderbook_imbalance: float = Field(default=-0.10, ge=-1, le=1, allow_inf_nan=False)
    min_relative_strength_pct: float = Field(default=0.50, allow_inf_nan=False)
    min_vwap_slope_pct: float = Field(default=0.0, allow_inf_nan=False)


class PaperChallengerExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    max_execution_delay_sec: int = Field(default=720, ge=1)
    max_entry_drift_pct: float = Field(default=0.60, ge=0, allow_inf_nan=False)
    cash_buffer_pct: float = Field(default=0.02, ge=0, lt=1, allow_inf_nan=False)
    opening_risk_end: str = Field(default="09:35", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    opening_position_factor: float = Field(default=0.50, ge=0, le=1, allow_inf_nan=False)


class Settings(BaseSettings):
    """应用配置 — 从环境变量或 .env 文件读取"""

    # 延迟/排队买单从原确认起计时，不能由新报价延长；路线挑战者沿用自身720秒策略。
    PAPER_PENDING_BUY_MAX_AGE_SEC: int = Field(default=720, ge=1)

    # === 基础 ===
    APP_NAME: str = "Claw"
    APP_VERSION: str = "5.0.0"
    DEBUG: bool = True
    # macOS调度进程运行时请求AC防休眠，不改变全机设置，不能阻止合盖/手动休眠。
    SCHEDULER_PREVENT_IDLE_SLEEP: bool = True

    # === 数据库 ===
    DATABASE_URL: str = "sqlite+aiosqlite:///./claw.db"
    SQL_ECHO: bool = False
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 30
    DB_POOL_TIMEOUT: int = 60
    DB_POOL_RECYCLE: int = 1800
    # 生产: postgresql+asyncpg://user:pass@localhost:5432/claw

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_sqlite_database_url(cls, value):
        """将相对 SQLite 文件固定到 backend，避免启动目录改变数据库。"""
        raw = str(value or "").strip()
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if not raw.startswith(prefix):
                continue
            location = raw[len(prefix):]
            path_value, separator, query = location.partition("?")
            if path_value == ":memory:" or path_value.startswith("file:"):
                return raw
            database_path = Path(path_value).expanduser()
            if not database_path.is_absolute():
                database_path = (_BACKEND_DIR / database_path).resolve()
            normalized = f"{prefix}{database_path.as_posix()}"
            return f"{normalized}{separator}{query}" if separator else normalized
        return raw

    # === Redis ===
    REDIS_URL: str = "redis://localhost:6379/0"

    # === 数据源 ===
    # AkShare (同花顺)
    AKSHARE_RATE_LIMIT: float = 0.5  # 秒/次
    # 东财
    EASTMONEY_RATE_LIMIT: float = 0.3
    EASTMONEY_FUND_FLOW_ROUND_TIMEOUT_SEC: float = 20.0
    EASTMONEY_FUND_FLOW_PAGE_TIMEOUT_SEC: float = 5.0
    # 备用客户端仍须提供可验证主力字段及源时钟；并非独立供应商。
    FUND_FLOW_INDIVIDUAL_FALLBACK_MIN_COVERAGE: float = 0.95
    # 腾讯个股资金独立接口：参考实时行情6并发/30秒调度，单股请求滚动覆盖。
    TENCENT_FUND_FLOW_CONCURRENCY: int = Field(default=6, ge=1, le=6)
    TENCENT_FUND_FLOW_CODES_PER_ROUND: int = Field(default=600, ge=1, le=1000)
    TENCENT_FUND_FLOW_ROUND_TIMEOUT_SEC: float = Field(default=20.0, gt=0, le=25, allow_inf_nan=False)
    TENCENT_FUND_FLOW_REQUEST_INTERVAL_SEC: float = Field(default=0.08, ge=0.08, allow_inf_nan=False)
    # 源报价水位在本次观测时最多陈旧10分钟，接收时间不能刷新它。
    FUND_FLOW_SOURCE_MAX_AGE_SEC: float = 600.0
    # 竞价证据在原始观测时的源时钟容差；接收标签不能刷新旧源帧。
    AUCTION_SOURCE_MAX_AGE_SEC: float = 30.0
    # 个股资金已完成时，不为慢速概念元数据继续阻塞整轮入库。
    FUND_FLOW_CONCEPT_WAIT_TIMEOUT_SEC: float = 1.0
    # pywencai
    PYWENCAI_RATE_LIMIT: float = 2.0
    # 问财自 2026-08 下旬起要求**登录会话**才返回数据：未登录时接口返回
    # 401+captcha_url 或 403 Access Denied，库随即在 `params.get('data')`
    # 抛出 `'NoneType' object has no attribute 'get'`。
    # 问财要求登录会话才返回数据（未登录时 401，或数据层返回 code_count=0）。
    # 该 cookie 由 `WencaiStreamSource` 写进 SSE 请求头；
    # 凭据只放 `.env`（已 gitignore），不得写进仓库、日志或测试。
    PYWENCAI_COOKIE: str = ""
    # 申万
    SW_RATE_LIMIT: float = 0.2
    # 新浪
    SINA_RATE_LIMIT: float = 1.0

    # === 股票标记 ===
    IPO_RECENT_DAYS: int = 60  # 次新股: 上市<60交易日

    # === 风控 ===
    DEFAULT_STOP_LOSS_PCT: float = 7.0  # 默认止损%
    MAX_DRAWDOWN_PCT: float = 15.0      # 最大回撤%
    POSITION_LIMIT_PCT: float = 55.0    # 单股仓位上限%
    TOTAL_POSITION_LIMIT_PCT: float = 100.0  # 总仓位上限%

    # === AI 模块 (Anthropic 兼容 — MiniMax M2.7) ===
    AI_ENABLED: bool = False
    AI_BASE_URL: str = "https://api.minimaxi.com/anthropic"
    AI_API_KEY: str = ""
    AI_MODEL: str = "MiniMax-M2.7"
    AI_MAX_TOKENS: int = 2048
    AI_TEMPERATURE: float = 0.3
    AI_API_FORMAT: str = "anthropic"  # 保留现有 MiniMax 协议，另支持 openai
    AI_AUTH_MODE: str = "api_key"
    AI_CONFIG_PATH: Path = Path.home() / ".claw" / "ai-config.json"
    AI_CODEX_EXECUTABLE: str = "codex"
    AI_CODEX_HOME: Path = Path.home() / ".claw" / "codex-news"
    AI_CODEX_MODEL: str = ""  # 空值使用账号默认模型
    AI_CODEX_REASONING_EFFORT: str = ""  # 空值使用模型默认思考强度

    # === 推送 ===
    FEISHU_WEBHOOK_URL: str = ""
    PUSH_STOCK_COOLDOWN: int = 300       # 同股推送冷却(秒)
    PUSH_HOURLY_LIMIT: int = 30         # 每小时推送上限
    URGENT_SCORE_THRESHOLD: int = 90     # 紧急信号评分阈值
    PUSH_ENABLED: bool = True            # 推送总开关
    # 用户选择只接收模拟策略买点；其他飞书通知保留实现，关闭本开关可恢复。
    FEISHU_PAPER_BUY_POINTS_ONLY: bool = True
    PAPER_BUY_POINT_PUSH_ENABLED: bool = True  # 十二账户真实策略买点，独立于成交提醒
    PAPER_BUY_POINT_PUSH_INTERVAL_SEC: int = Field(default=5, ge=1, le=60)
    PAPER_BUY_POINT_PUSH_MAX_AGE_SEC: int = Field(default=180, ge=30, le=600)
    PAPER_BUY_POINT_PUSH_RETRY_SEC: int = Field(default=15, ge=5, le=120)
    ANOMALY_LOW_BASE_WATCHLIST: str = (
        "002768,603530,603283,002518"
    )  # 核心成长低位观察池，保留旧字段名兼容现有接口
    ANOMALY_CORE_GROWTH_MIN_NET_PROFIT_GROWTH: float = 20.0
    ANOMALY_CORE_GROWTH_MAX_NET_PROFIT_GROWTH: float = 200.0
    ANOMALY_CORE_GROWTH_MAX_PE_TTM: float = 80.0
    ANOMALY_CORE_GROWTH_MAX_CIRC_MARKET_CAP: float = 300.0
    ANOMALY_CORE_GROWTH_MAX_POSITION_120D: float = 72.0
    ANOMALY_PUSH_WATCHLIST_A2_ONLY: bool = True  # 普通异动仅A1自动推，重点池允许A2
    ANOMALY_PUSH_ALLOW_LIMIT_UP: bool = True  # 仅推通过盘口确认的A1涨停，过滤一字板/高位板/弱封板
    ANOMALY_PUSH_SCAN_INTERVAL_SEC: int = 30  # 行情提交会即时唤醒；30秒仅作漏事件兜底
    ANOMALY_PUSH_MIN_FULL_SCAN_GAP_SEC: int = 15  # 全市场重扫最小启动间隔，合并高频行情提交事件
    ANOMALY_PUSH_SCAN_TIMEOUT_SEC: int = 50
    ANOMALY_QUOTE_MAX_AGE_SEC: int = 90  # 交易时段超过90秒的行情禁止生成新异动
    ANOMALY_QUOTE_ROUND_TOLERANCE_SEC: int = 10  # 部分批次失败时仅使用最新一轮报价
    ANOMALY_QUOTE_INBOX_MAX_BATCHES: int = 6  # 扫描繁忙时保留最近行情批次，避免A/B被单行upsert覆盖
    ANOMALY_QUOTE_INBOX_MAX_AGE_SEC: int = 180
    ANOMALY_PUSH_MAX_PER_SCAN: int = 3   # 单次最多1条重点+2条条件/观察，降低消息噪音
    ANOMALY_PUSH_MAX_PER_HOUR: int = 12  # 自动异动滚动小时上限，数据库持久化防重启失效
    ANOMALY_PUSH_BURST_CAPACITY: int = 2  # 普通消息平滑令牌桶容量，重点买点可走保留额度
    ANOMALY_PUSH_A1_RESERVE_PER_HOUR: int = 4  # A2超限时仍为真正A1买点保留额度
    ANOMALY_PUSH_RAPID_STRONG_RESERVE_PER_HOUR: int = 3  # 强急拉只作观察，压缩独立小时额度
    ANOMALY_PUSH_CORE_MAX_PER_SCAN: int = 1  # 每轮只突出综合优先级最高的非追高买点
    ANOMALY_PUSH_CORE_MIN_WIN_RATE: float = 0.62  # 成熟样本晋级重点买点的3日净胜率门槛
    ANOMALY_PUSH_CORE_MAX_CHANGE_PCT: float = 3.0  # 重点买点不得处在明显追高区
    # 安全买入边际：基于日K边界(乖离MA/近N日累计涨幅/距近N日高点)拦截追高买点。
    ANOMALY_CHASE_FILTER_ENABLED: bool = True  # 追高过滤总开关；关闭即回退旧算法，用于灰度对比
    # 以下阈值按 543 条已结算异动信号回放校准(见 scripts/calibrate_chase_thresholds.py)：
    # 拦下组的 3 日净胜率显著低于放行组，阈值向更保守方向收敛。
    ANOMALY_CHASE_MAX_BIAS_MA20_PCT: float = 6.0   # 现价乖离MA20超此值视为短线透支(原8.0)
    ANOMALY_CHASE_MAX_BIAS_MA60_PCT: float = 12.0  # 现价乖离MA60超此值视为中高位(原18.0)
    ANOMALY_CHASE_MAX_RETURN_5D_PCT: float = 14.0  # 近5日累计涨幅超此值不宜追入
    ANOMALY_CHASE_MAX_RETURN_20D_PCT: float = 20.0 # 近20日累计涨幅超此值处于主升高位(原25.0)
    ANOMALY_CHASE_NEAR_HIGH_GAP_PCT: float = -1.0  # 距60日高点缺口小于此值(更贴近)即箱体上沿
    ANOMALY_PUSH_STOCK_COOLDOWN: int = 900  # 买点类消息同股至少间隔15分钟
    # 水下快速反转：在翻红后的可执行窗口内提前提醒，超过窗口仍由追高风控拦截。
    ANOMALY_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT: float = 1.2
    # 只用进程真实观察到的当前涨幅武装翻红状态；日内最低价仅保留展示用途。
    ANOMALY_UNDERWATER_REVERSAL_ARM_CHANGE_PCT: float = -0.25
    ANOMALY_UNDERWATER_REVERSAL_RESET_CHANGE_PCT: float = -0.25
    ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT: float = -2.8
    ANOMALY_UNDERWATER_ACCELERATION_MIN_REBOUND_PCT: float = 1.2
    ANOMALY_UNDERWATER_ACCELERATION_MIN_MOMENTUM_PCT: float = 0.65
    ANOMALY_UNDERWATER_ACCELERATION_MIN_VOLUME_RATIO: float = 1.05
    ANOMALY_UNDERWATER_ACCELERATION_MIN_VWAP_GAP_PCT: float = -0.8
    ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT: float = 0.15
    ANOMALY_UNDERWATER_REVERSAL_MAX_CHANGE_PCT: float = 5.8
    ANOMALY_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO: float = 0.8
    ANOMALY_UNDERWATER_REVERSAL_MAX_VOLUME_RATIO: float = 2.8
    ANOMALY_UNDERWATER_REVERSAL_MAX_AMPLITUDE: float = 10.5
    ANOMALY_UNDERWATER_REVERSAL_MAX_TURNOVER: float = 18.0
    ANOMALY_UNDERWATER_REVERSAL_MIN_CLOSE_POSITION: float = 0.60
    ANOMALY_UNDERWATER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT: float = 2.5
    # 红盘二次加速：覆盖约+2%放量直线拉到+4%的盘中路径，只发A2轻提醒。
    ANOMALY_POSITIVE_ACCELERATION_MIN_CHANGE_PCT: float = 1.5
    ANOMALY_POSITIVE_ACCELERATION_MAX_CHANGE_PCT: float = 6.5
    # 行情30秒快照可能直接跨过常规窗口；连续快照确认后补发追赶/封板强度提醒，但不作为涨停价买入指令。
    ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_CHANGE_PCT: float = 8.2
    ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MIN_MOMENTUM_PCT: float = 1.2
    ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_AMPLITUDE: float = 12.0
    ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_TURNOVER: float = 20.0
    # +8%左右跨窗后到封板前的放量区间；仅作B类强度观察，明确禁止追板执行。
    ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MIN_PRICE_RATIO: float = 0.983
    ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MAX_PRICE_RATIO: float = 0.995
    ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MIN_MOMENTUM_PCT: float = 0.6
    ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MAX_CHANGE_PCT: float = 10.2
    ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MIN_MOMENTUM_PCT: float = 1.5
    ANOMALY_POSITIVE_ACCELERATION_MIN_MOMENTUM_PCT: float = 0.8
    ANOMALY_POSITIVE_ACCELERATION_MIN_VOLUME_RATIO: float = 1.2
    ANOMALY_POSITIVE_ACCELERATION_MAX_VOLUME_RATIO: float = 3.2
    ANOMALY_POSITIVE_ACCELERATION_MIN_NEAR_HIGH_RATIO: float = 0.992
    # 真正的滚动60秒涨速：用同一窗口的起止价和累计成交额确认，覆盖两轮
    # 30秒快照各涨约0.15%~0.5%、但单轮阈值未触发的持续急拉路径。
    ANOMALY_ROLLING_60S_WINDOW_SEC: int = 60
    ANOMALY_ROLLING_60S_TOLERANCE_SEC: int = 20
    ANOMALY_ROLLING_60S_MIN_CHANGE_PCT: float = 0.30
    ANOMALY_ROLLING_60S_MEDIUM_CHANGE_PCT: float = 0.60
    ANOMALY_ROLLING_60S_STRONG_CHANGE_PCT: float = 1.00
    ANOMALY_ROLLING_60S_MIN_VOLUME_RATIO: float = 1.00
    ANOMALY_ROLLING_60S_MAX_VOLUME_RATIO: float = 5.00
    ANOMALY_ROLLING_60S_HISTORY_SEC: int = 360
    ANOMALY_ROLLING_60S_MIN_CLOSE_POSITION: float = 0.70
    ANOMALY_ROLLING_60S_MAX_PEAK_PULLBACK_PCT: float = 0.35
    ANOMALY_ROLLING_60S_MIN_UP_LEG_RATIO: float = 0.50
    # 急拉必须由最近一轮真实增量成交确认，不能用全天量比代替分时放量。
    ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO: float = 1.25
    ANOMALY_ACCELERATION_MIN_AMOUNT_DELTA: float = 1_000_000.0
    ANOMALY_ACCELERATION_MAX_WITHDRAWAL_RATIO: float = 0.15
    # 趋势股支撑到达：先提示到达，不把下跌中的最低价直接包装成确定买点。
    ANOMALY_TREND_SUPPORT_TOUCH_MIN_GAP_PCT: float = -1.2
    ANOMALY_TREND_SUPPORT_TOUCH_MAX_GAP_PCT: float = 1.5
    ANOMALY_TREND_SUPPORT_TOUCH_MAX_FROM_LOW_PCT: float = 0.65
    ANOMALY_TREND_SUPPORT_TOUCH_MIN_MIN5_CHANGE_PCT: float = -0.45
    # 主升浪绿开下杀回收：主升记忆只负责入池，止跌、增量成交与盘口/板块
    # 共振后才给A2，禁止把开盘继续下杀当作低吸。
    ANOMALY_MAIN_WAVE_GREEN_OPEN_LOWER_PCT: float = -6.5
    ANOMALY_MAIN_WAVE_GREEN_OPEN_UPPER_PCT: float = -1.5
    ANOMALY_MAIN_WAVE_GREEN_MIN_LOW_DROP_PCT: float = -2.5
    ANOMALY_MAIN_WAVE_GREEN_MIN_REBOUND_PCT: float = 1.2
    ANOMALY_MAIN_WAVE_GREEN_MIN_MOMENTUM_PCT: float = 0.55
    ANOMALY_MAIN_WAVE_GREEN_MIN_CLOSE_POSITION: float = 0.60
    ANOMALY_MAIN_WAVE_GREEN_MAX_PULLBACK_FROM_HIGH_PCT: float = 2.5
    ANOMALY_MAIN_WAVE_GREEN_MIN_VOLUME_RATIO: float = 0.70
    ANOMALY_MAIN_WAVE_GREEN_MAX_VOLUME_RATIO: float = 2.8
    ANOMALY_MAIN_WAVE_GREEN_MAX_AMPLITUDE: float = 10.5
    ANOMALY_MAIN_WAVE_GREEN_MAX_TURNOVER: float = 18.0
    # 主升浪首阴/缩量十字星：形态低点仅作为次日支撑锚点，盘中必须先触及
    # 再出现滚动60秒回拉和增量成交，防止用事后“全天最低点”产生前视信号。
    ANOMALY_MAIN_WAVE_PULLBACK_MAX_SUPPORT_BREAK_PCT: float = 2.5
    ANOMALY_MAIN_WAVE_PULLBACK_MIN_REBOUND_PCT: float = 0.65
    ANOMALY_MAIN_WAVE_PULLBACK_MIN_MOMENTUM_PCT: float = 0.30
    ANOMALY_MAIN_WAVE_PULLBACK_MIN_CLOSE_POSITION: float = 0.50
    ANOMALY_MAIN_WAVE_PULLBACK_MIN_VOLUME_RATIO: float = 0.65
    ANOMALY_MAIN_WAVE_PULLBACK_MAX_VOLUME_RATIO: float = 2.2
    ANOMALY_MAIN_WAVE_PULLBACK_MAX_AMPLITUDE: float = 8.0
    ANOMALY_MAIN_WAVE_PULLBACK_MAX_TURNOVER: float = 15.0
    # 严格主升缩量回踩A2：净占比不能替代真实金额，且须有可验证的大单净流入。
    ANOMALY_MAIN_WAVE_PULLBACK_MIN_MAIN_INFLOW_AMOUNT: float = 10_000_000
    ANOMALY_MAIN_WAVE_PULLBACK_MIN_MAIN_INFLOW_PCT: float = 2.5
    ANOMALY_MAIN_WAVE_PULLBACK_MIN_LARGE_ORDER_INFLOW_AMOUNT: float = 3_000_000
    ANOMALY_EVAL_ROUND_TRIP_COST_PCT: float = 0.36
    ANOMALY_EVAL_MAX_ADVERSE_PCT: float = -5.0
    ANOMALY_EVAL_MIN_SAMPLES: int = 30
    ANOMALY_EVAL_MIN_WIN_RATE: float = 0.55
    ANOMALY_EVAL_EARLY_STOP_MIN_SAMPLES: int = 15
    ANOMALY_EVAL_EARLY_STOP_MAX_WIN_RATE: float = 0.30

    # === 模拟盘 ===
    # 2026-09-07用户批准全账户持续实验，并在14:33明确要求当日下午开始。
    # 首日仅接受授权后的新鲜行情与仍有效信号，不按上午旧价格回填成交。
    # 实验关闭只用于显式维护；历史亏损/牛熊切换不能自动关闭实验账户。
    PAPER_CONTINUOUS_EXPERIMENT_ENABLED: bool = True
    PAPER_EXPERIMENT_START_DATE: str = "2026-09-07"
    PAPER_EXPERIMENT_ACTIVATION_AT: str = "2026-09-07T14:33:17"
    PAPER_EXPERIMENT_VERSION: str = "continuous_paper_v1_pm"
    PAPER_EXPERIMENT_REGIME_VERSION: str = "sse_szse_ma20_60_slope5_v1"
    PAPER_EXPERIMENT_SENTIMENT_MAX_AGE_SEC: int = 600
    PAPER_INITIAL_CAPITAL: float = 50_000
    PAPER_COMMISSION_RATE: float = 0.0003  # 万三手续费
    PAPER_MIN_COMMISSION: float = 5.0      # 单次模拟成交最低佣金（可由环境变量覆盖）
    PAPER_STAMP_TAX_RATE: float = 0.001    # 千一印花税(仅卖出，独立于佣金记账)
    PAPER_EXECUTION_QUOTE_MAX_AGE_SEC: int = 90  # 自动委托实时行情最大允许延迟
    PAPER_EXECUTION_SLIPPAGE_PCT: float = 0.10   # 基于五档盘口的不利滑点
    PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND: bool = True
    PAPER_DEPTH_MAX_PARTICIPATION_RATIO: float = 1.0
    PAPER_CONTROL_SAMPLE_ENABLED: bool = True
    # forced_probe 只写隔离研究账本，默认不生成任何订单且永不计入策略绩效。
    PAPER_FORCED_PROBE_ENABLED: bool = False

    # === 不可变行情轮次与分时归档 ===
    QUOTE_ROUND_MIN_COVERAGE: float = 0.95
    QUOTE_ROUND_MIN_SOURCE_TIME_COVERAGE: float = 0.95
    QUOTE_ROUND_MAX_SOURCE_SKEW_SEC: int = 120
    QUOTE_ROUND_ARCHIVE_ENABLED: bool = True
    QUOTE_ROUND_ARCHIVE_DIR: Path = _BACKEND_DIR.parent / "runtime" / "quote_rounds"
    QUOTE_ROUND_ARCHIVE_RETENTION_TRADING_DAYS: int = 60
    QUOTE_ROUND_MINUTE_RETENTION_TRADING_DAYS: int = 370
    QUOTE_ROUND_RESTORE_MINUTES: int = 8
    QUOTE_ROUND_CODE_VERSION: str = ""
    PAPER_AUTO_TRADE_ENABLED: bool = True
    PAPER_INTRADAY_AUTO_TRADE_ENABLED: bool = True
    PAPER_INTRADAY_AUTO_INTERVAL_SEC: int = 60
    # Champion盘中真实模拟成交也必须跨轮确认；manual/close只做测试或审计，不套此门槛。
    PAPER_INTRADAY_CONFIRM_MIN_SAMPLES: int = 2
    PAPER_INTRADAY_CONFIRM_MIN_PERSISTENCE_SEC: int = 60
    PAPER_INTRADAY_CONFIRM_MAX_SAMPLE_GAP_SEC: int = 90
    # 行情轮询存在亚秒级调度抖动；59.9秒应按一分钟确认，不能因浮点时钟误差漏单。
    PAPER_CONFIRMATION_CLOCK_JITTER_SEC: float = 1.0
    PAPER_INTRADAY_CONFIRM_MAX_PULLBACK_FROM_HIGH_PCT: float = 2.0
    # 旧PAPER_INTRADAY_CONFIRM_*仅归A；其他执行账户拥有各自的同值基线。
    # 可用JSON环境变量单独覆盖某账户，未提供的账户使用自身默认值，不继承A。
    PAPER_ACCOUNT_CONFIRMATION_POLICIES: dict[str, PaperConfirmationPolicy] = Field(default_factory=dict)
    PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES: dict[str, PaperRouteSignalPolicy] = Field(default_factory=dict)
    PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES: dict[str, PaperChallengerExecutionPolicy] = Field(default_factory=dict)

    @field_validator("PAPER_ACCOUNT_CONFIRMATION_POLICIES", "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES",
                     "PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES")
    @classmethod
    def validate_paper_policy_accounts(cls, value, info):
        allowed = {
            "PAPER_ACCOUNT_CONFIRMATION_POLICIES": {
                "default", "promotion", "mainline", "auction", "tenbagger", "reversal", "challenger_e",
            },
            "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES": {
                "challenger_b", "challenger_c", "challenger_d", "challenger_f2",
            },
            "PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES": {
                "challenger_a", "challenger_b", "challenger_c", "challenger_d", "challenger_f2",
            },
        }[info.field_name]
        if set(value) - allowed:
            raise ValueError(f"{info.field_name}未知或不适用账户: {sorted(set(value) - allowed)}")
        return value
    # 当日曾以“自动买入暂停”运行的策略，即使盘中重启后开关变为True，也只演练到下一交易日。
    PAPER_AUTO_ORDER_ENABLE_NEXT_SESSION_ONLY: bool = True
    PAPER_INTRADAY_BUY_START: str = "09:35"
    PAPER_INTRADAY_BUY_END: str = "14:50"

    # === 3%~6%强势股首次回踩确认（产生影子证据；独立A2账户仍须执行层复核） ===
    # 版本阈值是前向实验协议的一部分；不得依据单日结果盘中改参。
    PAPER_MOMENTUM_RETEST_SHADOW_ENABLED: bool = True
    PAPER_MOMENTUM_RETEST_SHADOW_VERSION: str = "momentum_retest_v3"
    PAPER_MOMENTUM_RETEST_START: str = "09:35"
    PAPER_MOMENTUM_RETEST_CANDIDATE_END: str = "14:30"
    PAPER_MOMENTUM_RETEST_CONFIRM_END: str = "14:50"
    PAPER_MOMENTUM_RETEST_REARM_MAX_CHANGE_PCT: float = 2.5
    PAPER_MOMENTUM_RETEST_CANDIDATE_MIN_CHANGE_PCT: float = 3.0
    PAPER_MOMENTUM_RETEST_CANDIDATE_MAX_CHANGE_PCT: float = 6.0
    PAPER_MOMENTUM_RETEST_MIN_VOLUME_RATIO: float = 0.8
    PAPER_MOMENTUM_RETEST_MAX_VOLUME_RATIO: float = 5.0
    PAPER_MOMENTUM_RETEST_MIN_AMOUNT: float = 20_000_000
    PAPER_MOMENTUM_RETEST_MIN_PULLBACK_PCT: float = 0.5
    PAPER_MOMENTUM_RETEST_MAX_PULLBACK_PCT: float = 1.8
    PAPER_MOMENTUM_RETEST_MIN_HOLD_CHANGE_PCT: float = 2.5
    PAPER_MOMENTUM_RETEST_MIN_RECOVERY_PCT: float = 0.3
    PAPER_MOMENTUM_RETEST_MAX_PEAK_GAP_PCT: float = 0.8
    PAPER_MOMENTUM_RETEST_MIN_60S_CHANGE_PCT: float = 0.15
    PAPER_MOMENTUM_RETEST_MIN_AMOUNT_PACE_RATIO: float = 0.8
    PAPER_MOMENTUM_RETEST_MIN_ORDERBOOK_IMBALANCE: float = -0.2
    PAPER_MOMENTUM_RETEST_MAX_WITHDRAWAL_RATIO: float = 0.5
    PAPER_MOMENTUM_RETEST_MAX_VWAP_BREAK_PCT: float = 0.2
    # 2026-09-17：90 -> 180。实测各交易日 `quote_round` 相邻轮次间隔中位 30s，
    # 但盘中确有 91~111s 的真实抖动（09-09 有 17 次 >90s、09-15 有 29 次
    # 91~111s 的 quote_gap 阻断）。90s 只容 3 轮，刚好卡在这些正常抖动之外；
    # 180s 容 6 轮，覆盖实测最大值且留余量。用户授权「放宽风控避免踏空」。
    # 注意交易时段边界（09:24→09:30 的 361s、午休 11:29→13:00）不属此类：
    # 午休已在 `trading_elapsed_seconds` 里冻结，开盘边界由引擎的窗口判定处理。
    PAPER_MOMENTUM_RETEST_MAX_QUOTE_GAP_SEC: int = 180
    # 允许 `coverage_blocked` 在「连续性恢复 + 重新观察到武装低点」时解除，
    # 不再当日永久出局。置 False 回到原终态语义。
    PAPER_MOMENTUM_RETEST_ALLOW_COVERAGE_BLOCK_REARM: bool = True
    # 消费者水位落库的**最小间隔**（秒）。逐轮 commit 会给 SQLite 写锁紧张的
    # 热路径凭空加约 500 次写/天（实测 `database is locked` 9/15=306、
    # 9/16=304、9/17=501 次，并已造成 196 条「隔离Challenger模拟账户执行失败」）。
    # 默认 120s：水位最多陈旧 120s，叠加停机+重启约 20s，首帧 gap ≤140s，
    # 仍低于 180s 阈值 ⇒ 不会把幻影缺口误判成真缺口。置 0 = 每轮都落（旧行为）。
    PAPER_MOMENTUM_RETEST_WATERMARK_MIN_INTERVAL_SEC: float = 120.0
    # 仅缓存前向采集的A2证据帧；超过上限必须显式阻断路径，不能静默合并。
    PAPER_MOMENTUM_RETEST_QUOTE_INBOX_MAX_BATCHES: int = 6
    PAPER_MOMENTUM_RETEST_MIN_TRACK_SEC: int = 60
    PAPER_MOMENTUM_RETEST_MAX_TRACK_SEC: int = 1200
    PAPER_MOMENTUM_RETEST_MAX_CONFIRM_WAIT_SEC: int = 480
    PAPER_MOMENTUM_RETEST_EVAL_MIN_SESSIONS: int = 20
    PAPER_MOMENTUM_RETEST_EVAL_MIN_SAMPLES: int = 100
    PAPER_MOMENTUM_RETEST_EVAL_MIN_AVG_MAE_PCT: float = -3.0
    PAPER_MOMENTUM_RETEST_EVAL_MIN_WORST_MAE_PCT: float = -8.0
    PAPER_MOMENTUM_RETEST_EVAL_MAX_SESSION_SHARE: float = 0.15
    PAPER_MOMENTUM_RETEST_EVAL_MAX_CODE_SHARE: float = 0.10
    # ABCD/F形态扩展先进入不可变影子台账；确认事件可以同步到完全隔离的
    # Challenger paper 子账户，但绝不写入 A-F Champion 账户，也不接真实券商。
    # 至少积累20个独立交易日、100个确认样本后才允许提交人工晋级评审。
    PAPER_STRATEGY_ITERATION_SHADOW_ENABLED: bool = True
    PAPER_STRATEGY_ITERATION_SHADOW_VERSION: str = "abcdef_shape_v4_fillable_relative_strength"
    # v4把此前只写在配置/版本名、却未进入判定的横截面相对强度和VWAP斜率
    # 真正接入确认闸门，并要求有效卖一量；旧版事件继续归档但不混入新证据。
    PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE: float = 0.95
    # confirmed 不再由单帧报价触发：腾讯行情30秒一轮，至少连续3帧、跨度60秒，
    # 且相邻帧不能断档，避免09:30的瞬时冲高被当成可成交弱转强。
    PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES: int = 3
    PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC: int = 60
    PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC: int = 75
    PAPER_STRATEGY_ITERATION_MAX_PULLBACK_FROM_HIGH_PCT: float = 2.0
    PAPER_CHALLENGER_ACCOUNT_ENABLED: bool = True
    # A2消费已有首次回踩确认事件；E2独立验证高标强势/回封入口，不保证正收益。
    PAPER_CHALLENGER_A_VERSION: str = "a2_momentum_retest_v1"
    PAPER_CHALLENGER_E_VERSION: str = "e2_highboard_reseal_v1"
    PAPER_CHALLENGER_A_AUTO_ORDER_ENABLED: bool = True
    PAPER_CHALLENGER_E_AUTO_ORDER_ENABLED: bool = True
    PAPER_CHALLENGER_A_POSITION_PCT: float = 0.10
    PAPER_CHALLENGER_A_MAX_DAILY_BUYS: int = 3
    PAPER_CHALLENGER_A_MAX_POSITIONS: int = 5
    PAPER_CHALLENGER_A_TAKE_PROFIT_PCT: float = 8.0
    PAPER_CHALLENGER_A_STOP_LOSS_PCT: float = 5.0
    PAPER_CHALLENGER_A_MAX_HOLD_DAYS: int = 5
    PAPER_CHALLENGER_A_ENTRY_NOT_BEFORE: str = "09:35"
    # 次账户参数显式冻结；默认值等于当前已执行值，但不再运行时继承主账户。
    # 之后只改某个账户时，其余账户版本和有效配置必须保持不变。
    PAPER_CHALLENGER_B_AUTO_ORDER_ENABLED: bool = True
    PAPER_CHALLENGER_B_POSITION_PCT: float = 0.20
    PAPER_CHALLENGER_B_MAX_DAILY_BUYS: int = 2
    PAPER_CHALLENGER_B_MAX_POSITIONS: int = 3
    PAPER_CHALLENGER_B_TAKE_PROFIT_PCT: float = 12.0
    PAPER_CHALLENGER_B_STOP_LOSS_PCT: float = 5.0
    PAPER_CHALLENGER_B_MAX_HOLD_DAYS: int = 3
    PAPER_CHALLENGER_C_AUTO_ORDER_ENABLED: bool = True
    PAPER_CHALLENGER_C_POSITION_PCT: float = 0.20
    PAPER_CHALLENGER_C_MAX_DAILY_BUYS: int = 2
    PAPER_CHALLENGER_C_MAX_POSITIONS: int = 3
    PAPER_CHALLENGER_C_TAKE_PROFIT_PCT: float = 8.0
    PAPER_CHALLENGER_C_STOP_LOSS_PCT: float = 2.5
    PAPER_CHALLENGER_C_MAX_HOLD_DAYS: int = 3
    PAPER_CHALLENGER_D_AUTO_ORDER_ENABLED: bool = True
    PAPER_CHALLENGER_D_POSITION_PCT: float = 0.15
    PAPER_CHALLENGER_D_MAX_DAILY_BUYS: int = 1
    PAPER_CHALLENGER_D_MAX_POSITIONS: int = 2
    PAPER_CHALLENGER_D_TAKE_PROFIT_PCT: float = 5.0
    PAPER_CHALLENGER_D_STOP_LOSS_PCT: float = 4.0
    PAPER_CHALLENGER_D_MAX_HOLD_DAYS: int = 2
    # E2门槛和退出默认与当前E一致，但使用独立配置身份。
    PAPER_CHALLENGER_E_POSITION_PCT: float = 0.15
    PAPER_CHALLENGER_E_MAX_DAILY_BUYS: int = 1
    PAPER_CHALLENGER_E_MAX_POSITIONS: int = 3
    PAPER_CHALLENGER_E_TAKE_PROFIT_PCT: float = 18.0
    PAPER_CHALLENGER_E_STOP_LOSS_PCT: float = 6.0
    PAPER_CHALLENGER_E_MAX_HOLD_DAYS: int = 3
    PAPER_CHALLENGER_E_MIN_CONSECUTIVE: int = 4
    PAPER_CHALLENGER_E_MAX_CONSECUTIVE: int = 8
    PAPER_CHALLENGER_E_MIN_SEAL_AMOUNT: float = 1.0
    PAPER_CHALLENGER_E_MAX_BREAK_COUNT: int = 2
    PAPER_CHALLENGER_E_REQUIRE_ABOVE_VWAP: bool = True
    PAPER_CHALLENGER_E_MAX_PULLBACK_FROM_HIGH_PCT: float = 2.0
    PAPER_CHALLENGER_E_LIMIT_UP_QUEUE_ENABLED: bool = True
    PAPER_CHALLENGER_E_INTRADAY_BUY_START: str = "09:30"
    PAPER_CHALLENGER_E_QUEUE_CANCEL_TIME: str = "14:50"
    PAPER_CHALLENGER_E_MAX_ENTRY_CHANGE_PCT: float = 10.5
    PAPER_CHALLENGER_F2_AUTO_ORDER_ENABLED: bool = True
    PAPER_CHALLENGER_F2_POSITION_PCT: float = 0.10
    PAPER_CHALLENGER_F2_MAX_DAILY_BUYS: int = 1
    PAPER_CHALLENGER_F2_MAX_POSITIONS: int = 2
    PAPER_CHALLENGER_F2_TAKE_PROFIT_PCT: float = 12.0
    PAPER_CHALLENGER_F2_STOP_LOSS_PCT: float = 8.0
    PAPER_CHALLENGER_F2_MAX_HOLD_DAYS: int = 3
    # C2需要先收集至09:35再统一排序；12分钟事件有效期覆盖最早09:26的
    # 三帧确认到批处理窗口，但成交前仍强制使用最新spot复核，不按旧确认价回填。
    PAPER_CHALLENGER_MAX_EXECUTION_DELAY_SEC: int = 720
    PAPER_CHALLENGER_MAX_ENTRY_DRIFT_PCT: float = 0.60
    PAPER_CHALLENGER_CASH_BUFFER_PCT: float = 0.02
    PAPER_CHALLENGER_B_ENTRY_NOT_BEFORE: str = "09:32"
    PAPER_CHALLENGER_C_ENTRY_NOT_BEFORE: str = "09:35"
    PAPER_CHALLENGER_D_ENTRY_NOT_BEFORE: str = "09:32"
    PAPER_CHALLENGER_F2_ENTRY_NOT_BEFORE: str = "09:32"
    PAPER_CHALLENGER_OPENING_RISK_END: str = "09:35"
    PAPER_CHALLENGER_OPENING_POSITION_FACTOR: float = 0.50
    PAPER_STRATEGY_ITERATION_EVAL_MIN_SESSIONS: int = 20
    PAPER_STRATEGY_ITERATION_EVAL_MIN_SAMPLES: int = 100
    PAPER_STRATEGY_ITERATION_EVAL_MIN_AVG_MAE_PCT: float = -3.0
    PAPER_STRATEGY_ITERATION_EVAL_MIN_WORST_MAE_PCT: float = -8.0
    PAPER_STRATEGY_ITERATION_EVAL_MAX_SESSION_SHARE: float = 0.15
    PAPER_STRATEGY_ITERATION_EVAL_MAX_CODE_SHARE: float = 0.10
    PAPER_STRATEGY_ITERATION_EVAL_MAX_ACCOUNT_DRAWDOWN_PCT: float = 15.0
    PAPER_STRATEGY_B_WEAK_OPEN_CONFIRM_END: str = "10:30"
    PAPER_STRATEGY_B_WEAK_OPEN_MIN_PCT: float = -9.9
    PAPER_STRATEGY_B_WEAK_OPEN_MAX_PCT: float = 0.0
    PAPER_STRATEGY_B_RECLAIM_MIN_PCT: float = 0.0
    PAPER_STRATEGY_B_RECLAIM_MAX_PCT: float = 4.0
    PAPER_STRATEGY_C_RELAUNCH_LOOKBACK_SESSIONS: int = 5
    PAPER_STRATEGY_C_RELAUNCH_MIN_GAP_SESSIONS: int = 2
    PAPER_STRATEGY_C_RELAUNCH_MAX_GAP_SESSIONS: int = 5
    PAPER_STRATEGY_C_RELAUNCH_MAX_OPEN_PCT: float = 1.5
    PAPER_STRATEGY_C_RELAUNCH_MAX_LOW_PCT: float = 0.5
    PAPER_STRATEGY_C_RELAUNCH_MIN_RECLAIM_PCT: float = 0.0
    PAPER_STRATEGY_C_RELAUNCH_MAX_RECLAIM_PCT: float = 3.0
    PAPER_STRATEGY_D_AUCTION_CONFIRM_END: str = "10:00"
    PAPER_STRATEGY_D_AUCTION_BASELINE_MAX_PCT: float = -1.0
    PAPER_STRATEGY_D_AUCTION_FINAL_MIN_PCT: float = -2.0
    PAPER_STRATEGY_D_AUCTION_MIN_RECOVERY_PPT: float = 3.0
    # D路由必须有09:20前、09:20-09:25不可撤单阶段和09:25最终价的正量证据；
    # 仅凭正式开盘价或零量占位快照不得确认。
    PAPER_STRATEGY_D_AUCTION_MIN_CANCEL_PHASE_SAMPLES: int = 2
    PAPER_STRATEGY_D_AUCTION_RECLAIM_MIN_PCT: float = 0.0
    PAPER_STRATEGY_D_AUCTION_RECLAIM_MAX_PCT: float = 4.0
    PAPER_STRATEGY_F2_MIN_HIGHBOARD: int = 3
    PAPER_STRATEGY_F2_MIN_BREAK_SESSIONS: int = 1
    PAPER_STRATEGY_F2_MAX_BREAK_SESSIONS: int = 3
    PAPER_STRATEGY_F2_MAX_OPEN_PCT: float = 1.0
    PAPER_STRATEGY_F2_MAX_LOW_PCT: float = 0.5
    PAPER_STRATEGY_F2_MIN_RECLAIM_PCT: float = 0.0
    PAPER_STRATEGY_F2_MAX_RECLAIM_PCT: float = 3.0
    PAPER_STRATEGY_ITERATION_MIN_VOLUME_RATIO: float = 0.8
    PAPER_STRATEGY_ITERATION_MIN_ORDERBOOK_IMBALANCE: float = -0.10
    PAPER_STRATEGY_ITERATION_MIN_RELATIVE_STRENGTH_PCT: float = 0.50
    PAPER_STRATEGY_ITERATION_MIN_VWAP_SLOPE_PCT: float = 0.0

    # === 策略C3：主线首板盘中确认（只采证，不创建撮合账户） ===
    # 2026-09-03 上午复盘发现首板占涨停池绝大多数，但赢家样本不能反推阈值。
    # C3 从下一个完整交易日开始，先冻结“活跃主线板块内全部新鲜首板候选”分母，
    # 再记录连续确认；未确认候选作为对照，任何结果都不会自动修改 C Champion。
    PAPER_FIRST_BOARD_SHADOW_ENABLED: bool = True
    PAPER_FIRST_BOARD_SHADOW_VERSION: str = "c3_mainline_first_board_v1"
    PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE: str = "2026-09-04"
    PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE: float = 0.95
    PAPER_FIRST_BOARD_SHADOW_RECENT_LIMIT_LOOKBACK_SESSIONS: int = 5
    PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_STRENGTH: float = 25.0
    PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_CHANGE_PCT: float = 0.0
    PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_LIMIT_UP_COUNT: int = 1
    PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_STRENGTH: float = 50.0
    PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_CHANGE_PCT: float = 0.5
    PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_LIMIT_UP_COUNT: int = 2
    PAPER_FIRST_BOARD_SHADOW_MIN_CHANGE_PCT: float = 1.0
    PAPER_FIRST_BOARD_SHADOW_MAX_CHANGE_PCT: float = 7.0
    PAPER_FIRST_BOARD_SHADOW_MIN_VOLUME_RATIO: float = 1.0
    PAPER_FIRST_BOARD_SHADOW_MIN_RELATIVE_STRENGTH_PCT: float = 0.5
    PAPER_FIRST_BOARD_SHADOW_MAX_PULLBACK_FROM_HIGH_PCT: float = 1.5
    PAPER_FIRST_BOARD_SHADOW_CONFIRM_END: str = "14:30"
    PAPER_FIRST_BOARD_SHADOW_MIN_SESSION_FRAMES: int = 300
    PAPER_FIRST_BOARD_SHADOW_MIN_MORNING_FRAMES: int = 180
    PAPER_FIRST_BOARD_SHADOW_MIN_AFTERNOON_FRAMES: int = 120
    PAPER_FIRST_BOARD_SHADOW_FIRST_FRAME_DEADLINE: str = "09:35"
    PAPER_FIRST_BOARD_SHADOW_MORNING_LAST_NOT_BEFORE: str = "11:25"
    PAPER_FIRST_BOARD_SHADOW_AFTERNOON_FIRST_DEADLINE: str = "13:05"
    PAPER_FIRST_BOARD_SHADOW_LAST_FRAME_NOT_BEFORE: str = "14:30"
    PAPER_FIRST_BOARD_SHADOW_MAX_FRAME_GAP_SEC: int = 180
    PAPER_FIRST_BOARD_SHADOW_OUTCOME_MIN_KLINE_COVERAGE: float = 1.0

    PAPER_AUTO_MAX_POSITIONS: int = 2
    PAPER_AUTO_POSITION_PCT: float = 0.50
    PAPER_AUTO_MIN_SCORE: float = 72.0
    # 自动模拟盘只执行风控完全通过的信号；warn保留给人工判断，不能自动下单。
    PAPER_AUTO_WARN_RISK_BLOCK_BUY: bool = True
    PAPER_AUTO_DRAWDOWN_HALF_BUY_PCT: float = 1.0
    PAPER_AUTO_DRAWDOWN_PAUSE_BUY_PCT: float = 2.0
    PAPER_AUTO_DRAWDOWN_RECOVERY_ENABLED: bool = True
    PAPER_AUTO_DRAWDOWN_RECOVERY_HARD_PAUSE_PCT: float = 3.5
    PAPER_AUTO_DRAWDOWN_RECOVERY_MIN_SCORE: float = 88.0
    PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_ENABLED: bool = True
    PAPER_AUTO_DRAWDOWN_PERMANENT_LOCK_ENABLED: bool = True
    PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MAX_PCT: float = 5.0
    PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MIN_SCORE: float = 92.0
    PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MAX_AMOUNT: int = 5000
    PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_MIN_SCORE: float = 97.0
    PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_POSITION_PCT: float = 0.50
    PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_MAX_AMOUNT: int = 5000
    # 回撤期不能让算法完全失去盘面样本；仅对已通过性价比闸门的候选分层建仓。
    PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_ENABLED: bool = True
    PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MIN_SCORE: float = 82.0
    PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MAX_BUYS: int = 1
    PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MAX_AMOUNT: int = 1000
    PAPER_AUTO_STAGED_ENTRY_TRIAL_PCT: float = 0.08
    PAPER_AUTO_STAGED_ENTRY_NORMAL_PCT: float = 0.10
    PAPER_AUTO_STAGED_ENTRY_STRONG_PCT: float = 0.14
    PAPER_AUTO_STAGED_ENTRY_DEEP_DRAWDOWN_FACTOR: float = 0.65
    # 单股允许多次确认后逐层加仓；首层仍按8%/10%/14%，总仓不超过28%。
    PAPER_AUTO_STAGED_ENTRY_MAX_POSITION_PCT: float = 0.28
    PAPER_AUTO_STAGED_ENTRY_MAX_SHARES_PER_ORDER: int = 1000
    PAPER_AUTO_SCALE_IN_ENABLED: bool = True
    PAPER_AUTO_SCALE_IN_MIN_SCORE: float = 84.0
    PAPER_AUTO_SCALE_IN_MIN_COST_RETURN_PCT: float = -1.5
    PAPER_AUTO_SCALE_IN_MAX_COST_RETURN_PCT: float = 1.2
    PAPER_AUTO_STRONG_MARKET_RECOVERY_ENABLED: bool = True
    PAPER_AUTO_STRONG_MARKET_RECOVERY_MIN_LIMIT_UP_COUNT: int = 100
    PAPER_AUTO_STRONG_MARKET_RECOVERY_MIN_ADVANCE_DECLINE_RATIO: float = 3.0
    PAPER_AUTO_STRONG_MARKET_RECOVERY_MIN_MAIN_NET_INFLOW: float = 0.0
    PAPER_AUTO_STRONG_MARKET_RECOVERY_MAX_BUYS: int = 2
    PAPER_AUTO_STRONG_MARKET_RECOVERY_MAX_OPEN_POSITIONS: int = 2
    PAPER_AUTO_AFTERNOON_NEW_BUY_STRONG_MARKET_ONLY: bool = True
    PAPER_AUTO_AFTERNOON_NEW_BUY_START: str = "13:00"
    PAPER_AUTO_AFTERNOON_LEADER_EXCEPTION_ENABLED: bool = True
    PAPER_AUTO_AFTERNOON_LEADER_MIN_SCORE: float = 92.0
    PAPER_AUTO_AFTERNOON_LEADER_MIN_LIMIT_UP_COUNT: int = 30
    PAPER_AUTO_AFTERNOON_LEADER_MAX_LIMIT_DOWN_COUNT: int = 40
    PAPER_AUTO_AFTERNOON_LEADER_MIN_SEAL_RATE: float = 45.0
    PAPER_AUTO_AFTERNOON_LEADER_MIN_ADVANCE_DECLINE_RATIO: float = 0.85
    PAPER_AUTO_LATE_LEADER_EXCEPTION_ENABLED: bool = True
    PAPER_AUTO_LATE_LEADER_BUY_END: str = "14:40"
    # 每日参与不是追当天最强，而是从09:50起滚动观察低热度趋势回收，按评分分层建仓。
    PAPER_AUTO_DAILY_PARTICIPATION_ENABLED: bool = True
    PAPER_AUTO_DAILY_PARTICIPATION_START: str = "09:50"
    # 每日参与只能从A档强趋势中选性价比买点，不能把B档“观望”样本靠盘中加分抬成买单。
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_SCORE: float = 78.0
    PAPER_AUTO_DAILY_PARTICIPATION_ALLOWED_LEVELS: str = "S,A"
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_SHORT_TREND_SCORE: float = 90.0
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_CHANGE_PCT: float = -1.5
    PAPER_AUTO_DAILY_PARTICIPATION_MAX_CHANGE_PCT: float = 1.8
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_VOLUME_RATIO: float = 0.75
    PAPER_AUTO_DAILY_PARTICIPATION_MAX_VOLUME_RATIO: float = 2.2
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_AVG_PREMIUM_PCT: float = -0.4
    PAPER_AUTO_DAILY_PARTICIPATION_MAX_AVG_PREMIUM_PCT: float = 0.35
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_REBOUND_PCT: float = 0.5
    PAPER_AUTO_DAILY_PARTICIPATION_MAX_REBOUND_PCT: float = 2.0
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_CLOSE_POSITION: float = 0.45
    PAPER_AUTO_DAILY_PARTICIPATION_MAX_CLOSE_POSITION: float = 0.72
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_SUPPORT_STRENGTH: float = 60.0
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_ORDERBOOK: float = 0.10
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_MAIN_INFLOW_PCT: float = 2.5
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_LIQUIDITY_CONFIRMATIONS: int = 2
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_CONFIRMATIONS: int = 4
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_LIMIT_UP_COUNT: int = 0
    PAPER_AUTO_DAILY_PARTICIPATION_MAX_LIMIT_DOWN_COUNT: int = 50
    PAPER_AUTO_DAILY_PARTICIPATION_MIN_ADVANCE_DECLINE_RATIO: float = 0.20
    PAPER_AUTO_DAILY_PARTICIPATION_MAX_BUY_AMOUNT: int = 1000
    PAPER_AUTO_ICEPOINT_REVERSAL_ENABLED: bool = True
    PAPER_AUTO_ICEPOINT_REVERSAL_START: str = "10:15"
    PAPER_AUTO_ICEPOINT_REVERSAL_MIN_SCORE: float = 95.2
    PAPER_AUTO_ICEPOINT_REVERSAL_MIN_CHANGE_PCT: float = -0.3
    PAPER_AUTO_ICEPOINT_REVERSAL_MAX_CHANGE_PCT: float = 1.8
    PAPER_AUTO_ICEPOINT_REVERSAL_MIN_LOW_DROP_PCT: float = 0.8
    PAPER_AUTO_ICEPOINT_REVERSAL_MIN_REBOUND_PCT: float = 1.2
    PAPER_AUTO_ICEPOINT_REVERSAL_MIN_VOLUME_RATIO: float = 0.8
    PAPER_AUTO_ICEPOINT_REVERSAL_MAX_VOLUME_RATIO: float = 2.2
    PAPER_AUTO_ICEPOINT_REVERSAL_MAX_AVG_PREMIUM_PCT: float = 0.8
    PAPER_AUTO_ICEPOINT_REVERSAL_MIN_CLOSE_POSITION: float = 0.55
    PAPER_AUTO_ICEPOINT_REVERSAL_MAX_CLOSE_POSITION: float = 0.90
    PAPER_AUTO_ICEPOINT_REVERSAL_MAX_BUY_AMOUNT: int = 100
    PAPER_AUTO_ICEPOINT_LIMIT_UP_COUNT: int = 30
    PAPER_AUTO_ICEPOINT_LIMIT_DOWN_COUNT: int = 50
    PAPER_AUTO_ICEPOINT_MAX_ADVANCE_DECLINE_RATIO: float = 1.0
    PAPER_AUTO_ICEPOINT_MELTDOWN_LIMIT_DOWN_COUNT: int = 120
    PAPER_AUTO_DRAWDOWN_RECOVERY_POSITION_PCT: float = 0.50
    PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_BUYS: int = 2
    PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_AMOUNT: int = 5000
    PAPER_AUTO_DRAWDOWN_RECOVERY_ALLOW_PROFIT_POSITION: bool = True
    PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_OPEN_POSITIONS: int = 2
    PAPER_AUTO_DRAWDOWN_RECOVERY_MIN_HELD_PROFIT_PCT: float = 2.0
    PAPER_AUTO_DRAWDOWN_RECOVERY_BUY_END: str = "14:00"
    PAPER_AUTO_ANOMALY_MIN_SECTOR_STRENGTH: float = 75.0
    PAPER_AUTO_ANOMALY_MIN_SECTOR_CHANGE_PCT: float = 1.0
    PAPER_AUTO_ANOMALY_STRONG_SECTOR_OVERRIDE: float = 82.0
    PAPER_AUTO_ANOMALY_EDGE_SCORE_CAP: float = 84.0
    PAPER_AUTO_ANOMALY_WEAK_SECTOR_MAX_AVG_PREMIUM_PCT: float = 1.2
    PAPER_AUTO_ANOMALY_MAX_3D_CHANGE_PCT: float = 18.0
    PAPER_AUTO_ANOMALY_MAX_INTRADAY_CHASE_PCT: float = 6.0
    # 所有自动买入来源共用的成交前性价比闸门。异动可观察，但高位加速不执行。
    PAPER_AUTO_VALUE_ENTRY_MAX_CHANGE_PCT: float = 3.0
    PAPER_AUTO_VALUE_ENTRY_MAX_HIGH_GAP_PCT: float = 0.8
    PAPER_AUTO_VALUE_ENTRY_HIGH_GAP_MIN_CHANGE_PCT: float = 1.5
    PAPER_AUTO_VALUE_ENTRY_MAX_VWAP_PREMIUM_PCT: float = 1.2
    PAPER_AUTO_VALUE_ENTRY_MAX_RANGE_POSITION: float = 0.82
    PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT: float = 3.0
    PAPER_AUTO_VALUE_ENTRY_MAX_SUPPORT_GAP_PCT: float = 4.0
    PAPER_AUTO_VALUE_ENTRY_MIN_REWARD_RISK: float = 1.5
    # 水下/低开急拉只先观察；首次停顿回踩后仍守住VWAP与承接，才可进入自动买单。
    PAPER_AUTO_REVERSAL_RETEST_MAX_CHANGE_PCT: float = 1.8
    PAPER_AUTO_REVERSAL_RETEST_MAX_RANGE_POSITION: float = 0.72
    PAPER_AUTO_REVERSAL_RETEST_MAX_VWAP_PREMIUM_PCT: float = 0.50
    PAPER_AUTO_REVERSAL_RETEST_MIN_5M_CHANGE_PCT: float = -0.30
    PAPER_AUTO_REVERSAL_RETEST_MAX_5M_CHANGE_PCT: float = 0.80
    # 首次急拉形态写入自动执行日志后，在同一交易日短时保留“已触发”状态；
    # 只允许随后回到零轴/VWAP附近且5分钟动能收敛时恢复为可执行候选。
    PAPER_AUTO_REVERSAL_ARM_TTL_MINUTES: int = 90
    PAPER_AUTO_MA5_PULLBACK_ENABLED: bool = True
    PAPER_AUTO_MA5_PULLBACK_MIN_SCORE: float = 88.0
    PAPER_AUTO_MA5_PULLBACK_MAX_DIST_PCT: float = 2.0
    PAPER_AUTO_MA5_PULLBACK_MAX_CHANGE_PCT: float = 3.5
    PAPER_AUTO_MA5_PULLBACK_MIN_CHANGE_PCT: float = -1.5
    PAPER_AUTO_MA5_PULLBACK_MIN_VOLUME_RATIO: float = 0.8
    PAPER_AUTO_MA5_PULLBACK_MAX_VOLUME_RATIO: float = 2.2
    PAPER_AUTO_MA5_PULLBACK_MAX_5D_CHANGE_PCT: float = 18.0
    PAPER_AUTO_MA5_PULLBACK_MAX_BUY_AMOUNT: int = 5000
    PAPER_AUTO_MA5_PULLBACK_MAX_DAILY_LAYERS: int = 2
    PAPER_AUTO_MA5_PULLBACK_MAX_TOTAL_AMOUNT: int = 400
    # 禁止单一评分触发近满仓；高质量信号同样先买一层，下一交易日再确认加仓。
    PAPER_AUTO_FULL_CONVICTION_ENABLED: bool = False
    PAPER_AUTO_FULL_CONVICTION_MIN_SCORE: float = 96.0
    PAPER_AUTO_FULL_CONVICTION_POSITION_PCT: float = 0.95
    PAPER_AUTO_FULL_CONVICTION_MAX_MA5_DIST_PCT: float = 1.0
    PAPER_AUTO_FULL_CONVICTION_MAX_CHANGE_PCT: float = 2.5
    PAPER_AUTO_FULL_CONVICTION_MIN_VOLUME_RATIO: float = 1.0
    PAPER_AUTO_GREEN_REVERSAL_ENABLED: bool = True
    PAPER_AUTO_GREEN_REVERSAL_OPEN_DROP_PCT: float = 0.5
    PAPER_AUTO_GREEN_REVERSAL_LIMIT_BUFFER_PCT: float = 1.0
    PAPER_AUTO_GREEN_REVERSAL_MIN_CHANGE_PCT: float = 1.5
    PAPER_AUTO_GREEN_REVERSAL_MAX_CHANGE_PCT: float = 6.5
    PAPER_AUTO_GREEN_REVERSAL_MIN_VOLUME_RATIO: float = 1.0
    PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_STRENGTH: float = 70.0
    PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_CHANGE_PCT: float = 1.0
    PAPER_AUTO_GREEN_REVERSAL_MIN_SEAL_QUALITY: float = 35.0
    PAPER_AUTO_GREEN_REVERSAL_NEAR_LIMIT_CHANGE_PCT: float = 8.5
    PAPER_AUTO_GREEN_REVERSAL_NEAR_LIMIT_MIN_SECTOR_STRENGTH: float = 70.0
    PAPER_AUTO_GREEN_REVERSAL_LEADER_ENABLED: bool = True
    PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_CHANGE_PCT: float = 4.5
    PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_PREMIUM_PCT: float = 4.0
    PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_VOLUME_RATIO: float = 1.8
    PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_ORDERBOOK: float = 0.55
    PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_SECTOR_STRENGTH: float = 20.0
    PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_LIMIT_UP_COUNT: int = 3
    PAPER_AUTO_GREEN_REVERSAL_LEADER_MAX_BUY_AMOUNT: int = 100
    PAPER_AUTO_UNDERWATER_REVERSAL_ENABLED: bool = True
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT: float = 1.5
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_CHANGE_PCT: float = 0.2
    PAPER_AUTO_UNDERWATER_REVERSAL_MAX_CHANGE_PCT: float = 5.8
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO: float = 0.8
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_ORDERBOOK: float = 0.2
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LIMIT_UP_COUNT: int = 3
    # 涨停家数只能证明题材宽度，不能单独覆盖弱板块/净流出；扩散放行还需强度与资金同向。
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_SECTOR_STRENGTH: float = 50.0
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_SECTOR_CHANGE_PCT: float = 0.0
    PAPER_AUTO_UNDERWATER_REVERSAL_REQUIRE_POSITIVE_FUND_FLOW: bool = True
    PAPER_AUTO_UNDERWATER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT: float = 3.0
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN_CLOSE_POSITION: float = 0.45
    PAPER_AUTO_UNDERWATER_REVERSAL_MIN5_CHANGE_FLOOR: float = -1.0
    PAPER_AUTO_CONTINUATION_LIMITUP_GAP_RISK_PCT: float = 3.0
    PAPER_AUTO_CONTINUATION_LIMITUP_WEAK_CHANGE_PCT: float = 1.0
    PAPER_AUTO_CONTINUATION_MIN_SECTOR_DAYS: int = 2
    PAPER_AUTO_CONTINUATION_MIN_SECTOR_STRENGTH: float = 82.0
    PAPER_AUTO_CONTINUATION_MIN_LIMIT_UP_COUNT: int = 5
    PAPER_AUTO_REBOUND_DROP_PCT: float = 3.0
    PAPER_AUTO_REBOUND_MIN_VOLUME_RATIO: float = 1.0
    PAPER_AUTO_REBOUND_CRASH_PCT: float = 5.0
    PAPER_AUTO_REBOUND_CONFIRM_CHANGE_PCT: float = 3.0
    PAPER_AUTO_REBOUND_CLUSTER_DAYS: int = 5
    PAPER_AUTO_REBOUND_CLUSTER_DROP_COUNT: int = 2
    PAPER_AUTO_REBOUND_CLUSTER_CONFIRM_CHANGE_PCT: float = 5.0
    PAPER_AUTO_MAX_DAILY_NEW_BUYS: int = 2
    PAPER_AUTO_MAX_DAILY_BUYS_PER_SECTOR: int = 2
    PAPER_AUTO_OPEN_NOISE_END: str = "09:45"
    PAPER_AUTO_LATE_NEW_BUY_CUTOFF: str = "14:00"
    # 自动买入的数据/市场总闸门；任何来源降级都保留卖出管理但禁止新开仓。
    PAPER_MARKET_QUALITY_MIN_BREADTH_COVERAGE: float = 0.95
    PAPER_MARKET_WEAK_BREADTH_RATIO: float = 0.50
    PAPER_MARKET_WEAK_INDEX_AVG_CHANGE_PCT: float = -1.0
    PAPER_MARKET_WEAK_A_POSITION_FACTOR: float = 0.50
    # === 2026-08-31 复盘放宽：胜率 48.7%/盈亏比 0.57 期望值为负 ===
    # 把剥头皮式的紧止损止盈放宽到波段式, 给趋势更多空间, 让盈利单跑出去。
    PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT: float = 6.5
    # === 2026-09-17 复盘修复：开盘噪声窗的"独立走弱证据"门槛 ===
    # 缺陷：窗内止损豁免写成 weak_confirmations < 2，而 weak 计数包含
    # 「现价<开盘」「现价<均价」两项——止损价被击穿时这两项必然成立（同义反复），
    # 历史 61 次窗内止损中 <2 的为 0 次，open_severe_stop_loss_pct 从未生效。
    # 现在只统计与"处于日内低位"不构成同义反复的独立证据：五档卖压 / 放量下跌 / 5分钟急跌。
    # 2 = 修正后默认（窗内止损需 ≥2 项独立证据，或亏损超过 open_severe_stop_loss_pct）
    # 0 = 关闭豁免，等价于修复前的实际行为（窗内止损一律放行）
    PAPER_AUTO_OPEN_NOISE_STOP_MIN_EVIDENCE: int = 2
    # 窗内弱触发（跌破分时均价/开盘价/MA5、冲高回落、收弱、板块退潮等）的最低独立证据数。
    # 修复前为"窗内一律硬禁止"，导致 09:45 整点解除时集中释放（历史 52% 的定时卖出挤在该 5 分钟）。
    # 改为门槛后，自带独立证据的弱触发可提前生效；99 = 恢复修复前的硬禁止行为。
    PAPER_AUTO_OPEN_NOISE_WEAK_MIN_EVIDENCE: int = 1
    # A股T+1 阻塞日志的同键刷新间隔（秒）。同一 (账户,股票,交易日,原因) 只在
    # 首次落一条 skip_sell，其后每满该间隔把同一条的"末次复现"时间刷新一次。
    # 生产个案：账户3 600105 单日 3,028 条重复日志。0 = 每个行情轮次都刷新（仅用于诊断）。
    PAPER_T1_SKIP_LOG_REFRESH_MIN_SEC: float = 1800.0
    PAPER_AUTO_TAKE_PROFIT_PCT: float = 5.5
    PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PROFIT_PCT: float = 3.0
    PAPER_AUTO_BREAKEVEN_PROTECT_LOW_PCT: float = -0.2
    PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PCT: float = 0.6
    PAPER_AUTO_STOP_LOSS_PCT: float = 5.0
    PAPER_AUTO_SMALL_STOP_LOSS_PCT: float = 2.0
    PAPER_AUTO_NEXT_DAY_MIN_PROFIT_PCT: float = 0.5
    PAPER_AUTO_PULLBACK_FROM_HIGH_PCT: float = 2.5
    PAPER_AUTO_VOLUME_NEGATIVE_RATIO: float = 1.2
    PAPER_AUTO_SECTOR_RETREAT_STRENGTH: float = 55.0
    PAPER_AUTO_MAX_HOLD_DAYS: int = 5
    # === 2026-08-31 新增：回撤开仓闸门总开关 ===
    # pause    : 保守模式，回撤 ≥ 2% 暂停开仓
    # cautious : 跳过 paper 层暂停，但保留 risk 层 15% 熔断
    # unlimited: 两层闸门全部跳过, 只保留单笔止损/止盈/仓位上限等单仓位级风控,
    #            适合"穿越牛熊持续评估算法胜率"场景(2026-08-31 用户显式开启)
    # 注意: unlimited 模式只是不阻断开仓, 不代表无风险。_run_auto_sells 仍按止损/止盈/时间止损正常工作。
    PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE: str = "unlimited"

    # === 策略B：晋级二板（候选审计与隔离模拟买入均开启） ===
    # 2026-09-01 最终回放直接复用当前参数/仓位/并发限制并排除涨停开盘假成交：
    # 1178信号、59笔可评估，均收-0.90%、胜率33.9%、总盈亏-6129.46元。
    # 2026-09-02 用户明确要求 A-F 全路线参与前向模拟盘观察；保持原阈值、仓位与成交风控，不连接真实券商。
    PAPER_PROMOTION_ENABLED: bool = True
    # 持续前向模拟实验：历史表现仅影响实盘晋级，不暂停本策略的模拟买入。
    PAPER_PROMOTION_AUTO_ORDER_ENABLED: bool = True
    PAPER_PROMOTION_MIN_PROBABILITY: float = 0.25
    PAPER_PROMOTION_MAX_DAILY_BUYS: int = 2
    PAPER_PROMOTION_MAX_POSITIONS: int = 3
    PAPER_PROMOTION_POSITION_PCT: float = 0.20
    PAPER_PROMOTION_TAKE_PROFIT_PCT: float = 12.0
    PAPER_PROMOTION_STOP_LOSS_PCT: float = 5.0
    PAPER_PROMOTION_MAX_HOLD_DAYS: int = 3
    PAPER_PROMOTION_MIN_INTRADAY_CONFIRM_CHANGE_PCT: float = 0.0   # 09:35后现价涨幅下限(≥0不低开)
    PAPER_PROMOTION_MAX_INTRADAY_CONFIRM_CHANGE_PCT: float = 5.8   # 追高上限(接近涨停不追)
    PAPER_PROMOTION_ENTRY_CUTOFF: str = "10:30"
    PAPER_PROMOTION_MIN_SECTOR_STRENGTH: float = 50.0
    PAPER_PROMOTION_MIN_RELATIVE_STRENGTH_PCT: float = 0.50
    PAPER_PROMOTION_MAX_PULLBACK_FROM_HIGH_PCT: float = 1.50

    # === 策略C：主线扩散首板（主次账户持续模拟，旧回放结论仅作研究证据） ===
    # 2026-09-01 最终回放：3296信号、31笔可评估，均收-0.15%、
    # 胜率41.9%、总盈亏-1232.98元；同时当日首板路由结构性低召回。
    # C与C2各自按本路线规则入场，均保留完整卖出管理，不以旧胜率关闭模拟。
    PAPER_MAINLINE_ENABLED: bool = True
    PAPER_MAINLINE_AUTO_ORDER_ENABLED: bool = True
    PAPER_MAINLINE_MIN_PROBABILITY: float = 0.40
    PAPER_MAINLINE_MAX_DAILY_BUYS: int = 2
    PAPER_MAINLINE_MAX_POSITIONS: int = 3
    PAPER_MAINLINE_POSITION_PCT: float = 0.20
    PAPER_MAINLINE_TAKE_PROFIT_PCT: float = 8.0
    PAPER_MAINLINE_STOP_LOSS_PCT: float = 2.5
    PAPER_MAINLINE_MAX_HOLD_DAYS: int = 3
    PAPER_MAINLINE_MIN_INTRADAY_CONFIRM_CHANGE_PCT: float = -0.5  # 主线扩散允许小低开
    PAPER_MAINLINE_MAX_INTRADAY_CONFIRM_CHANGE_PCT: float = 4.0   # 补涨票不追高
    # 当前治理模型把首板预测与交易确认拆开；C 只允许当日盘中不可变快照中
    # 已进入正式榜/召回榜的主线路线，再用同一板块的实时扩散强度二次确认。
    # 阈值复用 promotion 主线扩散既有口径，不允许回退旧快照或直接放行观察池。
    PAPER_MAINLINE_LIVE_CONFIRM_ENABLED: bool = True
    PAPER_MAINLINE_LIVE_CONFIRM_MIN_PROBABILITY: float = 0.02
    PAPER_MAINLINE_LIVE_CONFIRM_MIN_STRICT_CONFIRMATIONS: int = 2
    PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_STRENGTH: float = 50.0
    PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_LIMIT_UP_COUNT: int = 6

    # === 策略D：竞价强攻（主次账户持续模拟，竞价证据仍必须完整） ===
    # 2026-09-01 最终回放只有9信号、3笔可评估；即使均收+2.59%，样本不足。
    # 当日83只涨停又没有可验证的完整09:15-09:25虚拟撮合路径，无法审计可识别性。
    # D与D2分别消费高开强攻/竞价恢复规则，不用缺失竞价数据强行补单。
    PAPER_AUCTION_ENABLED: bool = True
    PAPER_AUCTION_AUTO_ORDER_ENABLED: bool = True
    PAPER_AUCTION_MIN_PROBABILITY: float = 0.20
    PAPER_AUCTION_MAX_DAILY_BUYS: int = 1
    PAPER_AUCTION_MAX_POSITIONS: int = 2
    PAPER_AUCTION_POSITION_PCT: float = 0.15
    PAPER_AUCTION_TAKE_PROFIT_PCT: float = 5.0
    PAPER_AUCTION_STOP_LOSS_PCT: float = 4.0
    PAPER_AUCTION_MAX_HOLD_DAYS: int = 2
    PAPER_AUCTION_MIN_INTRADAY_CONFIRM_CHANGE_PCT: float = 1.0   # 竞价高开强攻要求开盘明显高开
    PAPER_AUCTION_MAX_INTRADAY_CONFIRM_CHANGE_PCT: float = 6.0   # 但不超过 6% 防追板

    # === 策略E：连板高标接力（保留，严格成交约束） ===
    # 原十倍评分路线已被证伪并撤回。2026-09-01 最终回放直接复用当前
    # 18/6/3、15%仓位、每日1笔/最多3仓，排除非主板和涨停开盘假成交：
    # 84信号、23笔可评估，均收6.14%、胜率65.2%、盈亏比1.64。
    # 样本仍小且日K不能证明排队成交，因此只保留现有质量门和真实撮合约束，
    # 不提高仓位、不放宽连板/封单/炸板条件。
    PAPER_TENBAGGER_ENABLED: bool = True
    PAPER_TENBAGGER_AUTO_ORDER_ENABLED: bool = True
    PAPER_TENBAGGER_MIN_SCORE: float = 85.0
    PAPER_TENBAGGER_MAX_DAILY_BUYS: int = 1
    PAPER_TENBAGGER_MAX_POSITIONS: int = 3
    PAPER_TENBAGGER_POSITION_PCT: float = 0.15
    PAPER_TENBAGGER_TAKE_PROFIT_PCT: float = 15.0
    PAPER_TENBAGGER_STOP_LOSS_PCT: float = 8.0
    PAPER_TENBAGGER_MAX_HOLD_DAYS: int = 30
    PAPER_TENBAGGER_MAX_INTRADAY_CONFIRM_CHANGE_PCT: float = 3.0  # 中线买入日不追高
    # 高标接力参数 (2026-08-31 重建, 取代十倍评分):
    PAPER_HIGHBOARD_MIN_CONSECUTIVE: int = 4      # 连板≥4 才买 (诊断: 4板+10日+2.4%, 5板+5.0%)
    PAPER_HIGHBOARD_MAX_CONSECUTIVE: int = 8      # 连板≤8 (9板+样本仅9个, 风险过大)
    PAPER_HIGHBOARD_MAX_HOLD_DAYS: int = 3        # 高标接力持仓≤3日 (2026-08-31 扫描最优)
    PAPER_HIGHBOARD_TAKE_PROFIT_PCT: float = 18.0 # 止盈18% (扫描最优: 15-20%平台, 18%均收最高)
    PAPER_HIGHBOARD_STOP_LOSS_PCT: float = 6.0    # 止损6% (扫描最优: 6%胜过8/10, 单笔亏损更小)
    PAPER_HIGHBOARD_MIN_SEAL_AMOUNT: float = 1.0  # 封板资金≥1亿 (有承接)
    PAPER_HIGHBOARD_MAX_BREAK_COUNT: int = 2      # 炸板≤2次 (封板质量)
    PAPER_HIGHBOARD_REQUIRE_ABOVE_VWAP: bool = True
    PAPER_HIGHBOARD_MAX_PULLBACK_FROM_HIGH_PCT: float = 2.0
    PAPER_HIGHBOARD_MAX_PEAK_CHANGE_PCT: float = 6.0
    PAPER_HIGHBOARD_CONFIRM_MIN_SAMPLES: int = 3
    PAPER_HIGHBOARD_CONFIRM_MIN_PERSISTENCE_SEC: int = 120
    # 高标可交易窗口常集中在开盘后数分钟；仅 E 提前到连续竞价开始，其他策略仍从09:35起。
    PAPER_HIGHBOARD_INTRADAY_BUY_START: str = "09:30"
    # 非一字板回封后允许按涨停价排队，但不即时虚假成交：开板或排队后成交量
    # 覆盖下单时买一队列才撮合，14:50仍未成交则撤单。
    PAPER_HIGHBOARD_LIMIT_UP_QUEUE_ENABLED: bool = True
    PAPER_HIGHBOARD_QUEUE_VOLUME_COVER_RATIO: float = 1.0
    PAPER_HIGHBOARD_QUEUE_CANCEL_TIME: str = "14:50"
    # 重建参数 (2026-08-31, 已验证无效, 预留备用):
    PAPER_TENBAGGER_MID_CAP_MIN: float = 50.0       # 流通市值下限(亿)
    PAPER_TENBAGGER_MID_CAP_MAX: float = 200.0      # 流通市值上限(亿)
    PAPER_TENBAGGER_PULLBACK_MA20_MAX_DIST_PCT: float = 3.0   # 现价距MA20偏离≤3%
    PAPER_TENBAGGER_PULLBACK_FROM_HIGH_MIN_PCT: float = 5.0   # 从60日高点回撤≥5%
    PAPER_TENBAGGER_PULLBACK_FROM_HIGH_MAX_PCT: float = 20.0  # 回撤≤20%
    PAPER_TENBAGGER_SHRINK_VOLUME_RATIO: float = 0.8  # 近5日均量/前20日均量≤0.8
    PAPER_TENBAGGER_MAX_HOLD_DAYS: int = 30
    # === 策略F1: 收盘确认后的断板反包次日接力（持续模拟，保留亏损实验样本） ===
    # 2026-09-01 最终校正：主板3056只/562.9万行；修复断板日 off-by-one、
    # 窗口回撤、T+1与右删失后，严格子集397信号/388笔可评估，12/8/3
    # 均收-1.01%、胜率41.5%、剔Top5后-1.41%，扫描组合均未恢复正收益。
    # 2026-09-07用户批准恢复持续模拟买卖；以上旧回放不能代替新协议前向验证。
    PAPER_REVERSAL_ENABLED: bool = True
    PAPER_REVERSAL_AUTO_ORDER_ENABLED: bool = True
    PAPER_REVERSAL_MIN_CONSECUTIVE: int = 3      # 断板前连板≥3 (2板反包负期望, 排除)
    PAPER_REVERSAL_MAX_GAP_DAYS: int = 3         # 断板交易日≤3 (强势不拖泥带水)
    PAPER_REVERSAL_MIN_DIP_PCT: float = -5.0     # 断板窗口内最深跌幅≤-5% (深跌洗盘确认)
    PAPER_REVERSAL_MIN_VOL_RATIO: float = 1.5    # 反包日量比≥1.5 (放量确认)
    PAPER_REVERSAL_TAKE_PROFIT_PCT: float = 12.0 # 历史兼容参数；不代表已验证最优
    PAPER_REVERSAL_STOP_LOSS_PCT: float = 8.0    # 历史兼容参数；不代表已验证最优
    PAPER_REVERSAL_MAX_HOLD_DAYS: int = 3        # 持仓≤3日 (快进快出)
    PAPER_REVERSAL_MAX_DAILY_BUYS: int = 1
    PAPER_REVERSAL_MAX_POSITIONS: int = 2
    PAPER_REVERSAL_POSITION_PCT: float = 0.10    # 小仓位观察 (样本少+风格风险)
    PAPER_REVERSAL_MAX_INTRADAY_CONFIRM_CHANGE_PCT: float = 3.0
    PAPER_REVERSAL_REQUIRE_ABOVE_VWAP: bool = True
    PAPER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT: float = 2.0
    # 策略版本写入每笔持仓/成交/决策，防止重启或升级后混算。
    PAPER_STRATEGY_A_VERSION: str = "paper_a_value_entry_v2"
    PAPER_STRATEGY_B_VERSION: str = "paper_b_promotion_dry_v2"
    PAPER_STRATEGY_C_VERSION: str = "paper_c_mainline_v2"
    PAPER_STRATEGY_D_VERSION: str = "paper_d_auction_quality_v2"
    PAPER_STRATEGY_E_VERSION: str = "paper_e_highboard_v2"
    PAPER_STRATEGY_F_VERSION: str = "paper_f_research_v2"
    # === 2026-08-31 复盘新增：硬止损仓位上限 ===
    # 任一持仓触发 PAPER_AUTO_STOP_LOSS_PCT 硬止损时, 单笔最大亏损不超过总资产的此比例。
    # 防止"小仓位 + 紧止损"被大额买入穿透, 出现 -3% × 5000 股 = -1500 的单笔巨亏。
    PAPER_AUTO_HARD_STOP_MAX_LOSS_PCT: float = 2.0
    PAPER_AUTO_TRADE_T_ENABLED: bool = True
    PAPER_AUTO_T_SELL_PCT: float = 0.50
    PAPER_AUTO_T_WEAK_SELL_PCT: float = 0.33
    PAPER_AUTO_SHORT_FULL_EXIT_PROFIT_MAX_AMOUNT: int = 300
    PAPER_AUTO_T_BUYBACK_DROP_PCT: float = 1.0
    PAPER_AUTO_T_BUYBACK_MAX_PER_DAY: int = 1
    PAPER_AUTO_T_BUYBACK_MAX_ABOVE_COST_PCT: float = 0.3

    # === 盘中预测调度预算（只影响调度，不调整模型或交易门槛） ===
    NEWS_FETCH_SOURCE_TIMEOUT_SEC: float = 20.0
    PROMOTION_NEWS_CHECK_TIMEOUT_SEC: float = 2.0
    PROMOTION_NEWS_REFRESH_TIMEOUT_SEC: float = 90.0
    PROMOTION_NEWS_REFRESH_COOLDOWN_SEC: float = 300.0
    PROMOTION_SNAPSHOT_TIMEOUT_SEC: float = 120.0
    PROMOTION_SNAPSHOT_RETRY_COOLDOWN_SEC: float = 30.0
    # 只读恢复探测预算（不是预测/交易阈值）；日历/台账异常不得卡死守护。
    PROMOTION_RECOVERY_PROBE_TIMEOUT_SEC: float = Field(default=3.0, gt=0, le=30, allow_inf_nan=False)

    # === 晋级预测模型治理 ===
    # legacy: 仅冠军模型；shadow: 记录挑战者但不改变生产输出；compare: 同时展示对比。
    PROMOTION_RUNTIME_MODE: str = "legacy"
    PROMOTION_CHAMPION_MODEL_VERSION: str = "promotion_v20260829_27_governed"
    PROMOTION_CHALLENGER_MODEL_VERSION: str = ""
    PROMOTION_FEATURE_VERSION: str = "legacy_point_in_time_features_v2"
    PROMOTION_DATA_VERSION: str = "legacy_labels_v2_regime_freeze_20260829"
    # off/monitor/enforce；历史标签修复完成前默认 monitor，避免无声使用坏数据。
    PROMOTION_QUALITY_GATE_MODE: str = "monitor"
    PROMOTION_QUALITY_GATE_LOOKBACK_DAYS: int = 120
    PROMOTION_ARTIFACT_DIR: str = "artifacts/promotion"
    # 独立研究归档；默认没有审定源解析器，不改变生产评分/质量门。
    PROMOTION_DAILY_MATERIAL_DIR: Path = _BACKEND_DIR.parent / "runtime" / "promotion_daily_materials"
    PROMOTION_DAILY_MATERIAL_READ_TIMEOUT_SEC: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    PROMOTION_TRAINING_MIN_SAMPLES: int = 500
    PROMOTION_TRAINING_MIN_TRADE_DAYS: int = 30
    PROMOTION_TRAINING_MIN_POSITIVES: int = 20
    # 影子运行只比较同一冻结候选集；自动化不会晋级，真实切换必须人工确认。
    PROMOTION_SHADOW_AUTOMATION_ENABLED: bool = True
    PROMOTION_SHADOW_MIN_KLINE_ROWS: int = 1000
    PROMOTION_SHADOW_MIN_KLINE_COMPLETENESS: float = 0.95
    # 人工审批事件存在时允许概率覆盖层驱动后续候选排序；无审批事件时完全不生效。
    PROMOTION_DEPLOYED_OVERLAY_ENABLED: bool = True
    # 生产模型晋级/回滚必须携带服务端配置的管理令牌；空值时写接口 fail closed。
    PROMOTION_GOVERNANCE_TOKEN: str = ""
    PROMOTION_GOVERNANCE_OPERATOR: str = "governance_admin"
    # 旧版按单日/近样本直接改类变量的反馈环默认永久关闭；仅保留代码供可审计回放。
    PROMOTION_LEGACY_DAILY_WEIGHT_ADJUSTMENT_ENABLED: bool = False

    # === 三阶段复盘自动化（只生成快照/告警，不自动改参或晋级） ===
    REVIEW_AUTOMATION_ENABLED: bool = True
    REVIEW_AUTOMATION_RETRY_ATTEMPTS: int = 2
    REVIEW_AUTOMATION_RETRY_DELAY_SECONDS: int = 30
    REVIEW_PREMARKET_TIME: str = "08:45"
    REVIEW_INTRADAY_TIME: str = "11:35"
    REVIEW_REGIME_TIME: str = "20:10"
    REVIEW_POSTMARKET_TIME: str = "20:35"
    # GPT 自动复盘：配置为可执行命令时在盘后生成结构化报告；空值时生成接口 fail closed，
    # 确定性复盘快照不受影响。命令从 stdin 读入 JSON 上下文，stdout 输出 JSON 报告。
    REVIEW_GPT_CLI: str = ""
    REVIEW_GPT_PROVIDER: str = "dsh_headless"
    REVIEW_GPT_MODEL: str = ""
    REVIEW_GPT_ENABLED: bool = False

    # === 数据质量 ===
    DATA_STALE_THRESHOLD_SEC: int = 30    # 数据过期阈值(秒)
    DATA_COMPLETENESS_MIN: float = 0.95   # 最低完整率

    # === 路径 ===
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    PUSH_TEMPLATES_DIR: Path = BASE_DIR / "push" / "templates"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


settings = Settings()
