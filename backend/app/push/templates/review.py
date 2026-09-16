"""4时段复盘推送模板

v2: 增加操作建议 + 风险提醒 + 明日策略

时段:
1. 盘前(8:30) — 隔夜外盘+要闻+竞价预判+今日策略
2. 午盘(11:35) — 半日行情+资金+情绪+午后策略
3. 盘后(15:10) — 全日复盘+涨停+资金+板块+明日预判
4. 晚间(20:00) — 深度复盘+牛股+风险+操作计划
"""

from app.push.channels.base import PushMessage


def morning_review(
    global_signals: list = None,
    key_news: list = None,
    auction_preview: dict = None,
    # 新增：今日策略
    today_strategy: str = "",
    watch_list: list = None,  # 今日关注 [{"code":"600519","name":"茅台","reason":"突破20日线","action":"回踩轻仓"}]
    risk_alerts: list = None,  # 风险提醒 ["解禁:宁德时代", "ST:*ST金钰"]
) -> PushMessage:
    """盘前复盘(8:30)"""
    parts = []

    if global_signals:
        parts.append("**🌍 隔夜外盘**")
        for s in global_signals[:5]:
            parts.append(f"• {s}")

    if key_news:
        parts.append("\n**📰 重要资讯**")
        for n in key_news[:5]:
            parts.append(f"• {n}")

    if auction_preview:
        parts.append("\n**🔮 竞价预判**")
        parts.append(f"• {auction_preview.get('summary', '待更新')}")
        if auction_preview.get('hot_sectors'):
            parts.append(f"• 竞价热点: {', '.join(auction_preview['hot_sectors'][:3])}")

    if risk_alerts:
        parts.append("\n**⚠️ 今日风险提醒**")
        for r in risk_alerts[:5]:
            parts.append(f"• {r}")

    if watch_list:
        parts.append("\n**👀 今日关注**")
        for w in watch_list[:5]:
            action_emoji = {"回踩轻仓": "🟡", "突破追入": "🟢", "观望不追": "🔴"}.get(w.get("action", ""), "⚪")
            parts.append(f"• {action_emoji} {w.get('name', '')}({w.get('code', '')}) — {w.get('reason', '')} | {w.get('action', '观望')}")

    if today_strategy:
        parts.append(f"\n**🎯 今日策略**\n{today_strategy}")

    content = "\n".join(parts) if parts else "盘前数据待更新"

    return PushMessage(
        title="🌅 盘前复盘",
        content=content,
        msg_type="info",
        category="review",
        priority=6,
    )


def midday_review(
    market_summary: dict = None,
    fund_flow: dict = None,
    sentiment: dict = None,
    # 新增
    afternoon_strategy: str = "",
    trap_warnings: list = None,  # 冲高回落预警 ["XX股份 冲高5%后回落至1%，疑似诱多"]
    position_advice: str = "",  # 仓位建议
) -> PushMessage:
    """午盘复盘(11:35)"""
    parts = []

    if market_summary:
        parts.append("**📊 半日行情**")
        parts.append(f"• 涨跌比: {market_summary.get('advance_decline', '-')}")
        parts.append(f"• 成交额: {market_summary.get('amount', '-')}")
        if market_summary.get('index_change'):
            parts.append(f"• 指数: {market_summary['index_change']}")

    if fund_flow:
        parts.append("\n**💰 资金动向**")
        parts.append(f"• 东财主力资金净额: {fund_flow.get('main_net', '-')}")
        if fund_flow.get('top_inflow'):
            parts.append(f"• 流入前三: {', '.join(fund_flow['top_inflow'][:3])}")
        if fund_flow.get('top_outflow'):
            parts.append(f"• 流出前三: {', '.join(fund_flow['top_outflow'][:3])}")

    if sentiment:
        parts.append("\n**🌡️ 情绪面**")
        parts.append(f"• 情绪周期: {sentiment.get('cycle', '-')}")
        parts.append(f"• 涨停: {sentiment.get('limit_up_count', '-')}")
        if sentiment.get('score'):
            parts.append(f"• 情绪评分: {sentiment['score']}/100")

    if trap_warnings:
        parts.append("\n**🚨 冲高回落预警**")
        for w in trap_warnings[:3]:
            parts.append(f"• {w}")

    if position_advice:
        parts.append(f"\n**📊 仓位建议**: {position_advice}")

    if afternoon_strategy:
        parts.append(f"\n**🎯 午后策略**\n{afternoon_strategy}")

    content = "\n".join(parts) if parts else "午盘数据待更新"

    return PushMessage(
        title="🕛 午盘复盘",
        content=content,
        msg_type="info",
        category="review",
        priority=5,
    )


def closing_review(
    market_summary: dict = None,
    limit_ups: list = None,
    fund_flow: dict = None,
    sector_rotation: list = None,
    board_height: dict = None,
    # 新增
    tomorrow_strategy: str = "",
    trap_summary: list = None,  # 今日诱多总结
    limit_up_quality: dict = None,  # 涨停质量 {"solid": 15, "loose": 8, "broken": 5}
    key_observations: list = None,  # 关键观察点
) -> PushMessage:
    """盘后复盘(15:10)"""
    parts = []

    if market_summary:
        parts.append("**📊 全日行情**")
        parts.append(f"• 涨跌比: {market_summary.get('advance_decline', '-')}")
        parts.append(f"• 成交额: {market_summary.get('amount', '-')}")
        if market_summary.get('index_change'):
            parts.append(f"• 指数: {market_summary['index_change']}")

    if board_height:
        parts.append("\n**🏔️ 连板高度**")
        parts.append(f"• 最高: {board_height.get('height', '-')}板")
        if board_height.get('ladder'):
            parts.append(f"• 梯队: {board_height['ladder']}")

    if limit_up_quality:
        parts.append("\n**📐 涨停质量**")
        parts.append(f"• 封死涨停: {limit_up_quality.get('solid', '-')}只")
        parts.append(f"• 烂板: {limit_up_quality.get('loose', '-')}只")
        parts.append(f"• 炸板: {limit_up_quality.get('broken', '-')}只")
        quality_ratio = limit_up_quality.get('solid', 0) / max(sum(limit_up_quality.values()), 1)
        if quality_ratio < 0.5:
            parts.append(f"• ⚠️ 封板率仅{quality_ratio:.0%}，市场分歧大，次日低开概率高")

    if limit_ups:
        parts.append("\n**🔴 涨停池**")
        for s in limit_ups[:10]:
            parts.append(f"• {s}")

    if trap_summary:
        parts.append("\n**🚨 今日诱多总结**")
        for t in trap_summary[:5]:
            parts.append(f"• {t}")

    if sector_rotation:
        parts.append("\n**🔄 板块轮动**")
        for s in sector_rotation[:5]:
            parts.append(f"• {s}")

    if fund_flow:
        parts.append("\n**💰 资金动向**")
        parts.append(f"• 东财主力资金净额: {fund_flow.get('main_net', '-')}")

    if key_observations:
        parts.append("\n**👁️ 关键观察**")
        for o in key_observations[:3]:
            parts.append(f"• {o}")

    if tomorrow_strategy:
        parts.append(f"\n**🎯 明日策略**\n{tomorrow_strategy}")

    content = "\n".join(parts) if parts else "盘后数据待更新"

    return PushMessage(
        title="📉 盘后复盘",
        content=content,
        msg_type="info",
        category="review",
        priority=7,
    )


def evening_review(
    market_summary: dict = None,
    bull_stocks: list = None,
    risk_warnings: list = None,
    tomorrow_preview: str = "",
    # 新增
    operation_plan: dict = None,  # 操作计划 {"buy_watch": [...], "sell_watch": [...], "position_advice": ""}
    key_events: list = None,  # 明日关键事件 ["美联储议息", "XX解禁"]
    taboo_list: list = None,  # 明日禁忌 ["不追涨停", "不加仓高位股"]
) -> PushMessage:
    """晚间深度复盘(20:00)"""
    parts = []

    if market_summary:
        parts.append("**📊 今日回顾**")
        parts.append(f"• 涨跌比: {market_summary.get('advance_decline', '-')}")
        parts.append(f"• 情绪周期: {market_summary.get('sentiment', '-')}")

    if bull_stocks:
        parts.append("\n**🐂 牛股雷达**")
        for s in bull_stocks[:5]:
            parts.append(f"• {s}")

    if risk_warnings:
        parts.append("\n**⚠️ 风险提示**")
        for w in risk_warnings[:5]:
            parts.append(f"• {w}")

    if key_events:
        parts.append("\n**📅 明日关键事件**")
        for e in key_events[:5]:
            parts.append(f"• {e}")

    if operation_plan:
        parts.append("\n**📋 明日操作计划**")
        if operation_plan.get("buy_watch"):
            parts.append("  买入观察:")
            for b in operation_plan["buy_watch"][:3]:
                parts.append(f"  • {b}")
        if operation_plan.get("sell_watch"):
            parts.append("  卖出观察:")
            for s in operation_plan["sell_watch"][:3]:
                parts.append(f"  • {s}")
        if operation_plan.get("position_advice"):
            parts.append(f"  仓位: {operation_plan['position_advice']}")

    if taboo_list:
        parts.append("\n**🚫 明日禁忌**")
        for t in taboo_list[:3]:
            parts.append(f"• {t}")

    if tomorrow_preview:
        parts.append(f"\n**🔮 明日预判**\n{tomorrow_preview}")

    content = "\n".join(parts) if parts else "晚间数据待更新"

    return PushMessage(
        title="🌙 晚间深度复盘",
        content=content,
        msg_type="info",
        category="review",
        priority=7,
    )
