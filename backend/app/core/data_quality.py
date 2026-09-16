"""数据质量监控 — P0核心"""

from datetime import datetime
from typing import Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.risk import DataSourceHealth
from app.core.prediction_data_quality import prediction_data_quality_auditor


class SourceHealth:
    """数据源健康状态"""
    def __init__(self, source: str, api_name: str = "", status: str = "up",
                 latency_ms: int = 0, completeness: float = 1.0, error_msg: str = ""):
        self.source = source
        self.api_name = api_name
        self.status = status
        self.latency_ms = latency_ms
        self.completeness = completeness
        self.error_msg = error_msg


class ValidationResult:
    """校验结果"""
    def __init__(self, is_valid: bool, errors: list[str] = None):
        self.is_valid = is_valid
        self.errors = errors or []


class DataQualityGuard:
    """数据质量守卫"""

    # 数值范围规则
    RANGE_RULES = {
        "close": (0.01, 100000),        # 收盘价
        "open": (0.01, 100000),
        "high": (0.01, 100000),
        "low": (0.01, 100000),
        "volume": (0, 1e12),            # 成交量
        "amount": (0, 1e13),            # 成交额
        "turnover": (0, 100),           # 换手率%
        "change_pct": (-30, 30),        # 涨跌幅% (考虑科创板20%+ST5%+新股首日无限制)
        "pe": (-999, 99999),            # PE
        "pb": (-100, 1000),             # PB
    }

    # 必填字段
    REQUIRED_FIELDS = {
        "stock_daily": ["code", "trade_date", "close", "volume"],
        "fund_flow": ["code", "trade_date"],
        "limit_up_pool": ["code", "trade_date"],
    }

    def validate_record(self, table: str, record: dict) -> ValidationResult:
        """校验单条数据"""
        errors = []

        # 必填字段检查
        required = self.REQUIRED_FIELDS.get(table, [])
        for field in required:
            if field not in record or record[field] is None:
                errors.append(f"必填字段缺失: {field}")

        # 数值范围检查
        for field, (low, high) in self.RANGE_RULES.items():
            if field in record and record[field] is not None:
                val = record[field]
                if val < low or val > high:
                    errors.append(f"字段{field}超出范围: {val} (预期{low}~{high})")

        # 特殊规则
        if "close" in record and record.get("close") == 0:
            errors.append("收盘价为0，疑似停牌或数据异常")

        if "volume" in record and record.get("volume", 0) < 0:
            errors.append("成交量为负数")

        return ValidationResult(is_valid=len(errors) == 0, errors=errors)

    async def check_source_health(self, session: AsyncSession, source: str) -> Optional[SourceHealth]:
        """查询数据源健康状态"""
        result = await session.execute(
            select(DataSourceHealth)
            .where(DataSourceHealth.source == source)
            .order_by(DataSourceHealth.id.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        return SourceHealth(
            source=row.source,
            api_name=row.api_name,
            status=row.status,
            latency_ms=row.latency_ms or 0,
            completeness=(
                float(row.completeness)
                if row.completeness is not None
                else 1.0
            ),
            error_msg=row.error_msg or "",
        )

    async def record_success(self, session: AsyncSession, source: str, api_name: str,
                             latency_ms: int, record_count: int = 0, expected_count: int = 0):
        """记录采集成功"""
        completeness = (
            min(max(record_count / expected_count, 0.0), 1.0)
            if expected_count > 0
            else 1.0
        )
        health = DataSourceHealth(
            source=source,
            api_name=api_name,
            status="up",
            last_success=datetime.now(),
            fail_streak=0,
            latency_ms=latency_ms,
            completeness=completeness,
        )
        session.add(health)
        await session.commit()
        logger.debug(f"数据源健康记录: {source}/{api_name} ✅ {latency_ms}ms 完整率{completeness:.2%}")

    async def record_failure(
        self,
        session: AsyncSession,
        source: str,
        api_name: str,
        error_msg: str,
        latency_ms: int = 0,
        completeness: float = 0.0,
    ):
        """记录采集失败或降级；完整率默认按0处理，绝不伪装成100%。"""
        completeness = min(max(float(completeness), 0.0), 1.0)
        # 查询最近一条记录获取连续失败次数
        result = await session.execute(
            select(DataSourceHealth)
            .where(DataSourceHealth.source == source, DataSourceHealth.api_name == api_name)
            .order_by(DataSourceHealth.id.desc())
            .limit(1)
        )
        last = result.scalar_one_or_none()
        fail_streak = (last.fail_streak + 1) if last else 1

        # 连续3次失败标记为down
        status = "down" if fail_streak >= 3 else "degraded"

        health = DataSourceHealth(
            source=source,
            api_name=api_name,
            status=status,
            last_failure=datetime.now(),
            fail_streak=fail_streak,
            latency_ms=latency_ms,
            completeness=completeness,
            error_msg=error_msg[:500],
        )
        session.add(health)
        await session.commit()

        if status == "down":
            logger.error(f"数据源降级: {source}/{api_name} 🚫 连续失败{fail_streak}次")
        else:
            logger.warning(f"数据源异常: {source}/{api_name} ⚠️ 连续失败{fail_streak}次: {error_msg[:100]}")

    async def get_all_health(self, session: AsyncSession) -> list[SourceHealth]:
        """获取所有数据源健康状态"""
        result = await session.execute(
            select(DataSourceHealth)
        )
        rows = result.scalars().all()

        # 每个source取最新一条
        latest = {}
        for row in rows:
            key = f"{row.source}:{row.api_name}"
            if key not in latest or row.id > latest[key].id:
                latest[key] = row

        return [
            SourceHealth(
                source=r.source, api_name=r.api_name, status=r.status,
                latency_ms=r.latency_ms or 0,
                completeness=(
                    float(r.completeness)
                    if r.completeness is not None
                    else 1.0
                ),
                error_msg=r.error_msg or "",
            )
            for r in latest.values()
        ]

    async def audit_prediction_data(
        self,
        session: AsyncSession,
        *,
        trade_date_value=None,
        snapshot_context: str = "",
        persist: bool = True,
        lookback_days: int = 120,
    ) -> dict:
        """Run the point-in-time prediction gate audit."""

        return await prediction_data_quality_auditor.audit(
            session,
            trade_date_value=trade_date_value,
            snapshot_context=snapshot_context,
            persist=persist,
            lookback_days=lookback_days,
        )


# 全局单例
data_quality_guard = DataQualityGuard()
