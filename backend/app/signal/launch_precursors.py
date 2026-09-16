"""启动前特征：资金预热、K线修复与低位行业点火。

所有函数都只处理调用方传入的 ``as-of`` 数据，不自行读取未来行情。特征阈值来自
2026-04-06 至 2026-08-28 主板首板样本与同日对照研究；它们用于排序和预测解释，
不能单独绕过竞价、盘口和交易风控闸门。
"""

from __future__ import annotations

from math import log
from typing import Any, Iterable


FEATURE_VERSION = "launch_precursors_v20260828_2_outcome_split"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def _pct_change(current: float, base: float) -> float:
    if current <= 0 or base <= 0:
        return 0.0
    return (current / base - 1.0) * 100.0


def _range_score(
    value: float,
    *,
    ideal_low: float,
    ideal_high: float,
    minimum: float,
    maximum: float,
) -> float:
    if ideal_low <= value <= ideal_high:
        return 100.0
    if value < ideal_low:
        return _clamp((value - minimum) / max(ideal_low - minimum, 1e-6) * 100.0)
    return _clamp((maximum - value) / max(maximum - ideal_high, 1e-6) * 100.0)


def build_stock_launch_profile(bars: list[dict]) -> dict:
    """从已完成日K构建启动前画像。

    研究结论不是“越低越好”：绝对低位在对照组更常见。这里要求中低位已经开始
    修复，同时有可识别的换手和量能活跃，避免把长期阴跌误判为启动。
    """

    valid = [
        item
        for item in bars
        if _float(item.get("close")) > 0
        and _float(item.get("high")) > 0
        and _float(item.get("low")) > 0
        and _float(item.get("volume")) > 0
    ]
    default = {
        "launch_feature_version": FEATURE_VERSION,
        "launch_profile_ready": False,
        "launch_mid_low_repair": False,
        "launch_active_volume_turnover": False,
        "launch_deep_low_risk": False,
        "launch_profile_score": 0.0,
        "launch_position_120": 1.0,
        "launch_return_5d": 0.0,
        "launch_return_20d": 0.0,
        "launch_return_60d": 0.0,
        "launch_distance_ma20": 0.0,
        "launch_volume_ratio_20": 0.0,
        "launch_volume_ratio_5_20": 0.0,
        "launch_turnover": 0.0,
        "launch_prior_impulse_20": 0.0,
    }
    if len(valid) < 61:
        return default

    latest = valid[-1]
    closes = [_float(item.get("close")) for item in valid]
    volumes = [_float(item.get("volume")) for item in valid]
    long_window = valid[-120:]
    long_high = max(_float(item.get("high")) for item in long_window)
    long_low = min(_float(item.get("low")) for item in long_window)
    latest_close = closes[-1]
    position_120 = (
        (latest_close - long_low) / (long_high - long_low)
        if long_high > long_low
        else 1.0
    )
    return_5d = _pct_change(latest_close, closes[-6])
    return_20d = _pct_change(latest_close, closes[-21])
    return_60d = _pct_change(latest_close, closes[-61])
    ma20 = _mean(closes[-20:])
    distance_ma20 = _pct_change(latest_close, ma20)
    prior_volume_20 = _mean(volumes[-21:-1])
    avg_volume_5 = _mean(volumes[-5:])
    avg_volume_20 = _mean(volumes[-20:])
    volume_ratio_20 = volumes[-1] / prior_volume_20 if prior_volume_20 > 0 else 0.0
    volume_ratio_5_20 = avg_volume_5 / avg_volume_20 if avg_volume_20 > 0 else 0.0
    turnover = _float(latest.get("turnover"))
    prior_changes = [_float(item.get("change_pct")) for item in valid[-21:-1]]
    prior_impulse_20 = max(prior_changes or [0.0])

    mid_low_repair = bool(
        0.12 <= position_120 <= 0.58
        and -4.0 <= return_5d <= 10.0
        and -3.0 <= return_20d <= 24.0
        and -2.5 <= distance_ma20 <= 12.0
    )
    active_volume_turnover = bool(
        0.75 <= volume_ratio_20 <= 2.50
        and turnover >= 1.50
    )
    deep_low_risk = bool(position_120 < 0.12 and return_20d < 0.0)
    profile_ready = bool(mid_low_repair and active_volume_turnover and not deep_low_risk)
    profile_score = _clamp(
        _range_score(
            position_120,
            ideal_low=0.20,
            ideal_high=0.48,
            minimum=0.05,
            maximum=0.72,
        )
        * 0.24
        + _range_score(
            return_20d,
            ideal_low=0.0,
            ideal_high=12.0,
            minimum=-12.0,
            maximum=28.0,
        )
        * 0.22
        + _range_score(
            distance_ma20,
            ideal_low=-0.5,
            ideal_high=6.0,
            minimum=-8.0,
            maximum=14.0,
        )
        * 0.18
        + _range_score(
            volume_ratio_20,
            ideal_low=0.85,
            ideal_high=1.65,
            minimum=0.45,
            maximum=3.20,
        )
        * 0.20
        + _range_score(
            turnover,
            ideal_low=2.0,
            ideal_high=9.0,
            minimum=0.5,
            maximum=22.0,
        )
        * 0.16
        - (18.0 if deep_low_risk else 0.0)
    )
    if not mid_low_repair:
        profile_score = min(profile_score, 54.0)
    if not active_volume_turnover:
        profile_score = min(profile_score, 58.0)

    return {
        "launch_feature_version": FEATURE_VERSION,
        "launch_profile_ready": profile_ready,
        "launch_mid_low_repair": mid_low_repair,
        "launch_active_volume_turnover": active_volume_turnover,
        "launch_deep_low_risk": deep_low_risk,
        "launch_profile_score": round(profile_score, 2),
        "launch_position_120": round(position_120, 4),
        "launch_return_5d": round(return_5d, 2),
        "launch_return_20d": round(return_20d, 2),
        "launch_return_60d": round(return_60d, 2),
        "launch_distance_ma20": round(distance_ma20, 2),
        "launch_volume_ratio_20": round(volume_ratio_20, 3),
        "launch_volume_ratio_5_20": round(volume_ratio_5_20, 3),
        "launch_turnover": round(turnover, 3),
        "launch_prior_impulse_20": round(prior_impulse_20, 2),
    }


def build_funding_preheat_context(rows: list[dict]) -> dict:
    """汇总截至锚点日的个股主力资金，重点保留三日改善而非单日脉冲。"""

    ordered = sorted(
        [item for item in rows if item.get("trade_date") is not None],
        key=lambda item: item.get("trade_date"),
    )
    recent_five = ordered[-5:]
    recent_three = ordered[-3:]
    pct_three = [_float(item.get("main_net_inflow_pct")) for item in recent_three]
    pct_five = [_float(item.get("main_net_inflow_pct")) for item in recent_five]
    net_three = [_float(item.get("main_net_inflow")) for item in recent_three]
    net_five = [_float(item.get("main_net_inflow")) for item in recent_five]
    positive_days_3d = sum(value > 0 for value in pct_three)
    positive_days_5d = sum(value > 0 for value in pct_five)
    pct_sum_3d = sum(pct_three)
    pct_sum_5d = sum(pct_five)
    previous = pct_five[:-2]
    acceleration = _mean(pct_five[-2:]) - _mean(previous) if len(pct_five) >= 4 else 0.0
    coverage_days = len(recent_five)
    coverage_complete = len(recent_three) == 3
    ready = bool(
        coverage_complete
        and positive_days_3d >= 2
        and pct_sum_3d >= 3.0
    )
    supportive = bool(
        coverage_complete
        and (pct_sum_3d >= 0.0 or positive_days_3d >= 2)
    )
    persistent_outflow = bool(
        coverage_complete
        and positive_days_3d == 0
        and pct_sum_3d <= -8.0
    )
    score = 0.0
    if coverage_complete:
        score = _clamp(
            45.0
            + _clamp(pct_sum_3d, -12.0, 12.0) * 1.2
            + positive_days_3d * 5.0
            + _clamp(acceleration, -6.0, 6.0)
            + (5.0 if ready else 0.0)
            - (10.0 if persistent_outflow else 0.0)
        )

    return {
        "launch_feature_version": FEATURE_VERSION,
        "funding_coverage_days": coverage_days,
        "funding_coverage_complete_3d": coverage_complete,
        "funding_as_of_trade_date": str(ordered[-1].get("trade_date")) if ordered else "",
        "funding_main_inflow_3d": round(sum(net_three), 2),
        "funding_main_inflow_5d": round(sum(net_five), 2),
        "funding_main_inflow_pct_3d": round(pct_sum_3d, 2),
        "funding_main_inflow_pct_5d": round(pct_sum_5d, 2),
        "funding_positive_days_3d": positive_days_3d,
        "funding_positive_days_5d": positive_days_5d,
        "funding_acceleration": round(acceleration, 2),
        "funding_preheat_ready": ready,
        "funding_preheat_supportive": supportive,
        "funding_persistent_outflow_risk": persistent_outflow,
        "funding_preheat_score": round(score, 2),
    }


def build_primary_industry_evidence(industry: dict | None) -> dict:
    """把主营行业上下文标准化为独立证据，避免被宽泛概念覆盖。"""

    industry = industry or {}
    strength = _float(industry.get("strength_score"))
    breadth = int(_float(industry.get("limit_up_count")))
    consecutive_days = int(_float(industry.get("consecutive_days")))
    rotation_score = _float(industry.get("sector_rotation_score"))
    strength_delta = _float(industry.get("sector_strength_delta"))
    lifecycle_state = str(industry.get("lifecycle_state") or "")
    low_rotation = bool(industry.get("sector_low_position_rotation"))
    emerging = lifecycle_state in {"emerging", "accelerating"}
    ignition_ready = bool(
        low_rotation
        or (
            strength >= 55.0
            and breadth >= 2
            and (consecutive_days <= 3 or emerging or strength_delta >= 8.0)
        )
        or (emerging and strength >= 50.0 and breadth >= 1)
    )
    score = _clamp(
        strength * 0.35
        + rotation_score * 0.28
        + min(breadth, 5) / 5.0 * 20.0
        + max(min(strength_delta, 20.0), 0.0) * 0.45
        + (8.0 if low_rotation else 0.0)
        + (6.0 if emerging else 0.0)
    )
    if not ignition_ready:
        score = min(score, 58.0)
    return {
        "primary_industry_code": str(industry.get("sector_code") or ""),
        "primary_industry_name": str(industry.get("sector_name") or ""),
        "primary_industry_strength": round(strength, 2),
        "primary_industry_limit_up_count": breadth,
        "primary_industry_consecutive_days": consecutive_days,
        "primary_industry_fund_flow": round(_float(industry.get("fund_flow")), 2),
        "primary_industry_rotation_score": round(rotation_score, 2),
        "primary_industry_strength_delta": round(strength_delta, 2),
        "primary_industry_limit_up_delta": round(
            _float(industry.get("sector_limit_up_delta")), 2
        ),
        "primary_industry_lifecycle_state": lifecycle_state,
        "primary_industry_low_position_rotation": low_rotation,
        "primary_industry_ignition_ready": ignition_ready,
        "primary_industry_ignition_score": round(score, 2),
    }


def build_low_base_sector_ignition_context(
    stock_profile: dict | None,
    funding: dict | None,
    primary_industry: dict | None,
    *,
    news: dict | None = None,
) -> dict:
    """组合低位修复、主营行业点火和资金/消息确认。

    该组合始终是预测证据，不直接表示可交易。交易执行仍应由调用方的实时竞价、
    盘口和风险闸门决定。
    """

    stock_profile = stock_profile or {}
    funding = funding or {}
    industry_evidence = build_primary_industry_evidence(primary_industry)
    news = news or {}
    stock_ready = bool(stock_profile.get("launch_profile_ready"))
    industry_ready = bool(industry_evidence.get("primary_industry_ignition_ready"))
    funding_ready = bool(funding.get("funding_preheat_ready"))
    direct_news = bool(
        str(news.get("news_mapping_mode") or "") == "direct_code"
        and (
            str(news.get("news_event_grade") or "") in {"hard", "medium"}
            or int(_float(news.get("news_count"))) >= 3
        )
        and _float(news.get("news_catalyst_score")) >= 54.0
    )
    overnight_direct_news = bool(
        direct_news and str(news.get("news_information_phase") or "") == "overnight"
    )
    ready = bool(stock_ready and industry_ready)
    confirmed = bool(ready and (funding_ready or direct_news))
    score = _clamp(
        _float(stock_profile.get("launch_profile_score")) * 0.38
        + _float(industry_evidence.get("primary_industry_ignition_score")) * 0.42
        + _float(funding.get("funding_preheat_score")) * 0.14
        + (6.0 if direct_news else 0.0)
    )
    if not ready:
        score = min(score, 55.0)

    # 全量特征表与后29日时间外留出共同约束不同预测目标：
    # - 首板排序：低位行业+活跃量价 LR 1.301，叠加三日资金后 LR 1.486；
    # - 普通收涨：该组合本身 lift 0.922，不得拿首板 LR 去抬高方向概率；
    # - 强涨：组合、资金和直接消息分别有 1.180 / 1.209 / 1.620 lift。
    # 因此首板证据、收涨微调和强涨微调必须独立，且只取部分 log-lift，
    # 避免与路由后验重复计算。
    evidence_log_lift = (
        log(1.486)
        if ready and funding_ready
        else log(1.301)
        if ready
        else log(1.234)
        if funding_ready
        else 0.0
    )

    direction_logit_delta = 0.0
    if stock_ready:
        direction_logit_delta += log(1.025) * 0.50
    if funding_ready:
        direction_logit_delta += log(1.020) * 0.60
    if direct_news:
        direction_logit_delta += log(1.127) * 0.60
    if overnight_direct_news:
        direction_logit_delta += log(1.306) * 0.45
    if bool(funding.get("funding_persistent_outflow_risk")):
        direction_logit_delta += log(0.979) * 0.80
    direction_logit_delta = max(-0.12, min(direction_logit_delta, 0.18))

    strong_rise_logit_delta = 0.0
    if stock_ready:
        strong_rise_logit_delta += log(1.036) * 0.50
    if ready:
        strong_rise_logit_delta += log(1.180) * 0.50
    if funding_ready:
        strong_rise_logit_delta += log(1.209) * 0.50
    if direct_news:
        strong_rise_logit_delta += log(1.620) * 0.40
    if overnight_direct_news:
        strong_rise_logit_delta += log(1.528) * 0.30
    if bool(funding.get("funding_persistent_outflow_risk")):
        strong_rise_logit_delta += log(0.881) * 0.50
    strong_rise_logit_delta = max(-0.18, min(strong_rise_logit_delta, 0.32))

    tier = (
        "confirmed"
        if confirmed
        else "industry_stock_resonance"
        if ready
        else "stock_repair_watch"
        if stock_ready
        else "none"
    )
    return {
        **industry_evidence,
        "low_base_sector_ignition_ready": ready,
        "low_base_sector_ignition_confirmed": confirmed,
        "low_base_sector_ignition_prediction_only": ready,
        "low_base_sector_ignition_tier": tier,
        "low_base_sector_ignition_score": round(score, 2),
        "low_base_sector_ignition_funding_confirmed": funding_ready,
        "low_base_sector_ignition_direct_news_confirmed": direct_news,
        "launch_evidence_log_lift": round(evidence_log_lift, 4),
        "launch_direction_logit_delta": round(direction_logit_delta, 4),
        "launch_strong_rise_logit_delta": round(strong_rise_logit_delta, 4),
        "launch_probability_targets_split": True,
    }


def apply_logit_delta(probability: float, delta: float) -> float:
    """给0~1概率施加有界 log-odds 微调。"""

    bounded = min(max(_float(probability), 1e-4), 1.0 - 1e-4)
    odds = bounded / (1.0 - bounded)
    adjusted_odds = odds * pow(2.718281828459045, _float(delta))
    return adjusted_odds / (1.0 + adjusted_odds)
