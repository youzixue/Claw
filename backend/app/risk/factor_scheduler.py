"""因子研究评估入口 — 只追加因子存储及真实收益IC，不自动调权

核心能力:
- 定时因子评估: 每日盘后自动评估所有因子
- 因子值批量存储: 截面因子值写入factor_values表
- 评估报告生成: IC/IR/衰减/排名汇总
- 权重隔离: 评估不调用优化器；存量因子缺PIT证明，不能据此晋级
- 因子健康监控: 检测衰减因子并预警
"""

from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.factors.base import FactorRegistry, FactorEngine, FactorCategory
from app.factors.evaluator import factor_evaluator, FactorEvaluationService
from app.models.factor import FactorValue, FactorEvaluation


# ========== 因子存储服务 ==========

class FactorStorageService:
    """因子值存储服务 — 批量存储截面因子值"""

    async def save_factor_values(self, session: AsyncSession,
                                  trade_date: date,
                                  factor_results: dict[str, dict]) -> int:
        """只追加尚不存在的旧格式因子值；历史记录不覆盖。

        Args:
            trade_date: 交易日期
            factor_results: {code: {factor_name: {value, rank, pct}}}
        """
        count = 0
        for code, factors in factor_results.items():
            for factor_name, result in factors.items():
                value = result.get("value")
                rank = result.get("rank")
                pct = result.get("pct")

                # 检查是否已存在
                existing = await session.execute(
                    select(FactorValue).where(
                        and_(
                            FactorValue.trade_date == trade_date,
                            FactorValue.stock_code == code,
                            FactorValue.factor_name == factor_name,
                        )
                    )
                )
                existing_record = existing.scalar_one_or_none()

                if existing_record:
                    # 旧表无版本/首次可用时钟，不能将重算结果覆盖成历史事前证据。
                    continue
                else:
                    record = FactorValue(
                        trade_date=trade_date,
                        stock_code=code,
                        factor_name=factor_name,
                        factor_value=value,
                        factor_rank=rank,
                        factor_pct=pct,
                    )
                    session.add(record)
                count += 1

        await session.commit()
        logger.info(f"因子值存储: {count}条, {trade_date}")
        return count

    async def get_factor_values(self, session: AsyncSession,
                                 factor_name: str,
                                 trade_date: date,
                                 codes: list[str] = None) -> list[dict]:
        """查询因子值"""
        query = select(FactorValue).where(
            and_(
                FactorValue.factor_name == factor_name,
                FactorValue.trade_date == trade_date,
            )
        )
        if codes:
            query = query.where(FactorValue.stock_code.in_(codes))

        result = await session.execute(query)
        records = result.scalars().all()

        return [
            {
                "code": r.stock_code,
                "value": r.factor_value,
                "rank": r.factor_rank,
                "pct": r.factor_pct,
            }
            for r in records
        ]

    async def get_factor_history(self, session: AsyncSession,
                                  factor_name: str,
                                  code: str,
                                  days: int = 60) -> pd.DataFrame:
        """获取因子历史值"""
        result = await session.execute(
            select(FactorValue).where(
                and_(
                    FactorValue.factor_name == factor_name,
                    FactorValue.stock_code == code,
                )
            ).order_by(FactorValue.trade_date.desc()).limit(days)
        )
        records = result.scalars().all()

        if not records:
            return pd.DataFrame()

        return pd.DataFrame([{
            "trade_date": r.trade_date,
            "value": r.factor_value,
            "rank": r.factor_rank,
            "pct": r.factor_pct,
        } for r in reversed(records)])

    async def cleanup_old_values(self, session: AsyncSession,
                                  keep_days: int = 180) -> int:
        """清理过期因子值"""
        cutoff = date.today() - timedelta(days=keep_days)
        result = await session.execute(
            delete(FactorValue).where(FactorValue.trade_date < cutoff)
        )
        await session.commit()
        count = result.rowcount
        logger.info(f"清理过期因子值: {count}条 (保留{keep_days}天)")
        return count


# ========== 因子评估调度器 ==========

class FactorEvaluationScheduler:
    """因子评估调度器 — 管理定时评估任务"""

    def __init__(self):
        self.storage = FactorStorageService()

    async def run_daily_evaluation(self, session: AsyncSession,
                                    trade_date: date = None, *, as_of_at: datetime = None) -> dict:
        """每日因子评估(盘后运行)

        读取存量因子及已成熟正式日K，追加冻结研究评估并生成报告。
        不重算/覆盖FactorValue，不改任何生产权重。
        """
        logger.info(f"📊 因子研究评估开始: {trade_date or as_of_at or '当前截止时点'}")

        # 1. 评估所有因子
        eval_result = await factor_evaluator.evaluate_all_factors(session, trade_date, as_of_at=as_of_at)

        # 2. 检查衰减因子
        decaying = []
        for name, result in eval_result.get("results", {}).items():
            decay = result.get("decay", {})
            if decay.get("is_decaying"):
                decaying.append({
                    "factor": name,
                    "decay_days": decay.get("decay_days", 0),
                    "recent_ic": decay.get("recent_ic_mean", 0),
                })

        # 3. 获取优化后的权重
        weights = eval_result.get("optimized_weights", {})

        # 4. 汇总
        report = {
            "eval_date": eval_result["eval_date"],
            "as_of_at": eval_result["as_of_at"],
            "automatic_weight_update": False,
            "promotion_eligible": False,
            "total_factors": eval_result.get("total_factors", 0),
            "evaluated_factors": sum(row.get("status") == "research_only"
                                     for row in eval_result.get("results", {}).values()),
            "decaying_factors": decaying,
            "decaying_count": len(decaying),
            "optimized_weights": weights,
            "category_summary": eval_result.get("category_summary", {}),
        }

        logger.info(
            f"📊 因子评估完成: {report['evaluated_factors']}个因子, "
            f"{len(decaying)}个衰减"
        )

        return report

    async def compute_and_store_factors(self, session: AsyncSession,
                                         trade_date: date = None,
                                         codes: list[str] = None) -> dict:
        """Compute bounded, explicitly scoped READ-TIME research, never PIT history."""
        from app.factors.market_inputs import (
            factor_sessions, load_factor_market_inputs, validate_code,
        )
        from app.models.stock import StockKline
        from app.factors.computation_evidence import (
            engine_descriptor, frame_material, freeze_stock, append_computation,
        )
        from uuid import uuid4

        at = datetime.now()
        sessions = await factor_sessions(session, trade_date=trade_date, as_of_at=at)
        trade_date = sessions[-1]
        if codes is None:
            result = await session.execute(select(StockKline.code).where(
                StockKline.trade_date == trade_date).distinct()
                .execution_options(autoflush=False))
            codes = list(result.scalars())
        for code in codes:
            validate_code(code)
        codes = sorted(set(codes))
        # Keep the existing operational bound, but NEVER label a truncated pool
        # as an all-market cross-section. Explicit remaining codes enable resumption.
        selected, deferred = codes[:100], codes[100:]
        engine = FactorEngine()
        descriptor = engine_descriptor(engine)
        all_results, inputs, failures, frozen_stocks = {}, {}, {}, {}
        for code in selected:
            try:
                df, evidence = await load_factor_market_inputs(
                    session, code=code, trade_date=trade_date, as_of_at=at)
                material = frame_material(df)
                results = await engine.compute_single(code, trade_date, df)
                if (frame_material(df) != material or engine_descriptor(engine) != descriptor):
                    raise ValueError("factor input or loaded implementation changed during calculation")
                frozen_stocks[code] = freeze_stock(
                    code=code, trade_date=trade_date, read_started_at=at,
                    material=material, evidence=evidence, results=results, descriptor=descriptor)
                inputs[code] = evidence
                all_results[code] = {
                    name: {"value": r.value, "rank": r.rank, "pct": r.pct, "confidence": r.confidence}
                    for name, r in results.items()
                }
            except Exception as exc:
                logger.warning(f"因子计算失败 [{code}]: {exc}")
                failures[code] = type(exc).__name__
        # Capture is mandatory for this explicit store operation. Missing migration,
        # serialization/flush/commit failure must never persist untraced legacy values.
        try:
            if engine_descriptor(engine) != descriptor:
                raise ValueError("factor implementation changed during batch")
            capture = await append_computation(
                session, capture_id=uuid4().hex, trade_date=trade_date,
                read_started_at=at, captured_at=datetime.now(), descriptor=descriptor,
                stocks=frozen_stocks, requested_codes=codes, attempted_codes=selected,
                deferred_codes=deferred, failures=failures)
            if all_results:
                saved = await self.storage.save_factor_values(session, trade_date, all_results)
            else:
                await session.commit()
                saved = 0
        except BaseException:
            await session.rollback()
            raise
        return {
            "status": "no_data" if not codes else "partial" if deferred or failures else "ok",
            "trade_date": trade_date.isoformat(), "as_of_at": at.isoformat(),
            "computed_stocks": len(all_results), "errors": len(failures), "saved_values": saved,
            "available_factor_values": sum(
                item["value"] is not None and item["confidence"] > 0
                for values in all_results.values() for item in values.values()),
            "requested_count": len(codes), "attempted_count": len(selected),
            "deferred_codes": deferred, "failures": failures,
            "scope": "bounded_per_stock_research_not_ranked_cross_section",
            "input_evidence": inputs,
            "computation_capture": {**capture, "status": "committed"},
            "point_in_time_verified": False, "automatic_weight_update": False,
            "promotion_eligible": False,
            "storage_basis": "legacy_factor_values_insert_missing_only_not_pit",
        }

    async def get_evaluation_report(self, session: AsyncSession) -> dict:
        """获取最新评估报告"""
        return await factor_evaluator.get_factor_report(session)

    async def get_decaying_factors(self, session: AsyncSession) -> list[dict]:
        """只使用新真实收益协议，不让旧排名自相关驱动衰减诊断。"""
        report = await factor_evaluator.get_factor_report(session)
        return [row for row in report["evaluations"].values() if row["is_decaying"] is True]

    async def get_factor_weights(self, session: AsyncSession) -> dict:
        """获取当前因子权重"""
        # 获取最新评估
        report = await self.get_evaluation_report(session)

        # 获取各分类默认权重
        from app.factors.evaluator import FactorWeightOptimizer
        optimizer = FactorWeightOptimizer()

        return {
            "default_weights": {
                cat.value: weight for cat, weight in optimizer.DEFAULT_WEIGHTS.items()
            },
            "factor_count": report.get("total_factors", 0),
            "by_category": report.get("by_category", {}),
            "decaying": report.get("decaying_factors", []),
        }

    async def force_recompute(self, session: AsyncSession,
                               factor_name: str = None,
                               trade_date: date = None) -> dict:
        """强制重新计算因子评估"""
        if trade_date is None:
            trade_date = date.today()

        if factor_name:
            result = await factor_evaluator.evaluate_factor(
                session, factor_name, trade_date
            )
            return {
                "status": "ok",
                "factor_name": factor_name,
                "result": result,
            }
        else:
            result = await factor_evaluator.evaluate_all_factors(
                session, trade_date
            )
            return {
                "status": "ok",
                "result": result,
            }


# 全局
factor_storage = FactorStorageService()
factor_eval_scheduler = FactorEvaluationScheduler()
