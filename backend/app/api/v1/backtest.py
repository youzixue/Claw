"""回测引擎 API — 信号回测/策略回测/绩效分析"""

import asyncio
import json
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

router = APIRouter()

_STATUS_CACHE_TTL_SECONDS = 60.0
_STATUS_CACHE: dict[str, object] = {"expires_at": 0.0, "data": None, "scope": None}
_STATUS_CACHE_LOCK = asyncio.Lock()


def _backtest_status_cache_scope(db: AsyncSession) -> int:
    """Keep the process cache isolated per engine (important for tests and multi-db workers)."""
    try:
        bind = db.get_bind()
    except (AttributeError, TypeError):
        bind = getattr(db, "bind", None)
    return id(bind)


# ========== 请求模型 ==========

class SignalBacktestRequest(BaseModel):
    """信号回测请求"""
    signal_ids: Optional[list[str]] = None  # 指定信号ID
    min_score: Optional[int] = 50           # 最低评分过滤
    start_date: Optional[str] = None        # YYYY-MM-DD
    end_date: Optional[str] = None
    holding_days: list[int] = Field(default_factory=lambda: [1, 3, 5, 10])


class StrategyBacktestRequest(BaseModel):
    """策略回测请求"""
    strategy_id: str = "signal_score_rank"      # 策略ID
    start_date: str                         # YYYY-MM-DD
    end_date: str
    initial_capital: float = 1_000_000
    commission_rate: float = 0.0003
    slippage_pct: float = 0.1
    volume_limit_pct: float = 10.0
    avoid_limit_up_down: bool = True
    stop_loss_pct: float = 7.0
    take_profit_pct: float = 30.0
    max_positions: int = 5
    min_score_to_buy: int = 70
    benchmark_code: str = "000001"
    validation_mode: str = "split"             # none/split/walk_forward
    train_ratio: float = Field(default=0.7, ge=0.2, le=0.9)
    walk_forward_windows: int = Field(default=3, ge=2, le=8)


class SignalBackfillRequest(BaseModel):
    """研究样本生成请求"""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    max_codes: int = Field(default=200, ge=1, le=1000)
    min_score: int = Field(default=70, ge=0, le=100)
    max_signals: int = Field(default=1000, ge=1, le=5000)


def _parse_date(value: Optional[str], field_name: str, *, required: bool = False) -> Optional[date]:
    """Parse a YYYY-MM-DD date and raise a client-friendly API error."""
    if not value:
        if required:
            raise HTTPException(status_code=400, detail=f"{field_name} 不能为空")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是 YYYY-MM-DD 格式") from exc


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _strategy_config(req: StrategyBacktestRequest) -> dict:
    return {
        "strategy_id": req.strategy_id,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "initial_capital": req.initial_capital,
        "commission_rate": req.commission_rate,
        "slippage_pct": req.slippage_pct,
        "volume_limit_pct": req.volume_limit_pct,
        "avoid_limit_up_down": req.avoid_limit_up_down,
        "stop_loss_pct": req.stop_loss_pct,
        "take_profit_pct": req.take_profit_pct,
        "max_positions": req.max_positions,
        "min_score_to_buy": req.min_score_to_buy,
        "benchmark_code": req.benchmark_code,
        "validation_mode": req.validation_mode,
        "train_ratio": req.train_ratio,
        "walk_forward_windows": req.walk_forward_windows,
    }


def _signal_config(req: SignalBacktestRequest) -> dict:
    return {
        "signal_ids": req.signal_ids,
        "min_score": req.min_score,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "holding_days": req.holding_days,
    }


def _json_loads(value: Optional[str]) -> dict:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _date_to_str(value) -> Optional[str]:
    return value.isoformat() if value else None


def _run_to_summary(run) -> dict:
    config = _json_loads(run.config_json)
    metrics = _json_loads(run.metrics_json)
    return {
        "run_id": run.run_id,
        "run_type": run.run_type,
        "status": run.status,
        "strategy_id": config.get("strategy_id"),
        "start_date": _date_to_str(run.start_date),
        "end_date": _date_to_str(run.end_date),
        "initial_capital": run.initial_capital or config.get("initial_capital"),
        "final_capital": run.final_capital,
        "total_return_pct": run.total_return_pct or metrics.get("total_return_pct"),
        "max_drawdown_pct": run.max_drawdown_pct or metrics.get("max_drawdown_pct"),
        "benchmark_return_pct": metrics.get("benchmark", {}).get("total_return_pct"),
        "excess_return_pct": metrics.get("benchmark", {}).get("excess_return_pct"),
        "win_rate_pct": run.win_rate_pct or metrics.get("win_rate_pct"),
        "sharpe_ratio": run.sharpe_ratio or metrics.get("sharpe_ratio"),
        "total_trades": run.total_trades or 0,
        "message": run.message,
        "created_at": _date_to_str(run.created_at),
    }


async def _get_backtest_data_status(db: AsyncSession) -> dict:
    from app.models.backtest import BacktestRun
    from app.models.factor import FactorValue
    from app.models.signal import SignalPerformance
    from app.models.stock import StockDaily, StockKline
    from sqlalchemy import func, select

    cache_scope = _backtest_status_cache_scope(db)
    now = _time.monotonic()
    cached = _STATUS_CACHE.get("data")
    if (
        cached is not None
        and _STATUS_CACHE.get("scope") == cache_scope
        and float(_STATUS_CACHE.get("expires_at", 0.0)) > now
    ):
        return cached

    async with _STATUS_CACHE_LOCK:
        now = _time.monotonic()
        cached = _STATUS_CACHE.get("data")
        if (
            cached is not None
            and _STATUS_CACHE.get("scope") == cache_scope
            and float(_STATUS_CACHE.get("expires_at", 0.0)) > now
        ):
            return cached

        signal_row = (await db.execute(select(
            func.count(SignalPerformance.signal_id),
            func.min(SignalPerformance.signal_time),
            func.max(SignalPerformance.signal_time),
        ))).one()
        daily_row = (await db.execute(select(
            func.count(StockDaily.id),
            func.count(func.distinct(StockDaily.trade_date)),
            func.min(StockDaily.trade_date),
            func.max(StockDaily.trade_date),
        ))).one()
        kline_row = (await db.execute(select(
            func.count(StockKline.id),
            func.count(func.distinct(StockKline.trade_date)),
            func.min(StockKline.trade_date),
            func.max(StockKline.trade_date),
        ))).one()
        factor_row = (await db.execute(select(
            func.count(FactorValue.id),
            func.count(func.distinct(FactorValue.factor_name)),
            func.min(FactorValue.trade_date),
            func.max(FactorValue.trade_date),
        ))).one()
        run_row = (await db.execute(select(
            func.count(BacktestRun.run_id),
            func.max(BacktestRun.created_at),
        ))).one()

        signal_count = signal_row[0] or 0
        stock_daily_count = daily_row[0] or 0
        stock_kline_count = kline_row[0] or 0
        factor_value_count = factor_row[0] or 0
        price_count = stock_kline_count or stock_daily_count
        price_trade_days = (kline_row[1] or 0) if stock_kline_count else (daily_row[1] or 0)
        price_min_date = kline_row[2] if stock_kline_count else daily_row[2]
        price_max_date = kline_row[3] if stock_kline_count else daily_row[3]
        price_source = "stock_kline" if stock_kline_count else ("stock_daily" if stock_daily_count else None)
        can_use_signal = signal_count > 0 and price_count > 0
        warnings = []
        if signal_count == 0:
            warnings.append("暂无研究样本，信号回测和当前策略回测无法产生有效样本")
        if price_count == 0:
            warnings.append("暂无日线行情，回测无法计算买卖价格与收益")
        if factor_value_count == 0:
            warnings.append("暂无因子快照，因子排名策略暂不可用")

        data = {
            "signal_count": signal_count,
            "signal_min_time": _date_to_str(signal_row[1]),
            "signal_max_time": _date_to_str(signal_row[2]),
            "stock_daily_count": stock_daily_count,
            "stock_daily_trade_days": daily_row[1] or 0,
            "stock_daily_min_date": _date_to_str(daily_row[2]),
            "stock_daily_max_date": _date_to_str(daily_row[3]),
            "stock_kline_count": stock_kline_count,
            "stock_kline_trade_days": kline_row[1] or 0,
            "stock_kline_min_date": _date_to_str(kline_row[2]),
            "stock_kline_max_date": _date_to_str(kline_row[3]),
            "price_count": price_count,
            "price_trade_days": price_trade_days,
            "price_min_date": _date_to_str(price_min_date),
            "price_max_date": _date_to_str(price_max_date),
            "price_source": price_source,
            "factor_value_count": factor_value_count,
            "factor_name_count": factor_row[1] or 0,
            "factor_min_date": _date_to_str(factor_row[2]),
            "factor_max_date": _date_to_str(factor_row[3]),
            "run_count": run_row[0] or 0,
            "latest_run_at": _date_to_str(run_row[1]),
            "can_signal_backtest": can_use_signal,
            "can_strategy_backtest": can_use_signal,
            "warnings": warnings,
        }
        _STATUS_CACHE["data"] = data
        _STATUS_CACHE["scope"] = cache_scope
        _STATUS_CACHE["expires_at"] = _time.monotonic() + _STATUS_CACHE_TTL_SECONDS
        return data


def _strategy_catalog(status: dict) -> list[dict]:
    can_signal = status["can_strategy_backtest"]
    can_factor = status["factor_value_count"] > 0 and status["price_count"] > 0
    return [
        {
            "id": "signal_score_rank",
            "name": "信号评分轮动",
            "description": "用研究样本评分选股，按最低评分、最大持仓、止损止盈执行资金曲线回测",
            "enabled": can_signal,
            "default": True,
            "disabled_reason": None if can_signal else "需要研究样本和日线行情",
        },
        {
            "id": "factor_top_rank",
            "name": "因子排名策略",
            "description": "按因子截面排名选股，目前先展示入口，执行逻辑待接入因子组合",
            "enabled": False,
            "default": False,
            "disabled_reason": "执行逻辑待接入" if can_factor else "需要因子快照和日线行情",
        },
        {
            "id": "limit_up_follow",
            "name": "涨停接力策略",
            "description": "围绕涨停池、连板高度和次日计划做验证，目前待接入交易规则",
            "enabled": False,
            "default": False,
            "disabled_reason": "执行规则尚未接入",
        },
    ]


async def _load_price_data(db: AsyncSession, codes: list[str], start: Optional[date] = None, end: Optional[date] = None) -> dict:
    """Load OHLC data for backtests, preferring the richer stock_kline table."""
    import pandas as pd
    from app.models.stock import StockDaily, StockKline
    from sqlalchemy import select

    price_data = {}
    for code in codes:
        for model in (StockKline, StockDaily):
            query = select(model).where(model.code == code)
            if start:
                query = query.where(model.trade_date >= start)
            if end:
                query = query.where(model.trade_date <= end)
            result = await db.execute(query.order_by(model.trade_date))
            records = result.scalars().all()
            if records:
                price_data[code] = pd.DataFrame([{
                    "trade_date": r.trade_date,
                    "open": r.open,
                    "high": r.high,
                    "low": r.low,
                    "close": r.close,
                    "volume": getattr(r, "volume", None),
                    "change_pct": getattr(r, "change_pct", None),
                } for r in records])
                break
    return price_data


def _calc_simple_return_curve(records: list, initial_value: float) -> list[dict]:
    valid = [r for r in records if r.close]
    if not valid:
        return []
    base = valid[0].close
    return [
        {
            "date": str(r.trade_date),
            "nav": round(r.close / base, 6) if base else 1,
            "total_value": round(initial_value * r.close / base, 2) if base else initial_value,
        }
        for r in valid
    ]


async def _calc_benchmark(db: AsyncSession, benchmark_code: str, start: date, end: date, initial_capital: float) -> dict:
    from app.models.stock import StockKline, StockDaily
    from sqlalchemy import select
    import numpy as np

    records = []
    for model in (StockKline, StockDaily):
        result = await db.execute(
            select(model)
            .where(model.code == benchmark_code)
            .where(model.trade_date >= start)
            .where(model.trade_date <= end)
            .order_by(model.trade_date)
        )
        records = result.scalars().all()
        if records:
            break

    curve = _calc_simple_return_curve(records, initial_capital)
    if not curve:
        return {"code": benchmark_code, "nav_curve": [], "total_return_pct": None, "max_drawdown_pct": None}

    values = np.array([item["total_value"] for item in curve], dtype=float)
    peak = np.maximum.accumulate(values)
    drawdown = np.divide(values - peak, peak, out=np.zeros_like(values), where=peak > 0) * 100
    total_return = (values[-1] / initial_capital - 1) * 100 if initial_capital else 0
    return {
        "code": benchmark_code,
        "nav_curve": curve,
        "total_return_pct": round(float(total_return), 2),
        "max_drawdown_pct": round(float(drawdown.min()), 2),
    }


async def _calc_validation_report(
    db: AsyncSession,
    req: StrategyBacktestRequest,
    backtester_cls,
    config_cls,
    daily_scores,
    price_data,
    start: date,
    end: date,
) -> dict:
    mode = req.validation_mode
    if mode not in {"split", "walk_forward"}:
        return {"mode": "none", "windows": []}

    dates = sorted(daily_scores["trade_date"].unique())
    if len(dates) < 4:
        return {"mode": mode, "windows": []}

    windows = []
    if mode == "split":
        split_idx = max(1, min(len(dates) - 1, int(len(dates) * req.train_ratio)))
        ranges = [
            ("in_sample", dates[0], dates[split_idx - 1]),
            ("out_of_sample", dates[split_idx], dates[-1]),
        ]
    else:
        window_size = max(2, len(dates) // req.walk_forward_windows)
        ranges = []
        for i in range(req.walk_forward_windows):
            left = i * window_size
            right = len(dates) - 1 if i == req.walk_forward_windows - 1 else min(len(dates) - 1, (i + 1) * window_size - 1)
            if left < len(dates) and left <= right:
                ranges.append((f"wf_{i + 1}", dates[left], dates[right]))

    for label, left, right in ranges:
        sub_scores = daily_scores[
            (daily_scores["trade_date"] >= left) & (daily_scores["trade_date"] <= right)
        ]
        if sub_scores.empty:
            continue
        config = config_cls(
            initial_capital=req.initial_capital,
            commission_rate=req.commission_rate,
            slippage_pct=req.slippage_pct,
            volume_limit_pct=req.volume_limit_pct,
            avoid_limit_up_down=req.avoid_limit_up_down,
            stop_loss_pct=req.stop_loss_pct,
            take_profit_pct=req.take_profit_pct,
            max_positions=req.max_positions,
            min_score_to_buy=req.min_score_to_buy,
        )
        result = await backtester_cls(config).backtest_strategy(sub_scores, price_data, left, right)
        bench = await _calc_benchmark(db, req.benchmark_code, left, right, req.initial_capital)
        metrics = result.get("metrics", {})
        windows.append({
            "label": label,
            "start_date": str(left),
            "end_date": str(right),
            "total_return_pct": metrics.get("total_return_pct", 0),
            "max_drawdown_pct": metrics.get("max_drawdown_pct", 0),
            "sharpe_ratio": metrics.get("sharpe_ratio", 0),
            "win_rate_pct": metrics.get("win_rate_pct", 0),
            "total_trades": metrics.get("total_trades", 0),
            "benchmark_return_pct": bench.get("total_return_pct"),
            "excess_return_pct": (
                round(metrics.get("total_return_pct", 0) - bench["total_return_pct"], 2)
                if bench.get("total_return_pct") is not None else None
            ),
        })

    return {"mode": mode, "windows": windows}


def _calc_forward_return(records: list, idx: int, days: int) -> Optional[float]:
    if idx + days >= len(records):
        return None
    base = records[idx].close or records[idx].open
    future = records[idx + days].close
    if not base or future is None:
        return None
    return round((future - base) / base * 100, 4)


def _calc_max_return(records: list, idx: int, horizon: int = 10) -> tuple[Optional[float], Optional[float]]:
    base = records[idx].close or records[idx].open
    if not base:
        return None, None
    window = records[idx + 1:idx + horizon + 1]
    if not window:
        return None, None
    highs = [item.high for item in window if item.high is not None]
    lows = [item.low for item in window if item.low is not None]
    max_return = round((max(highs) - base) / base * 100, 4) if highs else None
    max_drawdown = round((min(lows) - base) / base * 100, 4) if lows else None
    return max_return, max_drawdown


def _score_momentum_signal(records: list, idx: int) -> tuple[int, Optional[str]]:
    if idx < 20:
        return 0, None
    row = records[idx]
    close = row.close
    prev_close = records[idx - 1].close
    if not close or not prev_close:
        return 0, None

    history = records[idx - 20:idx]
    closes = [item.close for item in history if item.close is not None]
    volumes = [item.volume for item in history if item.volume]
    highs = [item.high for item in history if item.high is not None]
    if len(closes) < 20:
        return 0, None

    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes) / 20
    high20 = max(highs) if highs else close
    change_pct = row.change_pct
    if change_pct is None:
        change_pct = (close - prev_close) / prev_close * 100
    avg_volume = sum(volumes) / len(volumes) if volumes else None
    volume_ratio = (row.volume / avg_volume) if avg_volume and row.volume else 1.0

    trend_ok = close > ma20 and ma5 >= ma20
    breakout_ok = close >= high20 * 0.985
    volume_ok = volume_ratio >= 1.05
    if not trend_ok or change_pct < 1.0 or not (breakout_ok or volume_ok):
        return 0, None

    score = 45
    score += min(max(change_pct, 0), 6) * 4
    score += min(max(volume_ratio - 1, 0), 2) * 8
    if breakout_ok:
        score += 14
    if close > ma5:
        score += 6
    if change_pct > 8:
        score -= 8

    signal_type = "breakout" if breakout_ok else "momentum"
    return min(100, int(round(score))), signal_type


# ========== API ==========

@router.get("/status")
async def get_backtest_status(
    db: AsyncSession = Depends(get_db),
):
    """获取回测所需数据状态."""
    return await _get_backtest_data_status(db)


@router.get("/strategies")
async def get_backtest_strategies(
    db: AsyncSession = Depends(get_db),
):
    """获取可选回测策略."""
    status = await _get_backtest_data_status(db)
    return {"strategies": _strategy_catalog(status)}


@router.get("/runs")
async def get_backtest_runs(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """获取最近回测记录."""
    from app.models.backtest import BacktestRun
    from sqlalchemy import desc, select

    result = await db.execute(
        select(BacktestRun).order_by(desc(BacktestRun.created_at)).limit(limit)
    )
    return {"runs": [_run_to_summary(run) for run in result.scalars().all()]}


@router.get("/candidates")
async def preview_backtest_candidates(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_score: int = Query(70, ge=0, le=100),
    max_positions: int = Query(5, ge=1, le=50),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """预览当前评分规则会选出的候选股."""
    from app.models.signal import SignalPerformance
    from app.models.stock import StockDaily, StockKline, StockSectorMapping, StockTag
    from sqlalchemy import desc, func, or_, select

    start = _parse_date(start_date, "开始日期")
    end = _parse_date(end_date, "结束日期")
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    query = select(func.max(SignalPerformance.signal_time)).where(
        SignalPerformance.signal_score >= min_score
    )
    if start:
        query = query.where(SignalPerformance.signal_time >= datetime.combine(start, time.min))
    if end:
        query = query.where(SignalPerformance.signal_time <= datetime.combine(end, time.max))

    latest_time = (await db.execute(query)).scalar_one_or_none()
    if not latest_time:
        return {
            "signal_date": None,
            "min_score": min_score,
            "candidates": [],
            "rules": {
                "source": "研究样本评分",
                "ranking": "短线形态 + 个股/板块证据 + 成本后收益回撤排序",
                "entry": f"评分 >= {min_score}，形态达标且净收益期望为正",
                "max_positions": max_positions,
                "execution": "策略回测时再应用仓位、滑点、涨跌停和成交量约束",
            },
        }

    signal_day = latest_time.date() if hasattr(latest_time, "date") else latest_time
    pool_limit = min(max(limit * 5, limit), 300)
    result = await db.execute(
        select(SignalPerformance)
        .where(SignalPerformance.signal_time >= datetime.combine(signal_day, time.min))
        .where(SignalPerformance.signal_time <= datetime.combine(signal_day, time.max))
        .where(SignalPerformance.signal_score >= min_score)
        .order_by(desc(SignalPerformance.signal_score), SignalPerformance.stock_code)
        .limit(pool_limit)
    )
    rows = result.scalars().all()
    codes = [row.stock_code for row in rows]
    names = {}
    if codes:
        tag_rows = await db.execute(
            select(StockTag.code, StockTag.name).where(StockTag.code.in_(codes))
        )
        names = {code: name for code, name in tag_rows.all() if name}

    history_result = await db.execute(
        select(SignalPerformance)
        .where(SignalPerformance.signal_time < datetime.combine(signal_day, time.min))
        .where(SignalPerformance.signal_score >= min_score)
        .where(or_(
            SignalPerformance.return_1d.is_not(None),
            SignalPerformance.return_3d.is_not(None),
            SignalPerformance.return_5d.is_not(None),
        ))
        .limit(5000)
    )
    history_rows = history_result.scalars().all()
    evidence_codes = list({*(row.stock_code for row in history_rows), *codes})
    sector_by_code = {}
    sector_name_by_code = {}
    if evidence_codes:
        sector_rows = await db.execute(
            select(
                StockSectorMapping.code,
                StockSectorMapping.sector_code,
                StockSectorMapping.sector_name,
                StockSectorMapping.sector_type,
            ).where(StockSectorMapping.code.in_(evidence_codes))
        )

        def _sector_priority(sector_type: Optional[str]) -> int:
            if sector_type in {"industry", "sw_l1", "sw_l2", "sw_l3"}:
                return 0
            if sector_type == "concept":
                return 1
            return 2

        sector_candidates = {}
        for code, sector_code, sector_name, sector_type in sector_rows.all():
            if not sector_code:
                continue
            candidate = (_sector_priority(sector_type), sector_code, sector_name)
            current = sector_candidates.get(code)
            if current is None or candidate < current:
                sector_candidates[code] = candidate
        sector_by_code = {code: item[1] for code, item in sector_candidates.items()}
        sector_name_by_code = {code: item[2] for code, item in sector_candidates.items() if item[2]}

    price_start = signal_day - timedelta(days=45)

    async def _load_price_rows(model, target_codes: list[str]) -> dict[str, list]:
        if not target_codes:
            return {}
        price_result = await db.execute(
            select(model)
            .where(model.code.in_(target_codes))
            .where(model.trade_date >= price_start)
            .where(model.trade_date <= signal_day)
            .order_by(model.code, model.trade_date)
        )
        price_by_code: dict[str, list] = {}
        for item in price_result.scalars().all():
            price_by_code.setdefault(item.code, []).append(item)
        return price_by_code

    price_by_code = await _load_price_rows(StockKline, codes)
    missing_price_codes = [code for code in codes if not price_by_code.get(code)]
    if missing_price_codes:
        daily_prices = await _load_price_rows(StockDaily, missing_price_codes)
        for code, items in daily_prices.items():
            if items:
                price_by_code[code] = items

    def _bounded(value: Optional[float], low: float, high: float) -> float:
        if value is None:
            return low
        return max(low, min(high, value))

    def _to_float(value) -> Optional[float]:
        if value is None:
            return None
        return float(value)

    def _avg(values: list[float]) -> Optional[float]:
        if not values:
            return None
        return sum(values) / len(values)

    def _shape_for(code: str) -> dict:
        price_rows = price_by_code.get(code, [])
        if not price_rows:
            return {
                "passed": False,
                "score": 0,
                "reasons": ["缺少信号日行情"],
                "change_pct": None,
                "volume_ratio": None,
                "close_position": None,
                "turnover": None,
            }
        current = price_rows[-1]
        prior_rows = price_rows[:-1][-5:]
        close = _to_float(current.close)
        high = _to_float(current.high)
        low = _to_float(current.low)
        prev_close = _to_float(getattr(current, "prev_close", None))
        change_pct = _to_float(getattr(current, "change_pct", None))
        if change_pct is None and prev_close and close:
            change_pct = (close - prev_close) / prev_close * 100
        close_position = None
        if close is not None and high is not None and low is not None and high > low:
            close_position = (close - low) / (high - low)
        current_volume = _to_float(getattr(current, "volume", None))
        prior_volumes = [
            _to_float(getattr(item, "volume", None))
            for item in prior_rows
            if _to_float(getattr(item, "volume", None)) is not None and _to_float(getattr(item, "volume", None)) > 0
        ]
        avg_volume = _avg(prior_volumes)
        volume_ratio = current_volume / avg_volume if current_volume and avg_volume else None
        turnover = _to_float(getattr(current, "turnover", None))

        reasons = []
        if change_pct is None:
            reasons.append("缺少涨跌幅")
        elif change_pct < 1:
            reasons.append("涨幅不足")
        elif change_pct >= 9.5:
            reasons.append("接近涨停不追")
        if close_position is None:
            reasons.append("缺少日内位置")
        elif close_position < 0.55:
            reasons.append("收盘位置偏弱")
        if volume_ratio is not None and volume_ratio < 1.05:
            reasons.append("量能不足")
        if turnover is not None and turnover < 1:
            reasons.append("换手不足")

        score = 35
        score += _bounded(change_pct, -3, 8) * 4
        score += _bounded((volume_ratio or 1) - 1, -0.5, 2) * 14
        score += _bounded(close_position, 0, 1) * 24
        score += _bounded(turnover, 0, 8) * 2
        if change_pct is not None and change_pct >= 9.5:
            score -= 18

        return {
            "passed": not reasons,
            "score": round(max(0, min(100, score)), 1),
            "reasons": reasons,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "close_position": round(close_position, 2) if close_position is not None else None,
            "turnover": round(turnover, 2) if turnover is not None else None,
        }

    round_trip_cost_pct = 0.36

    def _environment_bucket(row: SignalPerformance) -> str:
        score = row.signal_score or 0
        if score >= 90:
            score_bucket = "high"
        elif score >= 80:
            score_bucket = "mid"
        else:
            score_bucket = "low"
        return f"{row.signal_type or 'unknown'}:{score_bucket}"

    stock_stats: dict[str, list[dict]] = {}
    sector_stats: dict[str, list[dict]] = {}
    type_stats: dict[str, list[dict]] = {}
    env_stats: dict[str, list[dict]] = {}
    for row in history_rows:
        signal_type = row.signal_type or "unknown"
        item = {
            "return_1d": row.return_1d,
            "return_3d": row.return_3d,
            "return_5d": row.return_5d,
            "net_return_1d": row.return_1d - round_trip_cost_pct if row.return_1d is not None else None,
            "net_return_3d": row.return_3d - round_trip_cost_pct if row.return_3d is not None else None,
            "max_drawdown": row.max_drawdown,
        }
        stock_stats.setdefault(row.stock_code, []).append(item)
        sector_code = sector_by_code.get(row.stock_code)
        if sector_code:
            sector_stats.setdefault(sector_code, []).append(item)
        type_stats.setdefault(signal_type, []).append(item)
        env_stats.setdefault(_environment_bucket(row), []).append(item)

    def _values(items: list[dict], field: str) -> list[float]:
        values = []
        for item in items:
            value = item.get(field)
            if value is not None:
                values.append(float(value))
        return values

    def _return_stats(values: list[float]) -> dict:
        if not values:
            return {"win_rate": None, "avg_return": None}
        wins = sum(1 for value in values if value > 0)
        return {
            "win_rate": round(wins / len(values) * 100, 2),
            "avg_return": round(sum(values) / len(values), 2),
        }

    def _stats_for(items: list[dict]) -> dict:
        if not items:
            return {
                "sample_count": 0,
                "win_rate_1d": None,
                "avg_return_1d": None,
                "avg_net_return_1d": None,
                "win_rate_3d": None,
                "avg_return_3d": None,
                "avg_net_return_3d": None,
                "win_rate_5d": None,
                "avg_return_5d": None,
                "avg_max_drawdown": None,
                "worst_drawdown": None,
            }
        net_stats_1d = _return_stats(_values(items, "net_return_1d"))
        raw_stats_1d = _return_stats(_values(items, "return_1d"))
        net_stats_3d = _return_stats(_values(items, "net_return_3d"))
        raw_stats_3d = _return_stats(_values(items, "return_3d"))
        stats_5d = _return_stats(_values(items, "return_5d"))
        drawdowns = _values(items, "max_drawdown")
        avg_drawdown = round(sum(drawdowns) / len(drawdowns), 2) if drawdowns else None
        return {
            "sample_count": len(items),
            "win_rate_1d": net_stats_1d["win_rate"],
            "avg_return_1d": raw_stats_1d["avg_return"],
            "avg_net_return_1d": net_stats_1d["avg_return"],
            "win_rate_3d": net_stats_3d["win_rate"],
            "avg_return_3d": raw_stats_3d["avg_return"],
            "avg_net_return_3d": net_stats_3d["avg_return"],
            "win_rate_5d": stats_5d["win_rate"],
            "avg_return_5d": stats_5d["avg_return"],
            "avg_max_drawdown": avg_drawdown,
            "worst_drawdown": round(min(drawdowns), 2) if drawdowns else None,
        }

    def _blend_stats(sources: list[tuple[str, dict, float]]) -> dict:
        usable_sources = [(name, stats, weight) for name, stats, weight in sources if stats["sample_count"] > 0]
        if not usable_sources:
            return _stats_for([])

        def weighted(field: str) -> Optional[float]:
            numerator = 0.0
            denominator = 0.0
            for _name, stats, weight in usable_sources:
                value = stats.get(field)
                if value is None:
                    continue
                reliability = min(1.0, stats["sample_count"] / 30)
                effective_weight = weight * max(0.25, reliability)
                numerator += value * effective_weight
                denominator += effective_weight
            if denominator == 0:
                return None
            return round(numerator / denominator, 2)

        source_samples = {name: stats["sample_count"] for name, stats, _weight in sources}
        local_sample_count = max(source_samples.get("stock", 0), source_samples.get("sector", 0))
        blended = {
            "sample_count": local_sample_count or max(stats["sample_count"] for _name, stats, _weight in usable_sources),
            "local_sample_count": local_sample_count,
            "win_rate_1d": weighted("win_rate_1d"),
            "avg_return_1d": weighted("avg_return_1d"),
            "avg_net_return_1d": weighted("avg_net_return_1d"),
            "win_rate_3d": weighted("win_rate_3d"),
            "avg_return_3d": weighted("avg_return_3d"),
            "avg_net_return_3d": weighted("avg_net_return_3d"),
            "win_rate_5d": weighted("win_rate_5d"),
            "avg_return_5d": weighted("avg_return_5d"),
            "avg_max_drawdown": weighted("avg_max_drawdown"),
            "worst_drawdown": min(
                (stats["worst_drawdown"] for _name, stats, _weight in usable_sources if stats["worst_drawdown"] is not None),
                default=None,
            ),
            "source_samples": source_samples,
        }
        return blended

    def evidence_for(row: SignalPerformance) -> dict:
        sector_code = sector_by_code.get(row.stock_code)
        shape = _shape_for(row.stock_code)
        sources = [
            ("stock", _stats_for(stock_stats.get(row.stock_code, [])), 0.35),
            ("sector", _stats_for(sector_stats.get(sector_code, [])) if sector_code else _stats_for([]), 0.25),
            ("signal_type", _stats_for(type_stats.get(row.signal_type or "unknown", [])), 0.25),
            ("environment", _stats_for(env_stats.get(_environment_bucket(row), [])), 0.15),
        ]
        evidence = _blend_stats(sources)
        expected_net_return = None
        if evidence["avg_net_return_1d"] is not None or evidence["avg_net_return_3d"] is not None:
            net_1d = evidence["avg_net_return_1d"] if evidence["avg_net_return_1d"] is not None else 0
            net_3d = evidence["avg_net_return_3d"] if evidence["avg_net_return_3d"] is not None else 0
            expected_net_return = round(net_1d * 0.55 + net_3d * 0.45, 2)
        downside = abs(evidence["avg_max_drawdown"]) if evidence["avg_max_drawdown"] is not None and evidence["avg_max_drawdown"] < 0 else 0
        short_score = (
            _bounded(evidence["win_rate_1d"], 0, 100) * 0.25
            + _bounded(evidence["win_rate_3d"], 0, 100) * 0.20
            + _bounded(expected_net_return, -3, 6) * 8
            + shape["score"] * 0.25
            - downside * 1.5
        )
        ranking_score = (
            max(0, min(100, short_score)) * 0.55
            + _bounded(expected_net_return, -3, 6) * 8
            + shape["score"] * 0.25
            - downside * 1.2
            + (row.signal_score or 0) * 0.05
        )
        evidence.update({
            "shape": shape,
            "short_score": round(max(0, min(100, short_score)), 1),
            "expected_net_return": expected_net_return,
            "ranking_score": round(ranking_score, 2),
            "cost_pct": round_trip_cost_pct,
            "sector_code": sector_code,
            "sector_name": sector_name_by_code.get(row.stock_code),
        })
        return evidence

    def decision_for(eligible_rank: Optional[int], evidence: dict) -> tuple[str, str]:
        shape = evidence["shape"]
        if not shape["passed"]:
            return "观察", f"形态不达标: {'、'.join(shape['reasons'])}"
        sample_count = evidence["sample_count"]
        win_1d = evidence["win_rate_1d"]
        win_3d = evidence["win_rate_3d"]
        expected_net_return = evidence["expected_net_return"]
        short_score = evidence["short_score"]
        if sample_count < 20:
            return "观察", "个股/板块短线样本不足"
        if (win_1d is None or win_1d < 52) and (win_3d is None or win_3d < 55):
            return "观察", "成本后1-3日胜率不够"
        if expected_net_return is None or expected_net_return <= 0:
            return "观察", "成本后净收益期望不足"
        if short_score < 65:
            return "观察", "证据质量分未达标"
        if eligible_rank is not None and eligible_rank > max_positions:
            return "候补", "短线达标但超出持仓上限"
        return "入选", "形态、证据和净收益期望达标"

    candidate_items = []
    for row in rows:
        evidence = evidence_for(row)
        candidate_items.append((row, evidence))
    candidate_items.sort(
        key=lambda item: (
            item[1]["ranking_score"],
            item[1]["expected_net_return"] if item[1]["expected_net_return"] is not None else -999,
            item[0].signal_score or 0,
        ),
        reverse=True,
    )

    eligible_seen = 0
    candidates = []
    for idx, (row, evidence) in enumerate(candidate_items[:limit], start=1):
        precheck_passed = (
            evidence["shape"]["passed"]
            and evidence["sample_count"] >= 20
            and evidence["expected_net_return"] is not None
            and evidence["expected_net_return"] > 0
            and evidence["short_score"] >= 65
            and (
                (evidence["win_rate_1d"] is not None and evidence["win_rate_1d"] >= 52)
                or (evidence["win_rate_3d"] is not None and evidence["win_rate_3d"] >= 55)
            )
        )
        eligible_rank = None
        if precheck_passed:
            eligible_seen += 1
            eligible_rank = eligible_seen
        decision, reason = decision_for(eligible_rank, evidence)
        candidates.append({
            "code": row.stock_code,
            "name": names.get(row.stock_code),
            "rank": idx,
            "eligible_rank": eligible_rank,
            "score": row.signal_score,
            "type": row.signal_type,
            "top_factor": row.top_factor,
            "board_tag": row.board_tag,
            "decision": decision,
            "reason": reason,
            "evidence": evidence,
            "return_1d": row.return_1d,
            "return_3d": row.return_3d,
            "return_5d": row.return_5d,
            "return_10d": row.return_10d,
        })

    return {
        "signal_date": str(signal_day),
        "min_score": min_score,
        "candidates": candidates,
        "rules": {
            "source": "研究样本评分",
            "ranking": "短线形态 + 个股/板块证据 + 成本后收益回撤排序",
            "entry": f"评分 >= {min_score}，形态达标且净收益期望为正",
            "max_positions": max_positions,
            "execution": "策略回测时再应用仓位、滑点、涨跌停和成交量约束",
        },
    }


@router.get("/compare")
async def compare_backtest_runs(
    run_ids: str = Query(..., description="逗号分隔的回测ID"),
    db: AsyncSession = Depends(get_db),
):
    """横向对比多次回测."""
    from app.models.backtest import BacktestRun
    from sqlalchemy import select

    ids = [item.strip() for item in run_ids.split(",") if item.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="请选择要对比的回测")
    result = await db.execute(select(BacktestRun).where(BacktestRun.run_id.in_(ids)))
    rows = {_run.run_id: _run for _run in result.scalars().all()}
    return {
        "runs": [_run_to_summary(rows[run_id]) for run_id in ids if run_id in rows],
    }


@router.post("/backfill-signals")
async def backfill_signals(
    req: SignalBackfillRequest,
    db: AsyncSession = Depends(get_db),
):
    """从 stock_kline 生成可回测的研究样本."""
    from app.models.signal import SignalPerformance
    from app.models.stock import StockKline
    from sqlalchemy import desc, func, select

    latest_date = (await db.execute(select(func.max(StockKline.trade_date)))).scalar_one_or_none()
    if not latest_date:
        raise HTTPException(status_code=400, detail="暂无 stock_kline 行情，无法生成研究样本")

    end = _parse_date(req.end_date, "结束日期") or latest_date
    start = _parse_date(req.start_date, "开始日期") or date.fromordinal(max(end.toordinal() - 90, 1))
    if start > end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    code_rows = await db.execute(
        select(StockKline.code)
        .where(StockKline.trade_date == latest_date)
        .order_by(desc(StockKline.amount))
        .limit(req.max_codes)
    )
    codes = [code for code in code_rows.scalars().all()]
    if not codes:
        raise HTTPException(status_code=400, detail="最新交易日无可用股票行情")

    history_start = date.fromordinal(max(start.toordinal() - 80, 1))
    created = 0
    updated = 0
    scanned = 0
    skipped_low_score = 0

    for code in codes:
        result = await db.execute(
            select(StockKline)
            .where(StockKline.code == code)
            .where(StockKline.trade_date >= history_start)
            .where(StockKline.trade_date <= end)
            .order_by(StockKline.trade_date)
        )
        records = result.scalars().all()
        if len(records) < 25:
            continue

        for idx, row in enumerate(records):
            if row.trade_date < start or row.trade_date > end:
                continue
            scanned += 1
            score, signal_type = _score_momentum_signal(records, idx)
            if score < req.min_score or not signal_type:
                skipped_low_score += 1
                continue

            signal_id = f"bk{row.trade_date:%Y%m%d}{code}"
            max_return, max_drawdown = _calc_max_return(records, idx)
            existing = await db.get(SignalPerformance, signal_id)
            payload = {
                "stock_code": code,
                "signal_time": datetime.combine(row.trade_date, time(15, 0)),
                "signal_price": row.close or row.open,
                "signal_score": score,
                "signal_type": f"backfill_{signal_type}",
                "return_1d": _calc_forward_return(records, idx, 1),
                "return_3d": _calc_forward_return(records, idx, 3),
                "return_5d": _calc_forward_return(records, idx, 5),
                "return_10d": _calc_forward_return(records, idx, 10),
                "max_return": max_return,
                "max_drawdown": max_drawdown,
                "is_correct": (max_return or 0) > 0,
                "top_factor": "price_momentum",
                "board_tag": "backfill",
            }
            if existing:
                for key, value in payload.items():
                    setattr(existing, key, value)
                updated += 1
            else:
                db.add(SignalPerformance(signal_id=signal_id, **payload))
                created += 1

            if created + updated >= req.max_signals:
                break
        if created + updated >= req.max_signals:
            break

    await db.commit()
    _STATUS_CACHE.update({"expires_at": 0.0, "data": None, "scope": None})
    status = await _get_backtest_data_status(db)
    return {
        "status": "ok",
        "source": "stock_kline",
        "start_date": str(start),
        "end_date": str(end),
        "latest_price_date": str(latest_date),
        "scanned_codes": len(codes),
        "scanned_rows": scanned,
        "created": created,
        "updated": updated,
        "skipped_low_score": skipped_low_score,
        "total": created + updated,
        "data_status": status,
    }

@router.post("/signal")
async def backtest_signals(
    req: SignalBacktestRequest,
    db: AsyncSession = Depends(get_db),
):
    """信号回测 — 给研究样本计算实际收益"""
    from app.backtest import signal_backtester
    from app.models.backtest import BacktestRun, BacktestSignalResult
    from app.models.signal import SignalPerformance
    from sqlalchemy import select

    run_id = str(uuid4())
    start = _parse_date(req.start_date, "开始日期")
    end = _parse_date(req.end_date, "结束日期")
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    if not req.holding_days:
        raise HTTPException(status_code=400, detail="持有天数不能为空")

    # 从数据库取信号
    query = select(SignalPerformance)
    if req.signal_ids:
        query = query.where(SignalPerformance.signal_id.in_(req.signal_ids))
    if req.min_score is not None:
        query = query.where(SignalPerformance.signal_score >= req.min_score)
    if start:
        query = query.where(SignalPerformance.signal_time >= datetime.combine(start, time.min))
    if end:
        query = query.where(SignalPerformance.signal_time <= datetime.combine(end, time.max))

    result = await db.execute(query.limit(500))
    signals = result.scalars().all()

    if not signals:
        db.add(BacktestRun(
            run_id=run_id,
            run_type="signal",
            status="no_signals",
            start_date=start,
            end_date=end,
            config_json=_json_dumps(_signal_config(req)),
            metrics_json=_json_dumps({}),
            message="当前条件下没有可回测的信号",
        ))
        await db.commit()
        return {"status": "no_signals", "run_id": run_id, "total": 0}

    # 获取涉及股票的行情数据
    codes = list(set(s.stock_code for s in signals))
    price_data = await _load_price_data(db, codes[:50])

    # 转信号格式
    signal_list = [{
        "code": s.stock_code,
        "signal_date": s.signal_time.date() if hasattr(s.signal_time, "date") else s.signal_time,
        "signal_price": s.signal_price,
        "score": s.signal_score,
        "type": s.signal_type,
    } for s in signals]

    bt_result = await signal_backtester.backtest_signals(
        signal_list, price_data, req.holding_days
    )

    stats = bt_result.get("stats", {})
    db.add(BacktestRun(
        run_id=run_id,
        run_type="signal",
        status="completed",
        start_date=start,
        end_date=end,
        config_json=_json_dumps(_signal_config(req)),
        metrics_json=_json_dumps(stats),
        total_trades=bt_result.get("total", 0),
        message="信号回测完成",
    ))
    for item in bt_result.get("signals", []):
        returns = {
            k: v for k, v in item.items()
            if k.startswith("return_") or k.startswith("max_return_") or k.startswith("max_drawdown_")
        }
        db.add(BacktestSignalResult(
            run_id=run_id,
            code=item["code"],
            signal_date=date.fromisoformat(item["signal_date"]) if isinstance(item.get("signal_date"), str) else item.get("signal_date"),
            buy_price=item.get("buy_price"),
            score=item.get("score"),
            signal_type=item.get("type"),
            returns_json=_json_dumps(returns),
        ))
    await db.commit()
    bt_result["run_id"] = run_id
    bt_result["status"] = "completed"
    return bt_result


@router.post("/strategy")
async def backtest_strategy(
    req: StrategyBacktestRequest,
    db: AsyncSession = Depends(get_db),
):
    """策略回测 — 完整资金曲线回测"""
    from app.backtest import StrategyBacktester, BacktestConfig
    from app.models.backtest import BacktestDailyValue, BacktestRun, BacktestTrade
    from app.models.signal import SignalPerformance
    from sqlalchemy import select
    import pandas as pd

    run_id = str(uuid4())
    if req.strategy_id != "signal_score_rank":
        raise HTTPException(status_code=400, detail="该策略尚未接入回测执行")

    start = _parse_date(req.start_date, "开始日期", required=True)
    end = _parse_date(req.end_date, "结束日期", required=True)
    if start > end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    config = BacktestConfig(
        initial_capital=req.initial_capital,
        commission_rate=req.commission_rate,
        slippage_pct=req.slippage_pct,
        volume_limit_pct=req.volume_limit_pct,
        avoid_limit_up_down=req.avoid_limit_up_down,
        stop_loss_pct=req.stop_loss_pct,
        take_profit_pct=req.take_profit_pct,
        max_positions=req.max_positions,
        min_score_to_buy=req.min_score_to_buy,
    )

    backtester = StrategyBacktester(config)

    signal_result = await db.execute(
        select(SignalPerformance)
        .where(SignalPerformance.signal_time >= datetime.combine(start, time.min))
        .where(SignalPerformance.signal_time <= datetime.combine(end, time.max))
        .where(SignalPerformance.signal_score >= req.min_score_to_buy)
        .order_by(SignalPerformance.signal_time)
        .limit(1000)
    )
    signals = signal_result.scalars().all()

    if not signals:
        db.add(BacktestRun(
            run_id=run_id,
            run_type="strategy",
            status="no_scores",
            start_date=start,
            end_date=end,
            initial_capital=req.initial_capital,
            final_capital=req.initial_capital,
            config_json=_json_dumps(_strategy_config(req)),
            metrics_json=_json_dumps({}),
            message="回测区间内没有满足最低评分的信号数据",
        ))
        await db.commit()
        return {
            "status": "no_scores",
            "run_id": run_id,
            "message": "回测区间内没有满足最低评分的信号数据",
            "metrics": {},
            "daily_values": [],
            "trades": [],
            "config": _strategy_config(req),
        }

    daily_scores = pd.DataFrame([
        {
            "code": s.stock_code,
            "trade_date": s.signal_time.date() if hasattr(s.signal_time, "date") else s.signal_time,
            "score": s.signal_score or 0,
        }
        for s in signals
    ])

    codes = sorted({s.stock_code for s in signals})
    price_data = await _load_price_data(db, codes[:100], start, end)

    result = await backtester.backtest_strategy(daily_scores, price_data, start, end)
    metrics = result.get("metrics", {})
    benchmark = await _calc_benchmark(db, req.benchmark_code, start, end, req.initial_capital)
    metrics["benchmark"] = {
        "code": req.benchmark_code,
        "total_return_pct": benchmark.get("total_return_pct"),
        "max_drawdown_pct": benchmark.get("max_drawdown_pct"),
        "excess_return_pct": (
            round(metrics.get("total_return_pct", 0) - benchmark["total_return_pct"], 2)
            if benchmark.get("total_return_pct") is not None else None
        ),
    }
    validation = await _calc_validation_report(
        db, req, StrategyBacktester, BacktestConfig, daily_scores, price_data, start, end
    )
    metrics["validation"] = validation
    result["benchmark_curve"] = benchmark.get("nav_curve", [])
    result["benchmark"] = metrics["benchmark"]
    result["validation"] = validation
    db.add(BacktestRun(
        run_id=run_id,
        run_type="strategy",
        status="completed",
        start_date=start,
        end_date=end,
        initial_capital=req.initial_capital,
        final_capital=metrics.get("final_capital", req.initial_capital),
        total_return_pct=metrics.get("total_return_pct", 0),
        max_drawdown_pct=metrics.get("max_drawdown_pct", 0),
        sharpe_ratio=metrics.get("sharpe_ratio", 0),
        win_rate_pct=metrics.get("win_rate_pct", 0),
        total_trades=metrics.get("total_trades", 0),
        config_json=_json_dumps(_strategy_config(req)),
        metrics_json=_json_dumps(metrics),
        message="策略回测完成",
    ))
    for day in result.get("daily_values", []):
        total_value = day.get("total_value", req.initial_capital) or req.initial_capital
        trade_date = day.get("trade_date")
        db.add(BacktestDailyValue(
            run_id=run_id,
            trade_date=date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date,
            nav=round(total_value / req.initial_capital, 6) if req.initial_capital else 0,
            cash=day.get("cash"),
            position_value=day.get("position_value"),
            total_value=total_value,
            position_count=day.get("position_count", 0),
        ))
    for trade in result.get("trades", []):
        trade_date = trade.get("date")
        db.add(BacktestTrade(
            run_id=run_id,
            code=trade.get("code"),
            trade_type=trade.get("type"),
            trade_date=date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date,
            price=trade.get("price", 0),
            shares=trade.get("shares", 0),
            amount=trade.get("amount") or round((trade.get("price", 0) or 0) * (trade.get("shares", 0) or 0), 2),
            commission=trade.get("commission", 0),
            stamp_tax=trade.get("stamp_tax", 0),
            pnl=trade.get("pnl"),
        ))
    await db.commit()
    result.update({
        "run_id": run_id,
        "status": "completed",
        "message": "策略回测完成",
        "config": _strategy_config(req),
    })
    return result


@router.get("/performance/{run_id}")
async def get_performance(
    run_id: str,
    db: AsyncSession = Depends(get_db),
):
    """获取回测绩效."""
    from app.models.backtest import BacktestDailyValue, BacktestRun
    from sqlalchemy import select

    run = await db.get(BacktestRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="回测结果不存在")

    nav_result = await db.execute(
        select(BacktestDailyValue).where(
            BacktestDailyValue.run_id == run_id
        ).order_by(BacktestDailyValue.trade_date)
    )
    nav_records = nav_result.scalars().all()
    metrics = json.loads(run.metrics_json or "{}")
    config = json.loads(run.config_json or "{}")

    nav_curve = [{
        "date": str(n.trade_date),
        "nav": n.nav,
        "total_value": n.total_value,
        "cash": n.cash,
        "position_value": n.position_value,
        "position_count": n.position_count,
    } for n in nav_records]
    benchmark_curve = metrics.get("benchmark_curve", [])
    if not benchmark_curve and run.run_type == "strategy" and run.start_date and run.end_date:
        benchmark = await _calc_benchmark(
            db,
            config.get("benchmark_code", "000001"),
            run.start_date,
            run.end_date,
            run.initial_capital or config.get("initial_capital") or 1_000_000,
        )
        benchmark_curve = benchmark.get("nav_curve", [])

    return {
        "run_id": run.run_id,
        "run_type": run.run_type,
        "status": run.status,
        "message": run.message,
        "config": config,
        "metrics": metrics,
        "initial_capital": run.initial_capital or config.get("initial_capital"),
        "current_value": run.final_capital,
        "total_return_pct": run.total_return_pct or 0,
        "max_drawdown_pct": run.max_drawdown_pct or 0,
        "sharpe_ratio": run.sharpe_ratio or 0,
        "win_rate": run.win_rate_pct or 0,
        "total_count": run.total_trades or 0,
        "nav_curve": nav_curve,
        "benchmark": metrics.get("benchmark", {}),
        "benchmark_curve": benchmark_curve,
        "monthly_returns": metrics.get("monthly_returns", []),
        "drawdown_curve": metrics.get("drawdown_curve", []),
        "trade_distribution": metrics.get("trade_distribution", {}),
        "validation": metrics.get("validation", {"mode": "none", "windows": []}),
    }


@router.get("/trades/{run_id}")
async def get_trades(
    run_id: str,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """获取回测交易记录."""
    from app.models.backtest import BacktestRun, BacktestTrade
    from sqlalchemy import select

    run = await db.get(BacktestRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="回测结果不存在")

    result = await db.execute(
        select(BacktestTrade).where(
            BacktestTrade.run_id == run_id
        ).order_by(BacktestTrade.trade_date.desc(), BacktestTrade.id.desc()).limit(limit)
    )
    trades = result.scalars().all()

    return {
        "run_id": run_id,
        "trades": [{
            "id": t.id,
            "code": t.code,
            "type": t.trade_type,
            "time": str(t.trade_date),
            "price": t.price,
            "shares": t.shares,
            "amount": t.amount,
            "commission": t.commission or 0,
            "stamp_tax": t.stamp_tax or 0,
            "pnl": t.pnl,
            "pnl_pct": None,
        } for t in trades],
    }


@router.get("/signals/{run_id}")
async def get_signal_results(
    run_id: str,
    limit: int = Query(100, le=500),
    db: AsyncSession = Depends(get_db),
):
    """获取信号回测逐信号收益."""
    from app.models.backtest import BacktestRun, BacktestSignalResult
    from sqlalchemy import select

    run = await db.get(BacktestRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="回测结果不存在")
    if run.run_type != "signal":
        raise HTTPException(status_code=400, detail="该回测不是信号回测")

    result = await db.execute(
        select(BacktestSignalResult).where(
            BacktestSignalResult.run_id == run_id
        ).order_by(BacktestSignalResult.signal_date.desc(), BacktestSignalResult.id.desc()).limit(limit)
    )
    rows = result.scalars().all()

    return {
        "run_id": run_id,
        "signals": [{
            "id": item.id,
            "code": item.code,
            "signal_date": str(item.signal_date),
            "buy_price": item.buy_price,
            "score": item.score,
            "type": item.signal_type,
            **json.loads(item.returns_json or "{}"),
        } for item in rows],
    }
