"""异动追高过滤阈值校准 + 周度复校报告生成（只读分析，不改库）

同时供两块复用:
1. CLI 脚本 scripts/calibrate_chase_thresholds.py — 手动跑、看完整阈值扫查表。
2. 调度器 scheduler._weekly_chase_review — 每周末自动复校，日志落完整报告，飞书推精简摘要。

口径说明（与线上 _derive_kline_boundary 对齐，属保守近似）:
  - MA20/MA60、近20/60/120日高点、近20日涨幅 = 用 signal_day 当日(含)之前收盘价计算。
  - bias_to_ma* = signal_price 相对均线的乖离；return_5d = signal_price 相对 5 个交易日前收盘。
  - 只依赖 signal_day 当日及之前可见数据，无未来函数。
"""

from sqlalchemy import select

from app.db.session import async_session
from app.models.signal import SignalPerformance
from app.models.stock import StockKline
from app.signal.anomaly_scanner import _derive_kline_boundary
from app.api.v1.tenbagger import _kline_chase_reasons
from app.config.settings import settings

_METRIC_SETTINGS = {
    "bias_to_ma20_pct": "ANOMALY_CHASE_MAX_BIAS_MA20_PCT",
    "bias_to_ma60_pct": "ANOMALY_CHASE_MAX_BIAS_MA60_PCT",
    "return_5d": "ANOMALY_CHASE_MAX_RETURN_5D_PCT",
    "return_20d": "ANOMALY_CHASE_MAX_RETURN_20D_PCT",
    "dist_to_high_60d_pct": "ANOMALY_CHASE_NEAR_HIGH_GAP_PCT",
}

_SWEEPS: list[tuple[str, list[float]]] = [
    ("bias_to_ma20_pct", [5, 6, 7, 8, 9, 10, 12, 15]),
    ("bias_to_ma60_pct", [10, 12, 15, 18, 20, 25, 30]),
    ("return_5d", [8, 10, 12, 14, 16, 18, 20]),
    ("return_20d", [15, 20, 25, 30, 35, 40]),
    ("dist_to_high_60d_pct", [-3, -2, -1, 0, 1, 2]),
]


def _tech_from_bars(bars: list) -> dict | None:
    """从按 trade_date 升序的 K 线重建 _calc_technical_indicators 所需技术字段。"""
    closes = [float(b.close) for b in bars if b.close is not None and b.close > 0]
    highs = [float(b.high) for b in bars if b.high is not None and b.high > 0]
    if not closes:
        return None
    tech: dict = {}
    if len(closes) >= 20:
        tech["ma20"] = sum(closes[-20:]) / 20
        tech["return_20d"] = round((closes[-1] / closes[-20] - 1.0) * 100.0, 2)
    if len(closes) >= 60:
        tech["ma60"] = sum(closes[-60:]) / 60
    if len(highs) >= 20:
        tech["high_20d"] = max(highs[-20:])
    if len(highs) >= 60:
        tech["high_60d"] = max(highs[-60:])
    if len(highs) >= 120:
        tech["high_120d"] = max(highs[-120:])
    tech["recent_closes"] = closes[-20:]
    return tech


def _win_rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["is_correct"]) / len(rows)


def _avg_net_3d(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return sum(r["net_3d"] for r in rows) / len(rows)


async def build_chase_calibration_report() -> dict:
    """回放已结算异动信号，返回灰度验证 + 阈值扫查结果。

    Returns:
        dict: 包含 total/usable/blocked/passed/lift/summary/detail 等字段。
        summary 供飞书精简推送，detail 供日志留档。
    """
    async with async_session() as session:
        result = await session.execute(
            select(SignalPerformance).where(
                SignalPerformance.evaluation_version == "anomaly_v2",
                SignalPerformance.is_correct.is_not(None),
                SignalPerformance.signal_price > 0,
                SignalPerformance.signal_time.is_not(None),
            )
        )
        records = result.scalars().all()

    report: dict = {
        "total": len(records),
        "usable": 0,
        "blocked": 0,
        "passed": 0,
        "lift": 0.0,
        "summary": "",
        "detail": "",
    }

    if not records:
        report["detail"] = "无已结算的 anomaly_v2 信号样本，请先积累推送并结算。"
        report["summary"] = report["detail"]
        return report

    # 按 code 缓存历史 K 线，避免重复查询。
    kline_cache: dict[str, list] = {}
    rows: list[dict] = []

    for rec in records:
        code = rec.stock_code
        if code not in kline_cache:
            async with async_session() as session:
                bars = (
                    await session.execute(
                        select(StockKline)
                        .where(StockKline.code == code)
                        .order_by(StockKline.trade_date)
                    )
                ).scalars().all()
            kline_cache[code] = bars
        bars = [b for b in kline_cache[code] if b.trade_date <= rec.signal_time.date()]
        tech = _tech_from_bars(bars) if bars else None
        if not tech:
            continue
        boundary = _derive_kline_boundary(tech, float(rec.signal_price))
        if not boundary:
            continue
        rows.append({
            "code": code,
            "boundary": boundary,
            "blocked": bool(_kline_chase_reasons(boundary)),
            "is_correct": bool(rec.is_correct),
            "net_3d": float(rec.net_return_3d or 0.0),
        })

    usable = len(rows)
    report["usable"] = usable
    if usable == 0:
        report["detail"] = "无可用 K 线样本（历史 K 线不足 20 日），请先补历史 K 线。"
        report["summary"] = report["detail"]
        return report

    blocked = [r for r in rows if r["blocked"]]
    passed = [r for r in rows if not r["blocked"]]
    blocked_wr = _win_rate(blocked)
    passed_wr = _win_rate(passed)
    lift = passed_wr - blocked_wr

    report["blocked"] = len(blocked)
    report["passed"] = len(passed)
    report["lift"] = round(lift, 4)
    report["blocked_win_rate"] = round(blocked_wr, 4)
    report["passed_win_rate"] = round(passed_wr, 4)
    report["blocked_net_3d"] = round(_avg_net_3d(blocked), 4)
    report["passed_net_3d"] = round(_avg_net_3d(passed), 4)

    lines: list[str] = []
    lines.append(f"已结算样本 {len(records)} 条，可回放 {usable} 条（现阈值下拦截 {len(blocked)} 条）")
    lines.append("")
    lines.append("【灰度验证】3日净胜率对比")
    lines.append(f"  拦截组 n={len(blocked):<3} 胜率={blocked_wr:.0%} net3d={_avg_net_3d(blocked):+.2f}%")
    lines.append(f"  放行组 n={len(passed):<3} 胜率={passed_wr:.0%} net3d={_avg_net_3d(passed):+.2f}%")
    lines.append(f"  过滤 lift = {lift:+.1%}（正值=放行组优于拦截组，过滤有效）")
    lines.append("")
    lines.append("【阈值扫查】")
    for metric, candidates in _SWEEPS:
        current = getattr(settings, _METRIC_SETTINGS[metric], None)
        lines.append(f"  [{metric}] 现值={current}")
        lines.append(f"    {'阈值':>6} | {'拦截n':>5} | {'拦截胜率':>7} | {'放行胜率':>7} | {'lift':>7}")
        for value in candidates:
            above = [r for r in rows if r["boundary"].get(metric, -9999.0) >= value]
            below = [r for r in rows if r["boundary"].get(metric, -9999.0) < value]
            if not above or not below:
                continue
            this_lift = _win_rate(below) - _win_rate(above)
            lines.append(
                f"    {value:>6} | {len(above):>5} | {_win_rate(above):>7.0%} | "
                f"{_win_rate(below):>7.0%} | {this_lift:>+7.1%}"
            )
    report["detail"] = "\n".join(lines)

    report["summary"] = (
        f"已结算样本 {len(records)} 条（可回放 {usable} 条）\n"
        f"拦截组 n={len(blocked)} 胜率 {blocked_wr:.0%} net3d {_avg_net_3d(blocked):+.2f}%\n"
        f"放行组 n={len(passed)} 胜率 {passed_wr:.0%} net3d {_avg_net_3d(passed):+.2f}%\n"
        f"过滤 lift {lift:+.1%}（正值=过滤有效）\n"
        f"完整阈值扫查见服务日志，阈值建议以 lift 最大且拦截组胜率显著<50% 为准。"
    )
    return report