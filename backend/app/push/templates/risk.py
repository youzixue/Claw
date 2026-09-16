"""风控预警推送模板

v2: 增加具体应对方案 + 时间节点
"""

from app.push.channels.base import PushMessage


def risk_block_alert(code: str, name: str, reasons: list) -> PushMessage:
    """风控拦截预警"""
    reasons_text = "\n".join(f"• {r['message']}" for r in reasons)

    return PushMessage(
        title=f"🚨 风控拦截 {name}",
        content=f"**{name}({code})** 被风控拦截\n\n{reasons_text}\n\n**📋 应对**: 该标的不符合买入条件，请勿手动绕过风控",
        msg_type="danger",
        category="risk",
        stock_code=code,
        stock_name=name,
        priority=9,
    )


def risk_warning_alert(code: str, name: str, warnings: list) -> PushMessage:
    """风控警告 — 含应对建议"""
    warning_details = []
    for w in warnings:
        detail = f"• {w['message']}"
        # 根据警告类型给建议
        if "解禁" in w.get('message', ''):
            detail += "\n  → 应对: 解禁前后3天减仓或回避"
        elif "集中度" in w.get('message', ''):
            detail += "\n  → 应对: 分散持仓，单行业不超40%"
        elif "仓位" in w.get('message', ''):
            detail += "\n  → 应对: 减仓至风控线以下"
        elif "情绪" in w.get('message', ''):
            detail += "\n  → 应对: 控制仓位，不宜追高"
        warning_details.append(detail)

    details_text = "\n".join(warning_details)

    return PushMessage(
        title=f"⚠️ 风控警告 {name}",
        content=f"**{name}({code})** 风控警告\n\n{details_text}",
        msg_type="warning",
        category="risk",
        stock_code=code,
        stock_name=name,
        priority=6,
    )


def drawdown_alert(current_drawdown: float, limit: float,
                   portfolio_value: float = 0,
                   peak_value: float = 0) -> PushMessage:
    """回撤熔断预警 — 含具体操作建议"""
    loss_amount = peak_value - portfolio_value if peak_value and portfolio_value else 0

    content_parts = [
        f"当前回撤 **{current_drawdown:.1f}%**，超过熔断线 **{limit:.1f}%**",
    ]
    if loss_amount > 0:
        content_parts.append(f"从峰值回撤金额: ¥{loss_amount:,.0f}")

    content_parts.extend([
        f"\n**🚨 紧急操作**",
        f"1. 立即停止所有新开仓",
        f"2. 将总仓位降至30%以下",
        f"3. 优先卖出亏损最大标的",
        f"4. 回撤收窄至{limit * 0.8:.1f}%以下再考虑加仓",
        f"\n**🛑 解除条件**: 回撤收窄至{limit * 0.7:.1f}%以下自动解除熔断",
    ])

    return PushMessage(
        title="🚨 回撤熔断",
        content="\n".join(content_parts),
        msg_type="danger",
        category="risk",
        priority=10,
    )


def lockup_warning(code: str, name: str, lockup_ratio: float,
                   lockup_date: str, risk_level: str,
                   lockup_type: str = "",  # 首发原股东/定增/股权激励
                   lockup_shares: float = 0,  # 解禁股数
                   avg_cost: float = 0,  # 解禁方成本
                   current_price: float = 0,  # 当前价格
                   ) -> PushMessage:
    """解禁预警 — 含影响评估和应对"""
    risk_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk_level, "⚪")

    content_parts = [
        f"**{name}({code})**",
        f"解禁日期: {lockup_date}",
        f"解禁占比: {lockup_ratio:.1f}%",
        f"风险等级: {risk_emoji} {risk_level}",
    ]

    if lockup_type:
        content_parts.append(f"解禁类型: {lockup_type}")
    if lockup_shares > 0:
        content_parts.append(f"解禁股数: {lockup_shares/1e8:.2f}亿股")

    # 解禁方盈亏分析
    if avg_cost > 0 and current_price > 0:
        profit_rate = (current_price - avg_cost) / avg_cost * 100
        content_parts.append(f"解禁方成本: {avg_cost:.2f} (当前{'盈利' if profit_rate > 0 else '亏损'}{abs(profit_rate):.0f}%)")
        if profit_rate > 50:
            content_parts.append("⚠️ 解禁方浮盈超50%，减持意愿强烈")

    # 时间轴应对
    content_parts.append(f"\n**📋 应对时间轴**")
    content_parts.append(f"• 解禁前5天: 减仓至半仓以下")
    content_parts.append(f"• 解禁前2天: 清仓或降至1/3仓")
    content_parts.append(f"• 解禁日: 观察是否出减持公告")
    content_parts.append(f"• 解禁后3天: 无减持公告可考虑回补")

    return PushMessage(
        title=f"🔓 解禁预警 {name}",
        content="\n".join(content_parts),
        msg_type="warning",
        category="risk",
        stock_code=code,
        stock_name=name,
        priority=7 if risk_level == "high" else 5,
    )


def sentiment_circuit_alert(cycle: str, score: float,
                            suggestion: str,
                            # 新增：具体仓位上限
                            max_position_pct: float = 0,
                            forbidden_actions: list = None,
                            allowed_actions: list = None,
                            ) -> PushMessage:
    """情绪熔断预警 — 含仓位上限和禁止操作"""
    cycle_map = {
        "freezing": "🧊 冰点期",
        "recovery": "🌱 修复期",
        "divergence": "⚡ 分歧期",
        "climax": "🔥 亢奋期",
    }

    # 各周期的具体指引
    cycle_guide = {
        "freezing": {
            "forbidden": ["禁止新开仓", "禁止抄底", "禁止加仓"],
            "allowed": ["减仓", "止损", "观望"],
            "position_limit": "≤20%",
        },
        "climax": {
            "forbidden": ["禁止追涨停", "禁止加仓", "禁止满仓操作"],
            "allowed": ["减仓锁利", "设紧止损", "调仓换股"],
            "position_limit": "≤50%",
        },
        "recovery": {
            "forbidden": ["禁止重仓追涨"],
            "allowed": ["轻仓试探", "分批建仓", "跟随资金"],
            "position_limit": "≤60%",
        },
        "divergence": {
            "forbidden": ["禁止满仓", "禁止追高"],
            "allowed": ["均衡配置", "高抛低吸", "控制仓位"],
            "position_limit": "≤80%",
        },
    }

    guide = cycle_guide.get(cycle, {})
    if not forbidden_actions:
        forbidden_actions = guide.get("forbidden", [])
    if not allowed_actions:
        allowed_actions = guide.get("allowed", [])
    if not max_position_pct:
        limit_str = guide.get("position_limit", "")
        max_position_pct = float(limit_str.replace("≤", "").replace("%", "")) if "≤" in limit_str else 0

    content_parts = [
        f"情绪评分: **{score:.0f}/100**",
        f"当前周期: {cycle_map.get(cycle, cycle)}",
        f"操作建议: {suggestion}",
    ]

    if max_position_pct > 0:
        content_parts.append(f"仓位上限: **≤{max_position_pct:.0f}%**")

    content_parts.append(f"\n**🚫 禁止操作**")
    for f in forbidden_actions:
        content_parts.append(f"• {f}")

    content_parts.append(f"\n**✅ 允许操作**")
    for a in allowed_actions:
        content_parts.append(f"• {a}")

    content_parts.append(f"\n**🛑 周期转换条件**")
    if cycle == "freezing":
        content_parts.append(f"• 涨停数>30且跌停数<10 → 转入修复期")
    elif cycle == "recovery":
        content_parts.append(f"• 连续2日涨跌比>2:1 → 可加仓")
        content_parts.append(f"• 再次出现跌停潮 → 回到冰点期")
    elif cycle == "divergence":
        content_parts.append(f"• 涨停>80 → 转入亢奋期（注意风险）")
        content_parts.append(f"• 跌停>30 → 转入冰点期")
    elif cycle == "climax":
        content_parts.append(f"• 涨停数骤减+跌停增加 → 转入分歧期，减仓")

    return PushMessage(
        title=f"🌡️ 情绪熔断 {cycle_map.get(cycle, cycle)}",
        content="\n".join(content_parts),
        msg_type="warning" if cycle in ("freezing", "climax") else "info",
        category="risk",
        priority=8 if cycle == "freezing" else 6,
    )
