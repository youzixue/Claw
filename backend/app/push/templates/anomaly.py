"""异动推送模板 — 涨停/跌停/资金异动/突破

v2: 增加风险提示 + 作战预案 + 失效条件，防止诱多诱空
"""

from app.push.channels.base import PushMessage


def _fmt_price(value: float) -> str:
    return f"¥{value:.2f}" if value else "--"


def _fmt_pct(value: float) -> str:
    if value is None:
        return "--"
    return f"{value:+.2f}%"


def _fmt_pct_plain(value: float) -> str:
    if not value:
        return "--"
    return f"{value:.2f}%"


def _fmt_ratio_pct(value: float) -> str:
    if value is None:
        return "--"
    return f"{float(value) * 100:.1f}%"


def _fmt_amount(value: float, force_yi: bool = False) -> str:
    if not value:
        return "--"
    if force_yi:
        return f"¥{value / 1e8:.2f}亿"
    if abs(value) >= 1e8:
        return f"¥{value / 1e8:.2f}亿"
    if abs(value) >= 1e4:
        return f"¥{value / 1e4:.2f}万"
    return f"¥{value:.0f}"


def _fmt_volume(value: float) -> str:
    if not value:
        return "--"
    hands = value
    if abs(hands) >= 1e4:
        return f"{hands / 1e4:.2f}万手"
    return f"{hands:.0f}手"


def _fmt_market_cap_billion(value: float) -> str:
    if not value:
        return "--"
    return f"¥{value:.2f}亿"


def _fmt_net_amount_yi(value: float) -> str:
    if value is None:
        return "--"
    abs_yi = abs(float(value)) / 1e8
    return f"-{abs_yi:.1f}亿" if float(value) < 0 else f"{abs_yi:.1f}亿"


def _risk_text(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("⚠️"):
        text = text[2:].strip()
    return text


def _fmt_sector_factors(sector_factors: list[dict] | None) -> list[str]:
    lines: list[str] = []
    for item in sector_factors or []:
        name = item.get("sector_name") or item.get("sector_code") or "未知板块"
        change_pct = float(item.get("change_pct") or 0)
        fund_flow = float(item.get("fund_flow") or 0)
        strength = float(item.get("strength_score") or 0)
        active_days = int(item.get("consecutive_days") or 0)
        limit_up_count = int(item.get("limit_up_count") or 0)
        lines.append(
            f"• {name} | {_fmt_pct(change_pct)} | 资金{fund_flow:.2f}亿 | 强度{strength:.0f} | 活跃{active_days}天 | 涨停{limit_up_count}家"
        )
    return lines


def _fmt_sector_components(sector_components: list[dict] | None) -> list[str]:
    lines: list[str] = []
    for item in sector_components or []:
        leaders = item.get("leaders") or []
        labels = [str(leader.get("label") or leader.get("name") or "") for leader in leaders if (leader.get("label") or leader.get("name"))]
        if not labels:
            continue
        sector_name = item.get("sector_name") or "同板块"
        lines.append(f"• {sector_name}: {'、'.join(labels[:3])}")
    return lines


def _fund_source_label(source: str) -> str:
    if (source or "").startswith("eastmoney_main_fund"):
        return "东方财富主力资金"
    if (source or "").startswith("fund_flow"):
        return "统一资金数据（来源见证据）"
    if source == "stock_spot":
        return "Tencent 实时行情（参考）"
    return "未知来源"


def _fund_source_method(source: str) -> str:
    if (source or "").startswith("eastmoney_main_fund"):
        return "主力 = 超大单 + 大单"
    if (source or "").startswith("fund_flow"):
        return "主力口径及有效性以来源证据为准，不以盘口或总净额替代"
    if source == "stock_spot":
        return "实时盘口参考口径"
    return ""


def _priority_label(level: str) -> str:
    return {
        "critical": "P1",
        "major": "P2",
        "minor": "P3",
    }.get(level, "P3")


def _risk_level(volatility_risk: str, risk_count: int) -> str:
    if volatility_risk == "high" or risk_count >= 3:
        return "高风险"
    if volatility_risk == "medium" or risk_count >= 1:
        return "中风险"
    return "低风险"


def _plan_prices(
    signal: str,
    current_price: float,
    support: float = 0,
    pressure: float = 0,
) -> dict:
    if current_price <= 0:
        return {"entry": 0, "target": 0, "stop": 0}
    if signal == "买入":
        entry = support if support and support > 0 and abs(current_price - support) / current_price <= 0.05 else current_price
        target = pressure if pressure and pressure > current_price else current_price * 1.05
        stop = support * 0.99 if support and support > 0 else current_price * 0.95
    else:
        entry = current_price
        target = support if support and support > 0 else current_price * 0.95
        stop = pressure * 1.01 if pressure and pressure > current_price else current_price * 1.03
    return {"entry": round(entry, 2), "target": round(target, 2), "stop": round(stop, 2)}


def _position_hint(signal: str, level: str, risk_count: int) -> str:
    if signal != "买入":
        return "0%（以减仓/回避为主）"
    if level == "critical" and risk_count == 0:
        return "20%"
    if level in {"critical", "major"} and risk_count <= 1:
        return "15%"
    return "10%"


def limit_up_alert(
    code: str, name: str, price: float,
    consecutive_days: int = 1, seal_amount: float = 0,
    seal_ratio: float = 0,  # 封单占成交量比
    first_seal_time: str = "",  # 首次封板时间
    open_times: int = 1,  # 开板次数
    turnover_rate: float = 0,  # 换手率
    reason: str = "",
    # 作战预案参数
    next_day_support: float = 0,  # 次日支撑位
    next_day_pressure: float = 0,  # 次日压力位
    volume_ratio: float = 0,  # 量比
    sector_strength: str = "",  # 板块强度 strong/medium/weak
    signal_label: str = "",
    confirmation_reasons: list[str] | None = None,
    source: str = "",
    as_of: str = "",
    setup_grade: str = "B类观察候选",
) -> PushMessage:
    """涨停异动 — 含封板质量评估 + 作战预案"""
    days_text = f"（{consecutive_days}连板）" if consecutive_days > 1 else ""
    confirmation_reasons = confirmation_reasons or []

    # === 封板质量评估 ===
    quality_parts = []
    if seal_amount > 0:
        quality_parts.append(f"封板资金 {seal_amount/1e8:.1f}亿")
    if seal_ratio > 0:
        quality_parts.append(f"封单比 {seal_ratio:.0%}")
    if first_seal_time:
        quality_parts.append(f"封板时间 {first_seal_time}")
    if open_times > 0:
        quality_parts.append(f"开板 {open_times}次")
    if turnover_rate > 0:
        quality_parts.append(f"换手 {turnover_rate:.1f}%")
    if volume_ratio > 0:
        quality_parts.append(f"量比 {volume_ratio:.1f}")
    quality_text = " | ".join(quality_parts) if quality_parts else "数据待更新"

    # === 风险评估 ===
    risks = []
    if open_times >= 3:
        risks.append("多次炸板，封板不牢，次日低开概率大")
    if first_seal_time and first_seal_time >= "14:00":
        risks.append("尾盘封板，资金分歧大，次日溢价不确定")
    if turnover_rate > 20:
        risks.append(f"换手率{turnover_rate:.1f}%过高，筹码松动")
    if volume_ratio and volume_ratio < 1:
        risks.append("缩量涨停，跟风不足")
    if sector_strength == "weak":
        risks.append("板块弱势，个股独木难支")

    # === 作战预案 ===
    if consecutive_days >= 3:
        action = "观望为主"
        action_detail = "连板高位股波动剧烈，仅适合超短选手，追高极易被套"
        entry_condition = f"若开板不破 {next_day_support:.2f} 且快速回封，可轻仓试探"
    elif consecutive_days == 2:
        action = "谨慎参与"
        action_detail = "2板确认度较高，但需看次日竞价强度"
        entry_condition = f"竞价高开3%以内可关注，回踩 {next_day_support:.2f} 不破可轻仓"
    else:
        action = "确认后参与"
        action_detail = "首板次日分化大，需竞价确认"
        entry_condition = f"竞价量达标+高开不破 {next_day_support:.2f}，回踩企稳可介入"

    # === 失效条件 ===
    invalid_conditions = []
    if next_day_support > 0:
        invalid_conditions.append(f"跌破 {next_day_support:.2f} 支撑位")
    invalid_conditions.append("竞价低开超过3%")
    if consecutive_days >= 3:
        invalid_conditions.append("开板后30分钟未回封")
    else:
        invalid_conditions.append("冲高回落跌破分时均线")

    # 组装内容
    content_parts = [
        f"**{name}({code})** 涨停{days_text} | 价格 {price:.2f}",
        f"**执行等级**: {setup_grade}",
        f"\n**📋 封板质量**: {quality_text}",
    ]
    if reason:
        content_parts.append(f"**驱动因素**: {reason}")
    if sector_strength:
        sector_map = {"strong": "🔴 强势", "medium": "🟡 中等", "weak": "🟢 弱势"}
        content_parts.append(f"**板块强度**: {sector_map.get(sector_strength, sector_strength)}")
    if confirmation_reasons:
        content_parts.append("\n**✅ 入场确认**")
        for item in confirmation_reasons[:4]:
            content_parts.append(f"• {item}")
    content_parts.append(f"\n**⚠️ 风险提示**")
    if risks:
        for r in risks:
            content_parts.append(f"• {r}")
    else:
        content_parts.append("• 封板质量尚可，注意次日竞价表现")

    content_parts.append(f"\n**🎯 作战预案**: {action}")
    content_parts.append(f"• {action_detail}")
    content_parts.append(f"• {entry_condition}")

    content_parts.append(f"\n**🛑 失效条件**")
    for ic in invalid_conditions:
        content_parts.append(f"• {ic}")

    content = "\n".join(content_parts)

    # 优先级：连板越高越紧急，但多次炸板降级
    base_priority = 8 if consecutive_days >= 3 else 6
    if open_times >= 3:
        base_priority = max(base_priority - 1, 5)  # 炸板多反而降级，提醒风险

    return PushMessage(
        title=(f"🧱 {name} ({code}) {setup_grade}｜{signal_label}" if signal_label else f"🔴 涨停异动 {name}{days_text}"),
        content=content,
        msg_type="signal",
        category="anomaly",
        stock_code=code,
        stock_name=name,
        priority=min(10, base_priority + (2 if setup_grade == "A1 可直接执行" else 1 if setup_grade == "A2 盘口确认后执行" else 0)),
        extra={"setup_grade": setup_grade},
    )


def limit_down_alert(
    code: str, name: str, price: float,
    consecutive_days: int = 1,
    volume_ratio: float = 0,
    is_st: bool = False,
    next_day_support: float = 0,
    signal_label: str = "",
    confirmation_reasons: list[str] | None = None,
    source: str = "",
    as_of: str = "",
    setup_grade: str = "B类观察候选",
    sector_factors: list[dict] | None = None,
    sector_components: list[dict] | None = None,
) -> PushMessage:
    """跌停异动"""
    days_text = f"（{consecutive_days}连跌）" if consecutive_days > 1 else ""
    confirmation_reasons = confirmation_reasons or []

    risks = []
    if is_st:
        risks.append("ST股跌停，流动性极差，不要抄底")
    if consecutive_days >= 3:
        risks.append("连续跌停，趋势性下跌，切勿接飞刀")
    if volume_ratio and volume_ratio > 3:
        risks.append("放量跌停，恐慌性抛售")

    content_parts = [
        f"**{name}({code})** 跌停{days_text} | 价格 {price:.2f}",
        f"**执行等级**: {setup_grade}",
    ]
    if volume_ratio > 0:
        content_parts.append(f"量比: {volume_ratio:.1f}")
    if confirmation_reasons:
        content_parts.append("\n**✅ 低吸确认**")
        for item in confirmation_reasons[:4]:
            content_parts.append(f"• {item}")
    sector_factor_lines = _fmt_sector_factors(sector_factors)
    if sector_factor_lines:
        content_parts.append("\n**🏷️ 主驱动板块因子**")
        content_parts.extend(sector_factor_lines)
    sector_component_lines = _fmt_sector_components(sector_components)
    if sector_component_lines:
        content_parts.append("\n**👥 主驱动板块强势股**")
        content_parts.extend(sector_component_lines)
    content_parts.append(f"\n**⚠️ 风险提示**")
    if risks:
        for r in risks:
            content_parts.append(f"• {r}")
    else:
        content_parts.append("• 仅适用于极强承接下的低吸观察，不满足条件不参与")

    if signal_label:
        content_parts.append(f"\n**🎯 作战预案**: {signal_label}")
        content_parts.append("• 仅在跌停打开后快速回抽且承接仍强时轻仓观察")
        if next_day_support > 0:
            content_parts.append(f"• 关注 {next_day_support:.2f} 一线能否形成止跌承接")
        content_parts.append(f"\n**🛑 失效条件**")
        content_parts.append("• 开板后承接迅速衰减或再次被大单压回跌停")
        content_parts.append("• 买盘撤单明显增大，低吸逻辑失效")
    else:
        content_parts.append(f"\n**🎯 作战预案**: 禁止抄底")
        content_parts.append(f"• 跌停股不参与，等待企稳信号")
        if next_day_support > 0:
            content_parts.append(f"• 关注 {next_day_support:.2f} 能否止跌")
        content_parts.append(f"\n**🛑 失效条件**: 持仓者应在开板第一时间止损")

    content = "\n".join(content_parts)

    return PushMessage(
        title=f"🪂 {name} ({code}) {setup_grade}｜{signal_label}" if signal_label else f"🟢 跌停异动 {name}{days_text}",
        content=content,
        msg_type="signal",
        category="anomaly",
        stock_code=code,
        stock_name=name,
        priority=9 if setup_grade == "A1 可直接执行" else 8 if setup_grade == "A2 盘口确认后执行" else 7,
        extra={"setup_grade": setup_grade},
    )


def capital_anomaly_alert(
    code: str, name: str, description: str,
    level: str = "major",
    net_amount: float = 0,
    price_change_pct: float = 0,
    volume_ratio: float = 0,
    price: float = 0,
    amplitude: float = 0,
    turnover: float = 0,
    volume: float = 0,
    amount: float = 0,
    open_price: float = 0,
    high_price: float = 0,
    low_price: float = 0,
    prev_close: float = 0,
    pe_ttm: float = 0,
    pb: float = 0,
    circ_market_cap: float = 0,
    avg_price: float = 0,
    net_inflow_pct: float = 0,
    super_net_inflow: float = 0,
    super_net_inflow_pct: float = 0,
    big_net_inflow: float = 0,
    big_net_inflow_pct: float = 0,
    bid_depth_5: float = 0,
    ask_depth_5: float = 0,
    orderbook_imbalance: float = 0,
    support_strength_score: float = 0,
    withdrawal_ratio: float = 0,
    source: str = "",
    as_of: str = "",
    confirmation_label: str = "",
    confirmation_reasons: list[str] | None = None,
    # 作战预案
    is_pump_and_dump: bool = False,  # 冲高回落特征
    next_day_support: float = 0,
    next_day_pressure: float = 0,
    setup_grade: str = "B类观察候选",
    sector_factors: list[dict] | None = None,
    sector_components: list[dict] | None = None,
) -> PushMessage:
    """资金异动推送 — 含冲高回落识别"""
    msg_type = "danger" if "流出" in description else "signal"
    priority = 8 if level == "critical" else 6 if level == "major" else 4
    confirmation_reasons = confirmation_reasons or []

    risks = []
    if is_pump_and_dump:
        risks.append("⚠️ 冲高回落特征！早盘拉升尾盘回落，大概率诱多")
        priority = max(priority - 1, 5)
    if "流出" in description and price_change_pct > 0:
        risks.append(f"资金净流出但股价涨{price_change_pct:.1f}%，主力对倒出货嫌疑")
    if volume_ratio > 5:
        risks.append(f"量比{volume_ratio:.1f}异常放大，可能是游资一日游")
    if net_amount < 0 and abs(net_amount) > 5e8:
        risks.append(f"资金净额 {_fmt_net_amount_yi(net_amount)}，大资金在撤退")
    signal = "买入" if net_amount >= 0 and not is_pump_and_dump else "卖出" if net_amount < 0 else "观望"
    if is_pump_and_dump:
        signal = "观望"
    plan = _plan_prices(signal if signal in {"买入", "卖出"} else "买入", price, next_day_support, next_day_pressure)
    risk_count = len(risks)
    risk_level = _risk_level("high" if amplitude >= 8 else "medium" if amplitude >= 5 else "low", risk_count)
    confidence_label = confirmation_label or ("资金确认流入" if net_amount >= 0 else "资金确认流出")
    content_parts = [
        "**📋 异动基本信息**",
        f"异动类型: {confidence_label} ｜ 执行等级: {setup_grade} ｜ 信号级别: {_priority_label(level)}",
        f"当前价格: {_fmt_price(price)} ({_fmt_pct(price_change_pct)}) ｜ 振幅: {_fmt_pct_plain(amplitude)} ｜ 换手率: {_fmt_pct_plain(turnover)}",
        "",
        "**💰 交易预案**",
        f"交易信号: {signal} ｜ 建议仓位: {_position_hint(signal, level, risk_count)}",
        f"建议入场价: {_fmt_price(plan['entry'])} ｜ 目标价格: {_fmt_price(plan['target'])} ｜ 止损价格: {_fmt_price(plan['stop'])}",
        "",
        "**📊 量价分析**",
        f"成交量: {_fmt_volume(volume)} ｜ 成交额: {_fmt_amount(amount, force_yi=True)} ｜ 量比: {volume_ratio:.2f}" if volume_ratio else f"成交量: {_fmt_volume(volume)} ｜ 成交额: {_fmt_amount(amount, force_yi=True)} ｜ 量比: --",
        f"开盘: {_fmt_price(open_price)} ｜ 最高: {_fmt_price(high_price)} ｜ 最低: {_fmt_price(low_price)} ｜ 昨收: {_fmt_price(prev_close)}",
        f"均价VWAP: {_fmt_price(avg_price)}" if avg_price else "均价VWAP: --",
        "",
        "**🧭 主力/盘口确认**",
        f"特大单: {_fmt_net_amount_yi(super_net_inflow)} ／ {_fmt_pct_plain(super_net_inflow_pct)} ｜ 大单: {_fmt_net_amount_yi(big_net_inflow)} ／ {_fmt_pct_plain(big_net_inflow_pct)}",
        f"买五总量: {_fmt_volume(bid_depth_5)} ｜ 卖五总量: {_fmt_volume(ask_depth_5)}",
        f"盘口失衡: {_fmt_ratio_pct(orderbook_imbalance)} ｜ 承接强度: {support_strength_score:.0f} ｜ 撤单比: {_fmt_ratio_pct(withdrawal_ratio)}",
        "",
        "**💎 估值指标**",
        (
            f"市盈率(PE): {pe_ttm:.2f} ｜ 市净率(PB): {pb:.2f} ｜ 流通市值: {_fmt_market_cap_billion(circ_market_cap)}"
            if pe_ttm and pb and circ_market_cap
            else f"市盈率(PE): {pe_ttm:.2f}" if pe_ttm
            else "市盈率(PE): --"
        ),
    ]
    if not (pe_ttm and pb and circ_market_cap):
        if pb or circ_market_cap:
            content_parts.append(
                f"市净率(PB): {pb:.2f} ｜ 流通市值: {_fmt_market_cap_billion(circ_market_cap)}"
                if pb and circ_market_cap
                else (f"市净率(PB): {pb:.2f}" if pb else f"流通市值: {_fmt_market_cap_billion(circ_market_cap)}" if circ_market_cap else "流通市值: --")
            )
    sector_factor_lines = _fmt_sector_factors(sector_factors)
    if sector_factor_lines:
        content_parts.extend(["", "**🏷️ 主驱动板块因子**", *sector_factor_lines])
    sector_component_lines = _fmt_sector_components(sector_components)
    if sector_component_lines:
        content_parts.extend(["", "**👥 主驱动板块强势股**", *sector_component_lines])
    content_parts.extend(
        [
            "",
            "**🎯 推荐理由**",
            f"资金流向得分: {_priority_label(level)} / {description}",
        ]
    )
    source_label = _fund_source_label(source)
    source_method = _fund_source_method(source)
    if source_label or source_method:
        content_parts.extend(
            [
                f"资金来源: {source_label}",
                f"口径: {source_method or '--'}",
            ]
        )
    if volume_ratio >= 2:
        content_parts.append(f"量比{volume_ratio:.2f}，成交量明显放大")
    elif volume_ratio >= 1.3:
        content_parts.append(f"量比{volume_ratio:.2f}，量能温和放大")
    if net_inflow_pct:
        content_parts.append(
            f"资金净额占成交额比 {net_inflow_pct:.1f}%，{'资金面偏强' if net_inflow_pct > 0 else '资金面偏弱'}"
        )
    if confirmation_reasons:
        for reason in confirmation_reasons[:5]:
            content_parts.append(f"• {reason}")
    if risks:
        content_parts.append(f"注意: 存在{len(risks)}个风险提示")
    content_parts.extend(
        [
            "",
            "**⚠️ 风险提示**",
        ]
    )
    if risks:
        content_parts.extend([f"• {_risk_text(risk)}" for risk in risks[:4]])
    else:
        content_parts.append("• 资金流入持续性待确认，注意次日承接")
    content_parts.extend(
        [
            f"综合风险: {risk_level}",
            f"波动率风险: {'高风险' if amplitude >= 8 else '中风险' if amplitude >= 5 else '低风险'}",
            f"建议仓位: {'轻仓观察(10-20%)' if signal == '买入' else '回避/减仓'}",
        ]
    )
    content = "\n".join(content_parts)

    title = f"📈 {name} ({code}) {setup_grade}｜买入信号"

    return PushMessage(
        title=title,
        content=content,
        msg_type=msg_type,
        category="anomaly",
        stock_code=code,
        stock_name=name,
        priority=min(10, priority + (2 if setup_grade == "A1 可直接执行" else 1 if setup_grade == "A2 盘口确认后执行" else 0)),
        extra={"setup_grade": setup_grade},
    )


def breakthrough_alert(
    code: str, name: str, description: str,
    strength: str = "moderate",
    # 突破质量参数
    breakthrough_price: float = 0,
    volume_ratio: float = 0,
    ma_status: str = "",  # 均线排列: multi_long/long/mixed/short
    days_near_pressure: int = 0,  # 在压力位附近盘整天数
    quality_score: float = 0,
    current_price: float = 0,
    change_pct: float = 0,
    amplitude: float = 0,
    turnover: float = 0,
    volume: float = 0,
    amount: float = 0,
    open_price: float = 0,
    high_price: float = 0,
    low_price: float = 0,
    prev_close: float = 0,
    pe_ttm: float = 0,
    pb: float = 0,
    circ_market_cap: float = 0,
    # 作战预案
    is_false_breakout: bool = False,  # 假突破特征
    pullback_probability: str = "",  # 回踩概率: high/medium/low
    next_day_support: float = 0,
    next_day_pressure: float = 0,
    stop_loss_price: float = 0,
    signal_variant: str = "",
    signal_label: str = "",
    confirmation_reasons: list[str] | None = None,
    source: str = "",
    as_of: str = "",
    setup_grade: str = "B类观察候选",
    sector_factors: list[dict] | None = None,
    sector_components: list[dict] | None = None,
) -> PushMessage:
    """突破信号推送 — 含真假突破判断 + 回踩预案"""
    risks = []
    confirmation_reasons = confirmation_reasons or []
    is_repair_signal = signal_variant in {
        "sector_repair_reversal",
        "old_hot_oversold_repair",
    }
    if is_false_breakout:
        risks.append("⚠️ 假突破特征！缩量突破/突破后回落，诱多嫌疑极大")
    if volume_ratio and volume_ratio < 1.5 and not is_repair_signal:
        risks.append(f"量比仅{volume_ratio:.1f}，突破无量，回踩概率大")
    if ma_status in ("mixed", "short"):
        risks.append("均线未多头排列，突破有效性存疑")
    if days_near_pressure > 0 and days_near_pressure < 3:
        risks.append(f"压力位仅盘整{days_near_pressure}天，蓄势不足")

    # 突破有效性判断
    raw_quality_score = quality_score
    quality_score = 0
    if is_repair_signal and raw_quality_score >= 82:
        quality_score += 3
    if volume_ratio >= 2:
        quality_score += 2
    elif volume_ratio >= 1.5:
        quality_score += 1
    if ma_status in ("multi_long", "long"):
        quality_score += 2
    if days_near_pressure >= 5:
        quality_score += 1
    if strength == "strong":
        quality_score += 1

    quality_label = {5: "🟢 高质量", 4: "🟢 高质量", 3: "🟡 待确认", 2: "🟡 待确认", 1: "🔴 存疑", 0: "🔴 存疑"}.get(quality_score, "🟡 待确认")

    # 作战预案
    if is_false_breakout or quality_score <= 1:
        action = "不参与"
        action_detail = "突破信号存疑，等待回踩确认或放弃"
        entry_condition = "暂不介入"
    elif is_repair_signal:
        action = "盘口确认后轻仓"
        action_detail = "属于超跌首日修复，不追涨停；回踩VWAP或修复支撑不破再轻仓"
        entry_condition = (
            f"回踩 {next_day_support:.2f} 附近不破且盘口承接延续可轻仓"
            if next_day_support > 0
            else "回踩VWAP不破且盘口承接延续可轻仓"
        )
    elif quality_score >= 4:
        action = "回踩确认后介入"
        action_detail = "高质量突破，但不要追，等回踩确认支撑"
        entry_condition = f"回踩 {next_day_support:.2f} 附近企稳可介入" if next_day_support > 0 else "回踩突破位企稳可介入"
    else:
        action = "观察为主"
        action_detail = "突破有效性待确认，先观察次日走势"
        entry_condition = f"次日站稳 {breakthrough_price:.2f} 以上可轻仓试探" if breakthrough_price > 0 else "次日站稳突破价以上可关注"

    plan = _plan_prices("买入", current_price or breakthrough_price, next_day_support, next_day_pressure or breakthrough_price)
    content_parts = [
        "**📋 异动基本信息**",
        f"异动类型: 突破关注信号 ｜ 执行等级: {setup_grade} ｜ 信号级别: {_priority_label('critical' if strength == 'strong' and quality_score >= 80 else 'major')}",
        f"当前价格: {_fmt_price(current_price or breakthrough_price)} ({_fmt_pct(change_pct)}) ｜ 振幅: {_fmt_pct_plain(amplitude)} ｜ 换手率: {_fmt_pct_plain(turnover)}",
        "",
        "**💰 交易预案**",
        f"交易信号: {'关注' if action == '观察为主' else '买入'} ｜ 建议仓位: {'10%' if action == '观察为主' else '15%'}",
        f"建议入场价: {_fmt_price(plan['entry'])} ｜ 目标价格: {_fmt_price(plan['target'])} ｜ 止损价格: {_fmt_price(stop_loss_price or plan['stop'])}",
        "",
        "**📊 量价分析**",
        f"成交量: {_fmt_volume(volume)} ｜ 成交额: {_fmt_amount(amount, force_yi=True)} ｜ 量比: {volume_ratio:.2f}" if volume_ratio else f"成交量: {_fmt_volume(volume)} ｜ 成交额: {_fmt_amount(amount, force_yi=True)} ｜ 量比: --",
        f"开盘: {_fmt_price(open_price)} ｜ 最高: {_fmt_price(high_price)} ｜ 最低: {_fmt_price(low_price)} ｜ 昨收: {_fmt_price(prev_close)}",
        "",
        "**💎 估值指标**",
        (
            f"市盈率(PE): {pe_ttm:.2f} ｜ 市净率(PB): {pb:.2f} ｜ 流通市值: {_fmt_market_cap_billion(circ_market_cap)}"
            if pe_ttm and pb and circ_market_cap
            else f"市盈率(PE): {pe_ttm:.2f}" if pe_ttm
            else "市盈率(PE): --"
        ),
    ]
    if not (pe_ttm and pb and circ_market_cap):
        if pb or circ_market_cap:
            content_parts.append(
                f"市净率(PB): {pb:.2f} ｜ 流通市值: {_fmt_market_cap_billion(circ_market_cap)}"
                if pb and circ_market_cap
                else (f"市净率(PB): {pb:.2f}" if pb else f"流通市值: {_fmt_market_cap_billion(circ_market_cap)}" if circ_market_cap else "流通市值: --")
            )
    sector_factor_lines = _fmt_sector_factors(sector_factors)
    if sector_factor_lines:
        content_parts.extend(["", "**🏷️ 主驱动板块因子**", *sector_factor_lines])
    sector_component_lines = _fmt_sector_components(sector_components)
    if sector_component_lines:
        content_parts.extend(["", "**👥 主驱动板块强势股**", *sector_component_lines])
    content_parts.extend(
        [
            "",
            "**🎯 推荐理由**",
            f"突破质量: {quality_label}",
            f"信号描述: {description}",
        ]
    )
    source_label = _fund_source_label(source)
    source_method = _fund_source_method(source)
    if source_label or source_method:
        content_parts.extend(
            [
                f"资金来源: {source_label}",
                f"口径: {source_method or '--'}",
            ]
        )
    if confirmation_reasons:
        content_parts.extend([f"• {reason}" for reason in confirmation_reasons[:5]])
    if ma_status:
        ma_map = {"multi_long": "均线多头排列", "long": "均线偏多", "mixed": "均线缠绕", "short": "均线偏空"}
        content_parts.append(f"均线状态: {ma_map.get(ma_status, ma_status)}")
    if pullback_probability:
        pb_map = {"high": "回踩概率高", "medium": "回踩概率中等", "low": "回踩概率低"}
        content_parts.append(f"回踩预判: {pb_map.get(pullback_probability, pullback_probability)}")
    if days_near_pressure:
        content_parts.append(f"压力位附近盘整: {days_near_pressure}天")

    content_parts.extend(["", "**⚠️ 风险提示**"])
    if risks:
        content_parts.extend([f"• {_risk_text(risk)}" for risk in risks[:4]])
    else:
        content_parts.append("• 突破后追高容易回撤，优先等待回踩确认。")
    content_parts.extend(
        [
            f"综合风险: {_risk_level('high' if amplitude >= 8 else 'medium' if amplitude >= 5 else 'low', len(risks))}",
            f"波动率风险: {'高风险' if amplitude >= 8 else '中风险' if amplitude >= 5 else '低风险'}",
            f"交易策略: {action}，{action_detail}",
        ]
    )
    content = "\n".join(content_parts)

    return PushMessage(
        title=f"🚀 {name} ({code}) {setup_grade}｜{signal_label}" if signal_label else f"🚀 {name} ({code}) 突破关注",
        content=content,
        msg_type="signal",
        category="anomaly",
        stock_code=code,
        stock_name=name,
        priority=(8 if strength == "strong" and quality_score >= 4 else 6) + (2 if setup_grade == "A1 可直接执行" else 1 if setup_grade == "A2 盘口确认后执行" else 0),
        extra={"setup_grade": setup_grade},
    )


def b1_state_alert(
    code: str,
    name: str,
    signal_label: str,
    signal_status: str,
    transition: str,
    current_price: float = 0,
    day_change_pct: float = 0,
    turnover_rate: float = 0,
    main_net_inflow: float = 0,
    j_value: float | None = None,
    rsi_value: float | None = None,
    short_score: float | None = None,
    long_score: float | None = None,
    hold_score: int | None = None,
    success_rate_3d: float | None = None,
    success_rate_5d: float | None = None,
    success_samples: int = 0,
    trend_white_price: float | None = None,
    is_break_trend: bool = False,
    setup_grade: str = "",
    setup_track: str = "",
    primary_reason: str = "",
    blockers: list[str] | None = None,
    previous_signal_label: str = "",
) -> PushMessage:
    """B1 状态机推送 — 盘中预判 / 收盘确认 / 信号失效。"""
    blockers = blockers or []
    status_labels = {
        "intraday_preview": "盘中预判",
        "close_confirmed": "收盘确认",
        "invalidated": "信号失效",
    }
    state_icons = {
        "intraday_preview": "🟠",
        "close_confirmed": "✅",
        "invalidated": "⚠️",
    }
    state_label = status_labels.get(signal_status, signal_status or "状态更新")
    title = f"{state_icons.get(signal_status, '📌')} {name} ({code}) {state_label}｜{signal_label or 'B1'}"
    alert_tier = {
        "intraday_preview": "b1_preview",
        "close_confirmed": "b1_confirmed",
        "invalidated": "b1_invalidated",
    }.get(signal_status, "b1_state")

    if signal_status == "invalidated":
        content_parts = [
            f"**{name}({code})** {signal_label or 'B1'} 已失效",
            f"**状态变化**: {state_label}",
        ]
        if previous_signal_label and previous_signal_label != signal_label:
            content_parts.append(f"上一状态信号: {previous_signal_label}")
        if primary_reason:
            content_parts.append(f"**触发背景**: {primary_reason}")
        if blockers:
            content_parts.extend(["", "**🛑 失效原因**", *[f"• {item}" for item in blockers[:4]]])
        if trend_white_price:
            content_parts.append(f"趋势白线参考: {_fmt_price(trend_white_price)}")
        if current_price:
            content_parts.append(f"当前价格: {_fmt_price(current_price)} ({_fmt_pct(day_change_pct)})")
        content = "\n".join(content_parts)
        return PushMessage(
            title=title,
            content=content,
            msg_type="warning",
            category="anomaly",
            stock_code=code,
            stock_name=name,
            priority=6,
            extra={
                "signal_status": signal_status,
                "transition": transition,
                "signal_label": signal_label,
                "alert_tier": alert_tier,
            },
        )

    transition_label = {
        "appeared": "首次触发",
        "confirmed": "由盘中预判升级为收盘确认",
        "signal_changed": "信号形态发生变化",
    }.get(transition, "状态更新")
    content_parts = [
        f"**{name}({code})** {signal_label or 'B1'}",
        f"**状态变化**: {state_label} · {transition_label}",
        f"**执行背景**: {setup_grade or '未评级'}" + (f" · {setup_track}" if setup_track else ""),
        f"当前价格: {_fmt_price(current_price)} ({_fmt_pct(day_change_pct)}) ｜ 换手率: {_fmt_pct_plain(turnover_rate)}",
        f"主力净额: {_fmt_net_amount_yi(main_net_inflow)}",
        "",
        "**📊 信号刻画**",
        f"J / RSI: {j_value:.2f} / {rsi_value:.2f}" if j_value is not None and rsi_value is not None else "J / RSI: -- / --",
        (
            f"短期 / 长期分数: {short_score:.2f} / {long_score:.2f}"
            if short_score is not None and long_score is not None
            else "短期 / 长期分数: -- / --"
        ),
        f"持股分数: {hold_score if hold_score is not None else '--'} ｜ 趋势白线: {_fmt_price(trend_white_price)}",
        "",
        "**📈 历史验证**",
        f"3日修复率: {_fmt_ratio_pct(success_rate_3d) if success_rate_3d is not None else '--'} ｜ 5日修复率: {_fmt_ratio_pct(success_rate_5d) if success_rate_5d is not None else '--'}",
        f"历史样本: {success_samples or 0} ｜ 趋势状态: {'跌破趋势白线' if is_break_trend else '未破趋势'}",
    ]
    if primary_reason:
        content_parts.extend(["", "**🎯 异动背景**", f"• {primary_reason}"])
    if blockers:
        content_parts.extend(["", "**⚠️ 观察点**", *[f"• {item}" for item in blockers[:4]]])
    content = "\n".join(content_parts)

    priority = 8 if signal_status == "close_confirmed" else 7
    return PushMessage(
        title=title,
        content=content,
        msg_type="signal",
        category="anomaly",
        stock_code=code,
        stock_name=name,
        priority=priority,
        extra={
            "signal_status": signal_status,
            "transition": transition,
            "signal_label": signal_label,
            "setup_grade": setup_grade,
            "alert_tier": alert_tier,
        },
    )


def bull_stock_alert(
    code: str, name: str, score: float, level: str,
    top_signals: list,
    # 风险上下文
    current_price: float = 0,
    price_position: str = "",  # 位置: bottom/middle/top/new_high
    days_from_low: int = 0,  # 距低点天数
    profit_rate_from_low: float = 0,  # 从低点涨幅
    # 作战预案
    next_day_support: float = 0,
    stop_loss_price: float = 0,
    suggested_position: str = "",  # 建议仓位: light/medium/heavy
) -> PushMessage:
    """牛股评分推送 — 含位置判断 + 仓位建议"""
    signals_text = "\n".join(f"• {s}" for s in top_signals[:5])

    # === 位置风险评估 ===
    position_risks = []
    if price_position == "new_high":
        position_risks.append("📍 新高位置，追高风险极大，等回调")
    elif price_position == "top":
        position_risks.append("📍 高位区域，获利盘丰厚，回调压力大")
    if profit_rate_from_low > 50:
        position_risks.append(f"已从低点涨{profit_rate_from_low:.0f}%，追高性价比低")
    if days_from_low > 0 and days_from_low < 5 and profit_rate_from_low > 20:
        position_risks.append(f"仅{days_from_low}天涨{profit_rate_from_low:.0f}%，短线过热")

    # === 仓位建议 ===
    if not suggested_position:
        if level in ("S", "A") and price_position in ("bottom", "middle"):
            suggested_position = "medium"
        elif level in ("S", "A"):
            suggested_position = "light"
        elif level == "B":
            suggested_position = "light"
        else:
            suggested_position = "observe"

    position_map = {
        "heavy": "3/4仓",
        "medium": "半仓",
        "light": "1/3仓",
        "observe": "观察，不建仓",
    }

    content_parts = [
        f"**{name}({code})** 综合评分: **{score}** [{level}级]",
        f"\n**核心信号**:\n{signals_text}",
    ]

    if current_price > 0:
        content_parts.append(f"\n**当前位置**: {current_price:.2f}")
        pos_map = {"bottom": "底部区域🟢", "middle": "中部区域🟡", "top": "高位区域🔴", "new_high": "创新高🔴"}
        if price_position:
            content_parts.append(f"位置判断: {pos_map.get(price_position, price_position)}")

    content_parts.append(f"\n**⚠️ 风险提示**")
    if position_risks:
        for r in position_risks:
            content_parts.append(f"• {r}")
    else:
        content_parts.append("• 评分高不等于立即买入，需等合适买点")

    content_parts.append(f"\n**🎯 作战预案**")
    content_parts.append(f"• 建议仓位: {position_map.get(suggested_position, suggested_position)}")
    if price_position in ("bottom", "middle"):
        content_parts.append(f"• 低位可逢低分批建仓")
    else:
        content_parts.append(f"• 高位不宜追涨，等回踩支撑确认")
    if next_day_support > 0:
        content_parts.append(f"• 关注支撑: {next_day_support:.2f}")
    if stop_loss_price > 0:
        content_parts.append(f"• 止损位: {stop_loss_price:.2f}")

    content_parts.append(f"\n**🛑 失效条件**")
    content_parts.append(f"• 评分从{level}级降至C级以下")
    if stop_loss_price > 0:
        content_parts.append(f"• 跌破止损位 {stop_loss_price:.2f}")
    content_parts.append(f"• 核心信号(如龙头地位)丧失")

    content = "\n".join(content_parts)

    # S级高位降优先级（防诱多）
    priority = 9 if level in ("S", "A") and price_position in ("bottom", "middle") else 6

    return PushMessage(
        title=f"🐂 牛股雷达 {name} [{level}级]",
        content=content,
        msg_type="signal",
        category="anomaly",
        stock_code=code,
        stock_name=name,
        priority=priority,
    )
