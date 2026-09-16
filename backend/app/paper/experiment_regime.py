"""冻结的双指数MA20/MA60趋势代理；是实验分层定义，不是行情预测或开仓开关。"""
from datetime import timedelta
import math

from sqlalchemy import select
from app.models.governance import TradeCalendarModel
from app.core.trade_calendar import is_official_closed_day
from app.data.index_history_window import latest_verified_history
from app.data.index_history import HISTORY_SCHEMA

REGIME_VERSION = "sse_szse_ma20_60_slope5_v1"
BENCHMARK_CODES = ("000001", "399001")  # StockDaily专用指数表写入契约，非StockKline平安银行


def classify_index_closes(closes):
    try:
        valid = len(closes) == 65 and not any(isinstance(v, bool) for v in closes)
        closes = [float(v) for v in closes]
        valid = valid and all(math.isfinite(v) and v > 0 for v in closes)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        return {"label": "unknown", "reason": "需要65个连续有效收盘价"}
    close = float(closes[-1])
    ma20, ma60 = sum(closes[-20:])/20, sum(closes[-60:])/60
    old20, old60 = sum(closes[-25:-5])/20, sum(closes[-65:-5])/60
    if close > ma20 > ma60 and ma20 > old20 and ma60 >= old60:
        label = "bull"
    elif close < ma20 < ma60 and ma20 < old20 and ma60 <= old60:
        label = "bear"
    else:
        label = "sideways"
    return {"label": label, "close": close, "ma20": round(ma20, 6),
            "ma60": round(ma60, 6), "ma20_5d_ago": round(old20, 6), "ma60_5d_ago": round(old60, 6)}


async def previous_known_trade_day(db, *, at):
    """只读已落库日历；不能跳过缺失工作日把更早日期冒充昨收。"""
    days = dict((await db.execute(select(
        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day,
    ).where(
        TradeCalendarModel.trade_date < at.date(),
        TradeCalendarModel.trade_date >= at.date() - timedelta(days=40),
    ))).all())
    for offset in range(1, 41):
        day = at.date() - timedelta(days=offset)
        if day.weekday() >= 5 or is_official_closed_day(day):
            continue
        if day not in days:
            return None
        if days[day]:
            return day
    return None


async def freeze_benchmark_regime(db, *, at, previous_trade_date):
    result = {
        "version": REGIME_VERSION, "label": "unknown", "quality_status": "missing",
        "source_trade_date": previous_trade_date.isoformat() if previous_trade_date else None, "observed_at": at.isoformat(),
        "definition": "沪深主板指数同时收盘>MA20>MA60且均线5日斜率向上为牛态；双指数反向为熊态；其余震荡态",
        "scope": "previous_completed_session_trend_proxy", "benchmarks": [],
        "data_contract": HISTORY_SCHEMA,
        "note": "牛熊仅是冻结的实验趋势代理，不等于长期牛熊定论；不限制策略参与",
    }
    if previous_trade_date is None:
        result["reason"] = "已落库交易日历不足以确认上一交易日；入场链路不联网补取"
        return result
    if previous_trade_date >= at.date():
        result["reason"] = "只允许上一完整交易日，不得消费当日未完成bar"
        return result
    # 直接消费当时已冻结的真实65日输入，不读取可被盘中覆盖/盘后回补的StockDaily。
    # 缺少该时点证据仍为unknown；不使用今天补到的数据重新标记历史订单。
    health, window = await latest_verified_history(db, through_date=previous_trade_date, at=at)
    if health is None:
        result["reason"] = "缺少决策时已知的65日完整指数历史窗口/日历/真实收盘证据"
        return result
    for code in BENCHMARK_CODES:
        source = window["codes"][code]
        values = list(source["input_closes"])
        classification = classify_index_closes(values)
        if classification["label"] == "unknown":
            result["reason"] = classification["reason"]
            return result
        result["benchmarks"].append({
            "code": code, **classification,
            "input_dates": list(source["input_dates"]), "input_closes": values,
            "source_contract": source["source_contract"],
            "source_observed_at": source["source_observed_at"],
            "input_sha256": source["input_sha256"],
        })
    labels = [row["label"] for row in result["benchmarks"]]
    result["label"] = labels[0] if len(set(labels)) == 1 else "sideways"
    result["quality_status"] = "ok"
    result["source_health_id"] = health.id
    return result
