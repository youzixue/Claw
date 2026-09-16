"""因子评估模块 — IC/IR/衰减检测/因子权重优化

核心能力:
- IC分析(信息系数): 因子值与下期收益的相关性
- IR分析(信息比率): IC均值/IC标准差
- 因子衰减检测: 连续IC<0的天数
- 因子权重优化: 基于IC的历史表现调整权重
- 因子有效性排名
"""

from datetime import date, datetime, time, timedelta
from typing import Optional
from collections import Counter
import hashlib
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factor import FactorValue, FactorEvaluation, FactorEvaluationRun
from app.models.stock import StockKline
from app.models.governance import TradeCalendarModel
from app.factors.base import FactorRegistry, FactorCategory, _finite_number
from app.promotion.outcome_evidence import (
    next_recorded_trade_day, formal_outcome_bar_error, _is_completed_outcome_date,
)

IC_PROTOCOL = "spearman_next_session_formal_close_v1"
IC_LIMITATIONS = [
    "FactorValue无首次计算/可用时间与版本，历史因子仅研究，不能作为PIT晋级证据",
    "收益为正式日K下一交易日收盘比值；不是可成交收益，不含费用/滑点/T+1/涨跌停约束",
    "只评价存量因子覆盖池，缺失/停牌/价格链断点可能造成选择偏差，非全市场绩效",
]


# ========== IC分析 ==========

class ICAnalyzer:
    """信息系数(IC)分析器

    IC = rank_corr(因子值, 下期收益)
    IC > 0.03 = 有效因子
    IC > 0.05 = 强有效因子
    """

    IC_EFFECTIVE_THRESHOLD = 0.03
    IC_STRONG_THRESHOLD = 0.05
    IR_EFFECTIVE_THRESHOLD = 0.5

    def calc_ic_series(self, factor_values: pd.Series,
                        forward_returns: pd.Series) -> pd.Series:
        """计算IC序列(Spearman秩相关)"""
        from scipy.stats import spearmanr

        # 按日期分组计算IC
        if len(factor_values) != len(forward_returns):
            return pd.Series(dtype=float)

        # Series必须按同一资产索引对齐；不按位置悄悄配对不同股票。
        if (not factor_values.index.is_unique or not forward_returns.index.is_unique
                or not factor_values.index.equals(forward_returns.index)):
            return pd.Series(dtype=float)
        x = factor_values.map(_finite_number)
        y = forward_returns.map(_finite_number)
        valid = x.notna() & y.notna()
        if valid.sum() < 10 or x[valid].nunique() < 2 or y[valid].nunique() < 2:
            return pd.Series(dtype=float)
        corr, _ = spearmanr(x[valid], y[valid])
        return pd.Series([float(corr)]) if np.isfinite(corr) else pd.Series(dtype=float)

    def calc_ic_dataframe(self, df: pd.DataFrame,
                           factor_col: str,
                           return_col: str,
                           date_col: str = "trade_date") -> pd.DataFrame:
        """按日期计算IC序列"""
        from scipy.stats import spearmanr

        results = []
        for trade_date, group in df.groupby(date_col):
            x = group[factor_col].map(_finite_number)
            y = group[return_col].map(_finite_number)
            valid = x.notna() & y.notna()
            if valid.sum() < 10 or x[valid].nunique() < 2 or y[valid].nunique() < 2:
                continue
            corr, pvalue = spearmanr(x[valid], y[valid])
            if not np.isfinite(corr):
                continue
            results.append({
                "trade_date": trade_date,
                "ic": float(corr),
                "p_value": _finite_number(pvalue),
                "sample_size": int(valid.sum()),
            })

        return pd.DataFrame(results, columns=["trade_date", "ic", "p_value", "sample_size"])

    def summarize_ic(self, ic_series: pd.Series) -> dict:
        """汇总IC指标"""
        ic_series = ic_series.map(_finite_number).dropna()
        if ic_series.empty:
            return {}

        ic_mean = float(ic_series.mean())
        ic_std = _finite_number(ic_series.std()) if len(ic_series) > 1 else None
        ir = ic_mean / ic_std if ic_std is not None and ic_std > 0 else None

        # IC>0的比例
        ic_positive_rate = (ic_series > 0).mean()

        # 显著性(IC>0.03)
        ic_significant_rate = (abs(ic_series) > self.IC_EFFECTIVE_THRESHOLD).mean()

        return {
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4) if ic_std is not None else None,
            "ir": round(ir, 4) if ir is not None else None,
            "ic_positive_rate": round(ic_positive_rate, 4),
            "ic_significant_rate": round(ic_significant_rate, 4),
            "sample_count": len(ic_series),
        }


# ========== 因子衰减检测 ==========

class FactorDecayDetector:
    """因子衰减检测器 — 发现因子失效信号"""

    DECAY_WINDOW = 20          # 检测窗口(天)
    DECAY_IC_THRESHOLD = 0     # IC低于此阈值视为衰减
    DECAY_STREAK_THRESHOLD = 5 # 连续衰减天数阈值

    def detect_decay(self, ic_series: pd.Series) -> dict:
        """检测因子是否正在衰减；不足窗口不标为正常。"""
        if (len(ic_series) < self.DECAY_WINDOW
                or ic_series.map(_finite_number).isna().any()):
            return {"is_decaying": None, "decay_days": None, "status": "insufficient_data"}

        recent_ic = ic_series.iloc[-self.DECAY_WINDOW:]

        # 连续IC<0的天数
        decay_streak = 0
        for ic in recent_ic.iloc[::-1]:
            if ic < self.DECAY_IC_THRESHOLD:
                decay_streak += 1
            else:
                break

        # 近期IC均值 vs 全局IC均值
        recent_mean = recent_ic.mean()
        overall_mean = ic_series.mean()
        performance_drop = recent_mean < overall_mean * 0.5

        is_decaying = (
            decay_streak >= self.DECAY_STREAK_THRESHOLD
            or (performance_drop and recent_mean < 0)
        )

        return {
            "is_decaying": bool(is_decaying),
            "decay_days": decay_streak,
            "recent_ic_mean": round(recent_mean, 4),
            "overall_ic_mean": round(overall_mean, 4),
            "performance_drop": bool(performance_drop),
        }


# ========== 因子权重优化 ==========

class FactorWeightOptimizer:
    """因子权重优化器 — 基于IC表现调整因子权重"""

    # 各分类默认权重
    DEFAULT_WEIGHTS = {
        FactorCategory.TECHNICAL: 15,
        FactorCategory.FUND_FLOW: 30,
        FactorCategory.SENTIMENT: 20,
        FactorCategory.SECTOR: 25,
        FactorCategory.FUNDAMENTAL: 10,
        FactorCategory.BREAKOUT: 20,
        FactorCategory.PROMOTION: 15,
        FactorCategory.LIFECYCLE: 15,
        FactorCategory.NEWS: 10,
        FactorCategory.MARGIN: 10,
    }

    def optimize_weights(self, ic_summary: dict[str, dict],
                          method: str = "ic_weighted") -> dict[str, float]:
        """优化因子权重

        Args:
            ic_summary: {factor_name: {ic_mean, ir, ...}}
            method: ic_weighted(按IC加权) / equal(等权) / ir_weighted(按IR加权)

        Returns:
            {factor_name: weight}
        """
        if method == "equal":
            n = len(ic_summary)
            return {name: 1.0 / n for name in ic_summary} if n > 0 else {}

        # IC加权
        weights = {}
        total_ic = 0

        for name, metrics in ic_summary.items():
            if method == "ic_weighted":
                w = max(metrics.get("ic_mean", 0), 0)  # 负IC的因子权重=0
            elif method == "ir_weighted":
                w = max(metrics.get("ir", 0), 0)
            else:
                w = 1.0

            weights[name] = w
            total_ic += w

        # 归一化
        if total_ic > 0:
            weights = {k: v / total_ic for k, v in weights.items()}

        return weights

    def get_category_weights(self, ic_by_category: dict[str, dict]) -> dict[str, float]:
        """按分类调整权重"""
        weights = {}
        for cat_name, metrics in ic_by_category.items():
            base = self.DEFAULT_WEIGHTS.get(FactorCategory(cat_name), 10)
            ic_mean = metrics.get("ic_mean", 0)

            # IC正向→加权, IC负向→减权
            adjustment = ic_mean * 100  # IC=0.05 → 加5
            adjusted = max(base * 0.3, base + adjustment)  # 最低保留30%
            weights[cat_name] = adjusted

        # 归一化
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total * 100 for k, v in weights.items()}

        return weights


# ========== 因子评估服务 ==========

def _evaluation_cutoff(eval_date=None, as_of_at=None):
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    cutoff = as_of_at or now
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if cutoff > now:
        raise ValueError("不允许未来评估时点")
    if eval_date is not None:
        if eval_date > cutoff.date():
            raise ValueError("评估日期不得晚于证据截止日")
        cutoff = min(cutoff, datetime.combine(eval_date, time.max))
    return cutoff


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evaluate_ic_material(material, analyzer, decay_detector):
    """纯函数：只用冻结材料配对真实下一交易日收益，不读排名或后续可变数据库。"""
    cutoff = datetime.fromisoformat(material["as_of_at"])
    through = date.fromisoformat(material["through_date"])
    calendar = {date.fromisoformat(d): state for d, state in material["calendar"]}
    bars = {(row["stock_code"], row["trade_date"]): SimpleNamespace(**row) for row in material["bars"]}
    exclusions = Counter()
    pairs = []
    dates = sorted({row["trade_date"] for row in material["factors"]})
    for row in material["factors"]:
        value = _finite_number(row["value"])
        if value is None:
            exclusions["missing_factor_value"] += 1
            continue
        trade_day = date.fromisoformat(row["trade_date"])
        next_day, reason = next_recorded_trade_day(trade_day, calendar, through=through)
        if reason:
            exclusions[reason] += 1
            continue
        if not _is_completed_outcome_date(next_day, now=cutoff):
            exclusions["outcome_not_matured"] += 1
            continue
        before = bars.get((row["stock_code"], row["trade_date"]))
        after = bars.get((row["stock_code"], next_day.isoformat()))
        reason = formal_outcome_bar_error(before, after)
        if reason:
            exclusions[reason] += 1
            continue
        forward_return = _finite_number(after.close / before.close - 1)
        if forward_return is None:
            exclusions["invalid_return"] += 1
            continue
        pairs.append({"trade_date": row["trade_date"], "stock_code": row["stock_code"],
                      "factor_value": value, "forward_return": forward_return,
                      "outcome_date": next_day.isoformat()})

    frame = pd.DataFrame(pairs, columns=["trade_date", "stock_code", "factor_value", "forward_return", "outcome_date"])
    ic_frame = analyzer.calc_ic_dataframe(frame, "factor_value", "forward_return")
    daily_map = {row["trade_date"]: row for row in ic_frame.to_dict("records")}
    pair_counts = Counter(row["trade_date"] for row in pairs)
    daily = []
    for day in dates:
        record = daily_map.get(day)
        _, calendar_reason = next_recorded_trade_day(date.fromisoformat(day), calendar, through=through)
        daily.append(record or {"trade_date": day, "ic": None, "p_value": None,
                                "sample_size": pair_counts[day],
                                "status": calendar_reason or "insufficient_or_constant_cross_section"})
    ic_series = pd.Series([row["ic"] for row in daily], dtype=float)
    # 尚未到下一交易日的尾部不属于衰减样本；已到但缺证的日期仍保留空值阻断连续性。
    decay_series = pd.Series([row["ic"] for row in daily
                              if row.get("status") != "outcome_session_not_available"], dtype=float)
    summary = analyzer.summarize_ic(ic_series)
    # 衰减使用方向调整后的IC，但不能跨过缺数据日期伪造连续改善/恶化。
    direction = material["direction"]
    return {
        "factor_name": material["factor_name"], "eval_date": cutoff.date().isoformat(),
        "as_of_at": cutoff.isoformat(), "through_date": through.isoformat(),
        "protocol_version": IC_PROTOCOL, "status": "research_only" if summary else "insufficient_data",
        "horizon_trade_days": 1, "return_definition": "next_formal_close / formal_close - 1",
        "direction": direction, "ic_summary": summary,
        "direction_adjusted_ic_mean": round(summary["ic_mean"] * direction, 4) if summary else None,
        "decay": decay_detector.detect_decay(decay_series * direction),
        "daily_ic": daily, "input_count": len(material["factors"]), "paired_count": len(pairs),
        "excluded_count": sum(exclusions.values()), "exclusions": dict(exclusions),
        "factor_availability": "legacy_unknown", "price_availability": "read_time_formal_close",
        "point_in_time_verified": False, "promotion_eligible": False,
        "automatic_weight_update": False, "limitations": list(IC_LIMITATIONS),
    }


class FactorEvaluationService:
    """真实收益IC的研究评估；读报告无副作用，新运行追加保存并隔离旧口径。"""

    def __init__(self):
        self.ic_analyzer = ICAnalyzer()
        self.decay_detector = FactorDecayDetector()
        self.weight_optimizer = FactorWeightOptimizer()

    async def evaluate_factor(self, session: AsyncSession, factor_name: str,
                              eval_date: date = None, *, as_of_at: datetime = None,
                              persist: bool = True) -> dict:
        factor = FactorRegistry.get(factor_name)
        if factor is None:
            raise ValueError(f"未知因子: {factor_name}")
        cutoff = _evaluation_cutoff(eval_date, as_of_at)
        through = cutoff.date() if _is_completed_outcome_date(cutoff.date(), now=cutoff) else cutoff.date() - timedelta(days=1)
        start = through - timedelta(days=90)
        values = list((await session.scalars(select(FactorValue).where(
            FactorValue.factor_name == factor_name,
            FactorValue.trade_date >= start, FactorValue.trade_date <= through,
        ).order_by(FactorValue.trade_date, FactorValue.stock_code))).all())
        codes = sorted({row.stock_code for row in values})
        # 不调用可联网/自动写日历的服务；缺失日历只能降低标签覆盖。
        calendars = (await session.execute(select(
            TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day,
        ).where(TradeCalendarModel.trade_date >= start,
                TradeCalendarModel.trade_date <= through).order_by(TradeCalendarModel.trade_date))).all()
        bars = []
        if codes:
            bars = list((await session.scalars(select(StockKline).where(
                StockKline.code.in_(codes), StockKline.trade_date >= start,
                StockKline.trade_date <= through,
            ).order_by(StockKline.trade_date, StockKline.code))).all())
        material = {
            "protocol_version": IC_PROTOCOL, "factor_name": factor_name, "direction": factor.direction,
            "as_of_at": cutoff.isoformat(), "through_date": through.isoformat(),
            "factor_source": "factor_values_legacy_unknown", "price_source": "stock_kline",
            "calendar": [[row[0].isoformat(), bool(row[1])] for row in calendars],
            "factors": [{"id": row.id, "stock_code": row.stock_code, "trade_date": row.trade_date.isoformat(),
                         "value": _finite_number(row.factor_value)} for row in values],
            "bars": [{"stock_code": row.code, "trade_date": row.trade_date.isoformat(),
                      "close": _finite_number(row.close), "prev_close": _finite_number(row.prev_close),
                      "volume": _finite_number(row.volume), "source": row.source} for row in bars],
        }
        material_json = _canonical_json(material)
        fingerprint = hashlib.sha256(material_json.encode("utf-8")).hexdigest()
        result = evaluate_ic_material(material, self.ic_analyzer, self.decay_detector)
        result["input_hash"] = fingerprint
        result["persisted"] = False
        if not persist:
            return result
        identity = (FactorEvaluationRun.factor_name == factor_name,
                    FactorEvaluationRun.protocol_version == IC_PROTOCOL,
                    FactorEvaluationRun.input_hash == fingerprint)
        existing = await session.scalar(select(FactorEvaluationRun).where(*identity))
        if existing is None:
            record = FactorEvaluationRun(
                factor_name=factor_name, eval_date=cutoff.date(), as_of_at=cutoff,
                protocol_version=IC_PROTOCOL, input_hash=fingerprint,
                input_json=material_json, result_json=_canonical_json(result),
            )
            try:
                # 唯一键/嵌套事务防并发重复；不覆盖已有同输入结果。
                async with session.begin_nested():
                    session.add(record)
                    await session.flush()
                await session.commit()
                existing = record
            except IntegrityError:
                existing = await session.scalar(select(FactorEvaluationRun).where(*identity))
                if existing is None:
                    raise
        saved = json.loads(existing.result_json)
        return {**saved, "run_id": existing.id, "persisted": True}

    async def evaluate_all_factors(self, session: AsyncSession, eval_date: date = None,
                                   *, as_of_at: datetime = None, persist: bool = True) -> dict:
        cutoff = _evaluation_cutoff(eval_date, as_of_at)
        results = {}
        for name in FactorRegistry.all_factors():
            results[name] = await self.evaluate_factor(session, name, as_of_at=cutoff, persist=persist)
        return {
            "eval_date": cutoff.date().isoformat(), "as_of_at": cutoff.isoformat(),
            "total_factors": len(results), "results": results, "optimized_weights": {},
            "automatic_weight_update": False, "promotion_eligible": False,
            "category_summary": FactorRegistry.summary(),
        }

    async def get_evaluations(self, session: AsyncSession, eval_date: date = None,
                              factor_name: str = None, *, as_of_at: datetime = None) -> dict:
        cutoff = _evaluation_cutoff(eval_date, as_of_at)
        # 报告只读结果叶子，不把大份冻结复算材料一并装入页面查询。
        query = select(FactorEvaluationRun.id, FactorEvaluationRun.factor_name,
                       FactorEvaluationRun.result_json).where(
            FactorEvaluationRun.as_of_at <= cutoff,
            FactorEvaluationRun.protocol_version == IC_PROTOCOL,
        ).order_by(FactorEvaluationRun.as_of_at.desc(), FactorEvaluationRun.id.desc())
        legacy_query = select(FactorEvaluation).where(FactorEvaluation.eval_date <= cutoff.date()).order_by(
            FactorEvaluation.eval_date.desc(), FactorEvaluation.id.desc())
        if factor_name:
            query = query.where(FactorEvaluationRun.factor_name == factor_name)
            legacy_query = legacy_query.where(FactorEvaluation.factor_name == factor_name)
        results = {}
        for row in (await session.execute(query)).all():
            if row.factor_name not in results:
                results[row.factor_name] = {**json.loads(row.result_json), "run_id": row.id, "persisted": True}
        legacy = {}
        for row in (await session.scalars(legacy_query)).all():
            if row.factor_name not in legacy:
                # 保留旧原始数值但不再将它放在真实IC/IR字段中展示。
                legacy[row.factor_name] = {
                    "factor_name": row.factor_name, "eval_date": row.eval_date.isoformat(),
                    "protocol_version": "legacy_rank_autocorrelation", "status": "legacy_not_ic",
                    "ic_summary": {}, "decay": {"is_decaying": None, "decay_days": None},
                    "legacy_values": {"rank_autocorrelation_mean": _finite_number(row.ic_mean),
                                      "rank_autocorrelation_std": _finite_number(row.ic_std)},
                    "promotion_eligible": False, "limitations": ["旧排名自相关不是收益IC，旧记录仅保留审计"],
                }
        for name, row in legacy.items():
            results.setdefault(name, row)
        return {"eval_date": cutoff.date().isoformat(), "results": results,
                "total_factors": FactorRegistry.count(), "optimized_weights": {},
                "automatic_weight_update": False, "promotion_eligible": False,
                "category_summary": FactorRegistry.summary(), "legacy_record_count": len(legacy)}

    async def get_factor_report(self, session: AsyncSession) -> dict:
        report = await self.get_evaluations(session)
        evaluations = {}
        for name, row in report["results"].items():
            summary = row.get("ic_summary", {})
            evaluations[name] = {
                "factor_name": name, "eval_date": row["eval_date"],
                "ic_mean": summary.get("ic_mean"), "ic_std": summary.get("ic_std"),
                "ir": summary.get("ir"), "win_rate": summary.get("ic_positive_rate"),
                "is_decaying": row.get("decay", {}).get("is_decaying"),
                "decay_days": row.get("decay", {}).get("decay_days"),
                "protocol_version": row["protocol_version"], "status": row["status"],
                "sample_count": summary.get("sample_count", 0), "promotion_eligible": False,
            }
        return {"total_factors": FactorRegistry.count(), "by_category": FactorRegistry.summary(),
                "evaluations": evaluations, "legacy_record_count": report["legacy_record_count"],
                "automatic_weight_update": False, "promotion_eligible": False,
                "decaying_factors": [name for name, row in evaluations.items() if row["is_decaying"] is True]}


# 全局
factor_evaluator = FactorEvaluationService()
