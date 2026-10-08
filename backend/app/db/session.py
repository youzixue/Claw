"""数据库会话管理"""

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from loguru import logger

from app.config.settings import settings
from app.db.engine_options import async_engine_options


engine = create_async_engine(
    settings.DATABASE_URL,
    **async_engine_options(
        settings.DATABASE_URL,
        echo=settings.SQL_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
    ),
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """ORM 基类"""
    pass


async def _ensure_stock_spot_orderbook_columns(conn):
    """为已存在的 stock_spot 表补齐五档盘口字段."""
    result = await conn.execute(text("PRAGMA table_info(stock_spot)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "bid1_price": "FLOAT",
        "bid1_volume": "BIGINT",
        "bid2_price": "FLOAT",
        "bid2_volume": "BIGINT",
        "bid3_price": "FLOAT",
        "bid3_volume": "BIGINT",
        "bid4_price": "FLOAT",
        "bid4_volume": "BIGINT",
        "bid5_price": "FLOAT",
        "bid5_volume": "BIGINT",
        "ask1_price": "FLOAT",
        "ask1_volume": "BIGINT",
        "ask2_price": "FLOAT",
        "ask2_volume": "BIGINT",
        "ask3_price": "FLOAT",
        "ask3_volume": "BIGINT",
        "ask4_price": "FLOAT",
        "ask4_volume": "BIGINT",
        "ask5_price": "FLOAT",
        "ask5_volume": "BIGINT",
        "bid_depth_5": "BIGINT",
        "ask_depth_5": "BIGINT",
        "orderbook_imbalance": "FLOAT",
        "bid_ask_spread": "FLOAT",
        "seal_quality_score": "FLOAT",
        "support_strength_score": "FLOAT",
        "withdrawal_ratio": "FLOAT",
        "source_quote_at": "DATETIME",
        "received_at": "DATETIME",
        "quote_round_id": "VARCHAR(64)",
    }
    for name, column_type in columns.items():
        if name in existing:
            continue
        await conn.execute(text(f"ALTER TABLE stock_spot ADD COLUMN {name} {column_type}"))
        logger.info(f"stock_spot 自动补列: {name}")


async def _ensure_paper_trade_columns(conn):
    """为已存在的模拟盘交易表补齐后续统计字段."""
    result = await conn.execute(text("PRAGMA table_info(paper_trade_log)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "realized_pnl": "FLOAT",
        "strategy_version": "VARCHAR(64)",
        "tax": "FLOAT DEFAULT 0",
        "decision_round_id": "VARCHAR(64)",
        "fill_round_id": "VARCHAR(64)",
        "forced_probe": "BOOLEAN DEFAULT 0 NOT NULL",
        "excluded_from_performance": "BOOLEAN DEFAULT 0 NOT NULL",
    }
    for name, column_type in columns.items():
        if name in existing:
            continue
        await conn.execute(text(f"ALTER TABLE paper_trade_log ADD COLUMN {name} {column_type}"))
        logger.info(f"paper_trade_log 自动补列: {name}")


async def _ensure_paper_position_columns(conn):
    """兼容历史 SQLite 持仓表；正式结构仍由 Alembic 迁移维护。"""
    result = await conn.execute(text("PRAGMA table_info(paper_position)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "entry_sector_code": "VARCHAR(20)",
        "entry_sector_name": "VARCHAR(30)",
        "strategy_version": "VARCHAR(64)",
    }
    for name, column_type in columns.items():
        if name in existing:
            continue
        await conn.execute(text(f"ALTER TABLE paper_position ADD COLUMN {name} {column_type}"))
        logger.info(f"paper_position 自动补列: {name}")


async def _ensure_finance_news_columns(conn):
    """为已存在的 finance_news 表补齐缓存与NLP字段."""
    result = await conn.execute(text("PRAGMA table_info(finance_news)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "url": "TEXT",
        "summary": "TEXT",
        "events_json": "TEXT",
        "nlp_status": "VARCHAR(20)",
        "sentiment_method": "VARCHAR(20)",
        "events_method": "VARCHAR(20)",
        "nlp_error": "TEXT",
        "nlp_analyzed_at": "DATETIME",
    }
    added_nlp_status = "nlp_status" not in existing
    for name, column_type in columns.items():
        if name in existing:
            continue
        await conn.execute(text(f"ALTER TABLE finance_news ADD COLUMN {name} {column_type}"))
        logger.info(f"finance_news 自动补列: {name}")
    if added_nlp_status:
        try:
            await conn.execute(text(
                "UPDATE finance_news "
                "SET nlp_status = CASE "
                "WHEN COALESCE(bull_bear_confidence, 0) > 0 THEN 'analyzed' "
                "ELSE 'raw' END "
                "WHERE nlp_status IS NULL"
            ))
        except OperationalError as exc:
            logger.warning(f"finance_news nlp_status 历史数据回填跳过: {exc}")


async def _ensure_signal_performance_columns(conn):
    """兼容历史开发库；正式结构仍由 Alembic 迁移维护。"""
    result = await conn.execute(text("PRAGMA table_info(signal_performance)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "signal_variant": "VARCHAR(40)",
        "setup_grade": "VARCHAR(30)",
        "net_return_1d": "FLOAT",
        "net_return_3d": "FLOAT",
        "net_return_5d": "FLOAT",
        "net_return_10d": "FLOAT",
        "benchmark_return_1d": "FLOAT",
        "benchmark_return_3d": "FLOAT",
        "benchmark_return_5d": "FLOAT",
        "benchmark_return_10d": "FLOAT",
        "excess_return_1d": "FLOAT",
        "excess_return_3d": "FLOAT",
        "excess_return_5d": "FLOAT",
        "excess_return_10d": "FLOAT",
        "evaluation_version": "VARCHAR(20)",
    }
    for name, column_type in columns.items():
        if name in existing:
            continue
        await conn.execute(text(f"ALTER TABLE signal_performance ADD COLUMN {name} {column_type}"))
        logger.info(f"signal_performance 自动补列: {name}")


async def _ensure_dashboard_snapshot_indexes(conn):
    """历史SQLite库兼容：避免预案/雷达快照冷启动全表扫描。"""
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_dashboard_snapshot_lookup "
        "ON dashboard_snapshot (snapshot_key, trade_date, status, snapshot_time)"
    ))


async def _ensure_close_quality_columns(conn):
    """兼容未运行 Alembic 的历史 SQLite；正式结构仍由 022 迁移维护。"""
    table_columns = {
        "fund_flow": {
            "source": "VARCHAR(20)",
            "source_version": "VARCHAR(40)",
            "observed_at": "DATETIME",
            # 025正式迁移同口径：只补空列，绝不回填历史源时钟。
            "source_quote_at": "DATETIME",
            "received_at": "DATETIME",
            # 027: 可空实测订单细分；历史不按主力减大单推算，不补0。
            "super_net_inflow": "FLOAT",
            "super_net_inflow_pct": "FLOAT",
            "big_net_inflow_pct": "FLOAT",
            "mid_net_inflow_pct": "FLOAT",
            "small_net_inflow_pct": "FLOAT",
        },
        # 026: 仅新增空的竞价来源证据列，不用旧auction_time回填。
        "auction_data": {
            "source": "VARCHAR(20)",
            "source_version": "VARCHAR(64)",
            "source_quote_at": "DATETIME",
            "received_at": "DATETIME",
            "observed_at": "DATETIME",
            "price_basis": "VARCHAR(32)",
            "volume_basis": "VARCHAR(32)",
            "volume_unit": "VARCHAR(16)",
            "amount_unit": "VARCHAR(16)",
        },
        "stock_sector_mapping": {
            "source_version": "VARCHAR(40)",
            "observed_at": "DATETIME",
        },
        # 037：仅新增前向来源证据，不为历史补造时钟或字段质量。
        "limit_up_pool": {
            "source_version": "VARCHAR(40)", "source_quote_at": "DATETIME",
            "observed_at": "DATETIME", "evidence_json": "TEXT",
        },
        "limit_down_pool": {
            "source_version": "VARCHAR(40)", "source_quote_at": "DATETIME",
            "observed_at": "DATETIME", "evidence_json": "TEXT",
        },
        "broken_limit_pool": {
            "source_version": "VARCHAR(40)", "source_quote_at": "DATETIME",
            "observed_at": "DATETIME", "evidence_json": "TEXT",
            "limit_up_price": "FLOAT",
            "close_price": "FLOAT",
            "close_at_limit": "BOOLEAN",
            "final_state": "VARCHAR(24)",
        },
        "market_sentiment": {
            "sentiment_score": "FLOAT",
            "quality_status": "VARCHAR(16)",
            "quality_reason": "TEXT",
            "breadth_sample_count": "INTEGER",
            "breadth_coverage": "FLOAT",
            "index_avg_change_pct": "FLOAT",
            "calculation_version": "VARCHAR(32)",
            "observed_at": "DATETIME",
        },
        "paper_auto_trade_log": {
            "strategy_version": "VARCHAR(64)",
        },
    }
    for table_name, columns in table_columns.items():
        result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
        existing = {row[1] for row in result.fetchall()}
        for name, column_type in columns.items():
            if name in existing:
                continue
            await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}"))
            logger.info(f"{table_name} 自动补列: {name}")


async def _ensure_quote_round_execution_columns(conn):
    """兼容未运行024迁移的历史SQLite；新表仍由ORM/Alembic正式表达。"""
    table_columns = {
        "quote_round": {
            "focus_path": "TEXT",
        },
        "paper_auto_trade_log": {
            "quote_round_id": "VARCHAR(64)",
            "as_of_at": "DATETIME",
            "stage_code": "VARCHAR(32)",
            "reason_code": "VARCHAR(48)",
            "metric_value": "FLOAT",
            "threshold_value": "FLOAT",
            "config_version": "VARCHAR(64)",
            "code_version": "VARCHAR(64)",
        },
        "trade_order": {
            "strategy_version": "VARCHAR(64)",
            "idempotency_key": "VARCHAR(160)",
            "decision_round_id": "VARCHAR(64)",
            "last_fill_round_id": "VARCHAR(64)",
            "decision_at": "DATETIME",
            "as_of_at": "DATETIME",
            "config_version": "VARCHAR(64)",
            "code_version": "VARCHAR(64)",
        },
        "trade_fill": {
            "decision_round_id": "VARCHAR(64)",
            "fill_round_id": "VARCHAR(64)",
        },
        # 2026-09-17 复盘修复：sector_persistence 按 (sector_code, trade_date) upsert，
        # 盘中值会被盘后终值覆盖，此前无观测时点导致板块归因证据无法回溯。
        "sector_persistence": {
            "observed_at": "DATETIME",
        },
    }
    for table_name, columns in table_columns.items():
        result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
        existing = {row[1] for row in result.fetchall()}
        for name, column_type in columns.items():
            if name in existing:
                continue
            await conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}")
            )
            logger.info(f"{table_name} 自动补列: {name}")

    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_stock_spot_quote_round_id "
        "ON stock_spot(quote_round_id)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_paper_auto_trade_log_quote_round_id "
        "ON paper_auto_trade_log(quote_round_id)"
    ))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_order_idempotency_key "
        "ON trade_order(idempotency_key) WHERE idempotency_key IS NOT NULL"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_trade_order_decision_round_id "
        "ON trade_order(decision_round_id)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_trade_order_last_fill_round_id "
        "ON trade_order(last_fill_round_id)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_trade_fill_fill_round_id "
        "ON trade_fill(fill_round_id)"
    ))


async def _ensure_active_paper_account_unique_index(conn):
    """只约束 active 同名账户；已归档的 legacy default 仍保留可审计。"""
    duplicate_rows = (
        await conn.execute(text(
            "SELECT account_name, COUNT(*) FROM paper_account "
            "WHERE status = 'active' GROUP BY account_name HAVING COUNT(*) > 1"
        ))
    ).fetchall()
    if duplicate_rows:
        logger.error(
            "检测到重复 active 模拟账户，暂不创建唯一索引；请先归档重复账户: "
            f"{[(row[0], row[1]) for row in duplicate_rows]}"
        )
        return
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_account_active_name "
        "ON paper_account(account_name) WHERE status = 'active'"
    ))


async def init_db():
    """初始化数据库 — 创建所有表+启用WAL模式"""
    # 延迟导入，确保所有 model 都已注册
    from app.models import (  # noqa: F401
        stock, sector, factor, signal, news, risk, paper, governance, promotion, regime, review, backtest,
        trading,
    )

    async with engine.begin() as conn:
        # 启用WAL模式(并发读写性能优化)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_stock_spot_orderbook_columns(conn)
        await _ensure_paper_trade_columns(conn)
        await _ensure_paper_position_columns(conn)
        await _ensure_finance_news_columns(conn)
        await _ensure_signal_performance_columns(conn)
        await _ensure_dashboard_snapshot_indexes(conn)
        await _ensure_close_quality_columns(conn)
        await _ensure_quote_round_execution_columns(conn)
        await _ensure_active_paper_account_unique_index(conn)
    logger.info("数据库表创建完成")


async def get_db() -> AsyncSession:
    """依赖注入 — 获取数据库会话"""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
