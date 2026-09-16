"""Comparison plus isolated execution for A/B/C/C3/D/F2 challengers.

Only immutable forward ``PaperShadowEvent(event_type='confirmed')`` rows may enter
the account-backed routes.  Orders are hard-wired to the local ``paper`` broker
and never share cash, positions, NAV or execution logs with the A-F Champion
accounts.  C3 has no account and is surfaced only as a forward-evidence ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, time
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.price_limit_rules import price_limit_rule
from app.models.paper import (
    PaperAutoTradeLog,
    PaperControlSample,
    PaperNav,
    PaperShadowEvaluation,
    PaperShadowEvent,
    PaperTradeLog,
)
from app.models.stock import StockSpot
from app.models.trading import TradeOrder
from app.paper.account_policy import account_entry_exit_policy, challenger_execution_policy, route_signal_policy
from app.paper.experiment import experiment_activation_at
from app.paper.momentum_retest_shadow import (
    MOMENTUM_LIQUIDITY_CONTRACT_VERSION, momentum_liquidity_gate_issues,
)


# Only the original single-frame route is known to have inadmissible entry
# evidence.  Later versioned positions remain auditable holdings: a route upgrade
# freezes new fills/add-ons, but must not turn the next opening quote into a
# synthetic liquidation signal.
_UNSAFE_POSITION_VERSION_PREFIXES = ("abcdef_shape_v1:",)


ROUTE_META = {
    "momentum_first_retest": {
        "label": "策略A2 · 强势股首次回踩",
        "hypothesis": "3%~6%强势股首次回踩后重新站上VWAP，并获价格、成交与盘口共同确认",
    },
    "b_weak_open_second_board": {
        "label": "策略B2 · 负开弱转强",
        "hypothesis": "昨日首板，今日弱开后收复零轴与日内成交均价线",
    },
    "c_recent_limit_relaunch": {
        "label": "策略C2 · 涨停记忆再启动",
        "hypothesis": "近期低板记忆，短暂调整后从水下或零轴重新走强",
    },
    "c3_mainline_first_board": {
        "label": "策略C3 · 主线首板盘中确认",
        "hypothesis": "活跃主线板块内无近期涨停记忆的首板候选，经可成交、相对强度与连续多帧确认后是否具备前向优势",
        "base_account_name": "mainline",
        "evidence_only": True,
    },
    "d_auction_recovery": {
        "label": "策略D2 · 竞价恢复",
        "hypothesis": "真实早期竞价偏弱，09:25最终竞价显著回收后再经盘中确认",
    },
    "f2_highboard_break_reclaim": {
        "label": "策略F2 · 高标断板收复",
        "hypothesis": "高标断板1至3个交易日后，从水下或零轴重新收复",
    },
}


def _nav_payload(rows: list[PaperNav]) -> list[dict[str, Any]]:
    return [
        {
            "date": row.trade_date.isoformat(),
            "nav": row.nav,
            "daily_return": row.daily_return,
        }
        for row in rows
    ]


async def _account_return_breakdown(
    db: AsyncSession, account: Any, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only lifetime and daily reporting, never a trading/risk input."""
    from datetime import timedelta
    from app.api.v1 import paper
    from app.core.trade_calendar import TRADE_SESSIONS

    now = now or paper._paper_now()
    payload = paper._account_payload(account)
    capital = _safe_float(payload.get("initial_capital"))
    assets = _safe_float(payload.get("total_assets"))
    stats = payload.get("trade_stats")
    stats = stats if isinstance(stats, dict) else {}
    total_pnl = (
        _safe_float(assets - capital)
        if assets is not None and assets >= 0 and capital is not None and capital > 0
        else None
    )
    realized_pnl = _safe_float(stats.get("total_pnl"))
    # The legacy aggregate silently skips unknown sell PnL. Do not represent
    # that partial sum as the lifetime realized result, and do not rebuild trades.
    if realized_pnl is not None:
        with db.no_autoflush:
            sell_pnls = (await db.scalars(select(PaperTradeLog.realized_pnl).where(
                PaperTradeLog.account_id == account.id,
                PaperTradeLog.trade_type == "sell",
            ))).all()
        if any(_safe_float(value) is None for value in sell_pnls):
            realized_pnl = None
    result = {
        "total_pnl": round(total_pnl, 2) if total_pnl is not None else None,
        "realized_pnl": realized_pnl,
        # _account_payload has a legacy zero fallback; missing is unknown here.
        "fees_paid": _safe_float(getattr(account, "fee_drag", None)),
        "daily_pnl": None,
        "daily_return_pct": None,
        "daily_trade_date": None,
        "daily_status": "invalid_data",
        "daily_note": "本金或总资产无效，无法计算今日收益",
    }
    calendar = await paper._nav_reporting_calendar(db, now.date() - timedelta(days=40), now.date())
    if not await calendar.is_trade_day(now.date()):
        result.update(daily_status="non_trading_day", daily_note="非交易日，不展示今日收益")
        return result
    if now.time() < TRADE_SESSIONS["morning"][0]:
        result.update(daily_status="before_open", daily_note="尚未开盘，不展示今日收益")
        return result
    result["daily_trade_date"] = now.date().isoformat()
    if capital is None or capital <= 0 or assets is None or assets < 0:
        return result
    previous_date = await calendar.previous_trade_day(now.date())
    with db.no_autoflush:
        previous = await db.scalar(select(PaperNav).where(
            PaperNav.account_id == account.id, PaperNav.trade_date == previous_date,
        ))
        if previous is None:
            result.update(daily_status="missing_previous_nav", daily_note="缺少上一交易日实际净值；首次开户不以本金或更早记录冒充昨资产")
            return result
        previous_nav = _safe_float(previous.nav)
        if previous_nav is None or previous_nav <= 0:
            result.update(daily_note="上一交易日净值无效，无法计算今日收益")
            return result
        positions = await paper._open_positions(db, account.id)
        for position in positions:
            spot = await db.scalar(select(StockSpot).where(StockSpot.code == position.code))
            quote_at = getattr(spot, "source_quote_at", None)
            price = _safe_float(getattr(spot, "price", None))
            if (
                price is None or price <= 0 or not isinstance(quote_at, datetime)
                or quote_at.date() != now.date()
                or quote_at.replace(tzinfo=None) > now.replace(tzinfo=None)
            ):
                result.update(daily_status="stale_valuation", daily_note="持仓行情缺失、价格无效或报价日期陈旧，暂不展示今日收益")
                return result
    previous_assets = capital * previous_nav
    if not math.isfinite(previous_assets) or previous_assets <= 0:
        result.update(daily_note="上一交易日资产无效，无法计算今日收益")
        return result
    daily_pnl = assets - previous_assets
    daily_return = daily_pnl / previous_assets * 100
    if not math.isfinite(daily_pnl) or not math.isfinite(daily_return):
        return result
    result.update(
        daily_pnl=round(daily_pnl, 2),
        daily_return_pct=round(daily_return, 6),
        daily_status="ok",
        daily_note="相对上一交易日实际净值估算；历史净值精度6位，金额可能有分位误差；费用已含在收益内，不重复扣除",
    )
    return result


def _common_period_comparison(
    champion_rows: list[PaperNav],
    challenger_rows: list[PaperNav],
) -> dict[str, Any]:
    """仅在双方拥有同一日期净值后，才计算可比观察期收益。"""

    champion_by_date = {
        row.trade_date: _safe_float(row.nav)
        for row in champion_rows
        if _safe_float(row.nav) is not None and _safe_float(row.nav) > 0
    }
    challenger_by_date = {
        row.trade_date: _safe_float(row.nav)
        for row in challenger_rows
        if _safe_float(row.nav) is not None and _safe_float(row.nav) > 0
    }
    common_dates = sorted(set(champion_by_date) & set(challenger_by_date))
    result: dict[str, Any] = {
        "comparable": len(common_dates) >= 2,
        "start_date": common_dates[0].isoformat() if common_dates else None,
        "end_date": common_dates[-1].isoformat() if common_dates else None,
        "session_count": len(common_dates),
        "champion_return_pct": None,
        "challenger_return_pct": None,
        "return_delta_pct": None,
        "note": "至少需要两个共同净值日期，才能比较同一观察期收益",
    }
    if len(common_dates) < 2:
        return result
    first_date = common_dates[0]
    last_date = common_dates[-1]
    champion_base = champion_by_date[first_date]
    challenger_base = challenger_by_date[first_date]
    if not champion_base or not challenger_base:
        return result
    champion_return = (champion_by_date[last_date] / champion_base - 1.0) * 100.0
    challenger_return = (challenger_by_date[last_date] / challenger_base - 1.0) * 100.0
    result.update({
        "champion_return_pct": round(champion_return, 4),
        "challenger_return_pct": round(challenger_return, 4),
        "return_delta_pct": round(challenger_return - champion_return, 4),
        "note": "按双方首个共同净值日归一化后比较，不混用不同成立日期",
    })
    return result


async def _current_version_execution_summary(
    db: AsyncSession,
    *,
    account_id: int,
    strategy_version: str,
    open_positions: list[Any],
) -> dict[str, Any]:
    """Summarize only trades tagged with the current immutable route version.

    Account NAV and drawdown remain lifetime, cross-version facts. This compact
    ledger prevents those values from being mistaken for current-rule results.
    Safety exits of unversioned or explicitly unsafe positions are reported
    across their original versions rather than charged to the new version's PnL.
    """

    legacy_exit_marker = "旧版或未标版本仓位隔离退出"
    rows = list(
        (
            await db.scalars(
                select(PaperTradeLog)
                .where(
                    PaperTradeLog.account_id == account_id,
                    or_(
                        PaperTradeLog.strategy_version == strategy_version,
                        and_(
                            PaperTradeLog.trade_type == "sell",
                            PaperTradeLog.reason.contains(legacy_exit_marker),
                        ),
                    ),
                )
                .order_by(PaperTradeLog.trade_time, PaperTradeLog.id)
            )
        ).all()
    )
    forced_legacy_exits = [
        row
        for row in rows
        if row.trade_type == "sell" and legacy_exit_marker in str(row.reason or "")
    ]
    forced_exit_ids = {row.id for row in forced_legacy_exits}
    scoped_rows = [
        row
        for row in rows
        if str(row.strategy_version or "") == strategy_version
        and row.id not in forced_exit_ids
    ]
    buys = [row for row in scoped_rows if row.trade_type == "buy"]
    sells = [row for row in scoped_rows if row.trade_type == "sell"]
    commissions = sum(float(row.commission or 0.0) for row in scoped_rows)
    realized_pnl = sum(float(row.realized_pnl or 0.0) for row in sells)
    timestamps = [row.trade_time for row in scoped_rows if row.trade_time is not None]
    return {
        "strategy_version": strategy_version,
        "trade_count": len(scoped_rows),
        "buy_trade_count": len(buys),
        "sell_trade_count": len(sells),
        "open_position_count": sum(
            str(getattr(position, "strategy_version", "") or "") == strategy_version
            for position in open_positions
        ),
        "realized_pnl": round(realized_pnl, 2),
        "commission": round(commissions, 2),
        "forced_legacy_exit_count": len(forced_legacy_exits),
        "first_trade_at": min(timestamps).isoformat() if timestamps else None,
        "latest_trade_at": max(timestamps).isoformat() if timestamps else None,
        "scope": "仅当前strategy_version标记的成交；未标版本/已知不合格版本的安全退出跨版本单列",
    }


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _signal_token(event_key: str, route_id: str) -> str:
    route_token = {
        "momentum_first_retest": "a2",
        "b_weak_open_second_board": "b2",
        "c_recent_limit_relaunch": "c2",
        "d_auction_recovery": "d2",
        "f2_highboard_break_reclaim": "f2",
    }.get(route_id, "cx")
    digest = hashlib.sha1(event_key.encode("utf-8")).hexdigest()[:16]
    return f"chlg-{route_token}-{digest}"


def _run_id(event_key: str) -> str:
    return f"challenger-{hashlib.sha1(event_key.encode('utf-8')).hexdigest()[:20]}"


def _entry_policy(route_id: str) -> dict[str, float | int]:
    account_name = {
        "momentum_first_retest": "challenger_a",
        "b_weak_open_second_board": "challenger_b",
        "c_recent_limit_relaunch": "challenger_c",
        "d_auction_recovery": "challenger_d",
        "f2_highboard_break_reclaim": "challenger_f2",
    }[route_id]
    policy = account_entry_exit_policy(account_name)
    return {
        "position_pct": float(policy["position_pct"]),
        "stop_loss_pct": float(policy["stop_loss_pct"]),
    }


def _parse_hhmm(value: Any, fallback: time) -> time:
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        return time(int(hour_text), int(minute_text[:2]))
    except (TypeError, ValueError):
        return fallback


def _route_entry_not_before(route_id: str) -> time:
    configured = {
        "momentum_first_retest": settings.PAPER_CHALLENGER_A_ENTRY_NOT_BEFORE,
        "b_weak_open_second_board": settings.PAPER_CHALLENGER_B_ENTRY_NOT_BEFORE,
        "c_recent_limit_relaunch": settings.PAPER_CHALLENGER_C_ENTRY_NOT_BEFORE,
        "d_auction_recovery": settings.PAPER_CHALLENGER_D_ENTRY_NOT_BEFORE,
        "f2_highboard_break_reclaim": settings.PAPER_CHALLENGER_F2_ENTRY_NOT_BEFORE,
    }.get(route_id)
    return _parse_hhmm(configured, time(9, 35))


def _route_auto_order_enabled(
    route_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Resolve route approval through its isolated account and experiment clock."""
    if not settings.PAPER_CHALLENGER_ACCOUNT_ENABLED or not settings.PAPER_AUTO_TRADE_ENABLED:
        return False
    from app.api.v1 import paper

    account_name = paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.get(route_id)
    return bool(
        account_name
        and paper._strategy_auto_order_enabled(account_name, now=now)
    )


def _route_shadow_version(route_id: str) -> str:
    """Signal/evidence versions are independent from account execution versions."""
    if route_id == "momentum_first_retest":
        return str(settings.PAPER_MOMENTUM_RETEST_SHADOW_VERSION)
    from app.paper.strategy_iteration_shadow import route_version_for

    return str(route_version_for(route_id))


def _route_reclaim_bounds(route_id: str) -> tuple[float, float]:
    return {
        "b_weak_open_second_board": (
            settings.PAPER_STRATEGY_B_RECLAIM_MIN_PCT,
            settings.PAPER_STRATEGY_B_RECLAIM_MAX_PCT,
        ),
        "c_recent_limit_relaunch": (
            settings.PAPER_STRATEGY_C_RELAUNCH_MIN_RECLAIM_PCT,
            settings.PAPER_STRATEGY_C_RELAUNCH_MAX_RECLAIM_PCT,
        ),
        "d_auction_recovery": (
            settings.PAPER_STRATEGY_D_AUCTION_RECLAIM_MIN_PCT,
            settings.PAPER_STRATEGY_D_AUCTION_RECLAIM_MAX_PCT,
        ),
        "f2_highboard_break_reclaim": (
            settings.PAPER_STRATEGY_F2_MIN_RECLAIM_PCT,
            settings.PAPER_STRATEGY_F2_MAX_RECLAIM_PCT,
        ),
    }[route_id]


def _event_priority(
    event: PaperShadowEvent,
    snapshot: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    """Rank only with leaves frozen at confirmation time; never use outcomes."""

    quote = snapshot.get("quote") if isinstance(snapshot.get("quote"), dict) else {}
    prior = (
        snapshot.get("prior_structure")
        if isinstance(snapshot.get("prior_structure"), dict)
        else {}
    )
    price = _safe_float(quote.get("price")) or _safe_float(event.price, 0.0) or 0.0
    avg_price = _safe_float(quote.get("avg_price"), 0.0) or 0.0
    high = _safe_float(quote.get("high"), 0.0) or 0.0
    prev_close = _safe_float(quote.get("prev_close"), 0.0) or 0.0
    change_pct = (
        (price / prev_close - 1.0) * 100.0
        if price > 0 and prev_close > 0
        else 0.0
    )
    confirmation_metrics = (
        prior.get("confirmation_metrics")
        if isinstance(prior.get("confirmation_metrics"), dict)
        else {}
    )
    relative_strength = (
        _safe_float(confirmation_metrics.get("relative_strength_pct"), 0.0) or 0.0
    )
    vwap_slope = _safe_float(confirmation_metrics.get("vwap_slope_pct"), 0.0) or 0.0
    volume_ratio = _safe_float(quote.get("volume_ratio"), 0.0) or 0.0
    orderbook = _safe_float(quote.get("orderbook_imbalance"), 0.0) or 0.0
    vwap_premium = (
        max((price / avg_price - 1.0) * 100.0, 0.0)
        if price > 0 and avg_price > 0
        else 0.0
    )
    pullback = (
        max((high - price) / high * 100.0, 0.0)
        if high > 0 and price > 0 and high >= price
        else (route_signal_policy(event.route_id)["max_pullback_from_high_pct"]
              if event.route_id != "momentum_first_retest" else 2.0)
    )
    consecutive = (
        _safe_float(prior.get("last_consecutive_days"))
        or _safe_float(prior.get("previous_consecutive_days"))
        or 0.0
    )
    break_sessions = _safe_float(prior.get("break_sessions_before_today"), 0.0) or 0.0
    components = {
        "change": min(max(change_pct, 0.0), 4.0) * 4.0,
        "relative_strength": min(max(relative_strength, 0.0), 4.0) * 3.0,
        "vwap": min(vwap_premium, 3.0) * 3.0,
        "vwap_slope": min(max(vwap_slope, 0.0), 1.5) * 3.0,
        "volume": min(max(volume_ratio, 0.0), 3.0) * 4.0,
        "orderbook": min(max(orderbook, -0.2), 2.0) * 3.0,
        "high_retention": max(2.0 - pullback, 0.0) * 4.0,
        "prior_board": min(max(consecutive, 0.0), 5.0) * 2.0,
        "break_penalty": -min(max(break_sessions, 0.0), 5.0) * 1.5,
    }
    score = 50.0 + sum(components.values())
    return round(score, 4), {
        key: round(value, 4) for key, value in components.items()
    }


def _live_route_confirmation_valid(
    event: PaperShadowEvent,
    snapshot: dict[str, Any],
    spot: StockSpot,
    *,
    audit: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Revalidate strength and fillability against the latest quote."""

    price = _safe_float(getattr(spot, "price", None))
    prev_close = _safe_float(getattr(spot, "prev_close", None))
    avg_price = _safe_float(getattr(spot, "avg_price", None))
    high = _safe_float(getattr(spot, "high", None))
    ask = _safe_float(getattr(spot, "ask1_price", None))
    ask_volume = _safe_float(getattr(spot, "ask1_volume", None))
    volume_ratio = _safe_float(getattr(spot, "volume_ratio", None))
    orderbook = _safe_float(getattr(spot, "orderbook_imbalance", None))
    is_momentum = event.route_id == "momentum_first_retest"
    rules = snapshot.get("rule_snapshot") if isinstance(snapshot.get("rule_snapshot"), dict) else {}
    min_volume = (_safe_float(rules.get("min_volume_ratio"), settings.PAPER_MOMENTUM_RETEST_MIN_VOLUME_RATIO)
                  if is_momentum else route_signal_policy(event.route_id)["min_volume_ratio"])
    min_orderbook = (_safe_float(rules.get("min_orderbook_imbalance"), settings.PAPER_MOMENTUM_RETEST_MIN_ORDERBOOK_IMBALANCE)
                     if is_momentum else route_signal_policy(event.route_id)["min_orderbook_imbalance"])
    if (
        price is None
        or price <= 0
        or prev_close is None
        or prev_close <= 0
        or avg_price is None
        or avg_price <= 0
    ):
        return False, "暂缺有效价格/昨收/均价证据，等待新轮次"
    if price < avg_price:
        return False, "执行前已跌破实时成交均价线，原确认信号失效"
    if ask is None or ask <= 0 or ask_volume is None or ask_volume <= 0:
        return False, "暂缺可成交卖一价格或数量，不把排队当成交"
    if is_momentum:
        liquidity_issues = momentum_liquidity_gate_issues(
            {key: getattr(spot, key, None) for key in (
                "volume_ratio", "amount", "orderbook_imbalance", "withdrawal_ratio",
            )}, rules,
        )
        if audit is not None:
            audit["execution_liquidity_contract"] = MOMENTUM_LIQUIDITY_CONTRACT_VERSION
            audit["execution_liquidity_issues"] = liquidity_issues
        if liquidity_issues:
            return False, liquidity_issues[0]["reason"]
    if volume_ratio is None:
        return False, "暂缺执行前量比证据"
    if volume_ratio < min_volume:
        return False, "执行前量比已低于自身策略确认门槛"
    if orderbook is None:
        return False, "暂缺执行前盘口失衡证据"
    if orderbook < min_orderbook:
        return False, "执行前盘口失衡已低于自身策略确认门槛"
    if is_momentum:
        state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
        peak = _safe_float(state.get("peak_price"))
        if peak is None or peak <= 0:
            return False, "首次回踩确认事件缺少不可变跟踪峰值，不能以日内高点替代"
        max_gap = _safe_float(rules.get("max_peak_gap_pct"), settings.PAPER_MOMENTUM_RETEST_MAX_PEAK_GAP_PCT)
        if (peak / price - 1) * 100 > max_gap:
            return False, "执行前已离开首次回踩的跟踪峰值确认区间"
    else:
        if high is None or high <= 0 or high < price:
            return False, "暂缺有效日内高点证据"
        pullback_pct = (high - price) / high * 100.0
        if pullback_pct > route_signal_policy(event.route_id)["max_pullback_from_high_pct"]:
            return False, f"执行前较日内高点回撤{pullback_pct:.2f}%，持续性失效"
    # Never trust a potentially stale/rounded upstream percentage when the live
    # price and previous close can deterministically reproduce it.
    change_pct = (price / prev_close - 1.0) * 100.0
    if event.route_id == "momentum_first_retest":
        rules = snapshot.get("rule_snapshot") if isinstance(snapshot.get("rule_snapshot"), dict) else {}
        lower = _safe_float(rules.get("candidate_min_change_pct"), 3.0) or 3.0
        upper = _safe_float(rules.get("candidate_max_change_pct"), 6.0) or 6.0
        # 量比/累计成交额/盘口/撤单统一使用上面的流动性契约，不再单独回退0值规则。
    else:
        lower, upper = _route_reclaim_bounds(event.route_id)
    if not lower <= change_pct <= upper:
        return False, f"执行前重算涨跌幅{change_pct:.2f}%已离开路由确认区间"

    if event.route_id == "d_auction_recovery":
        prior = (
            snapshot.get("prior_structure")
            if isinstance(snapshot.get("prior_structure"), dict)
            else {}
        )
        if (
            prior.get("auction_volume_path_verified") is not True
            or prior.get("cancel_phase_verified") is not True
        ):
            return False, "D2缺少09:20至09:25不可撤单阶段及最终竞价的可验证正量路径证据"
    return True, ""


def _event_candidate(event: PaperShadowEvent, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_key": event.event_key,
        "route_id": event.route_id,
        "route_version": event.route_version,
        "signal_trade_date": event.trade_date.isoformat(),
        "signal_time": event.observed_at.isoformat(),
        "point_in_time_only": True,
        "real_order_connected": False,
        "rule_snapshot": snapshot.get("rule_snapshot") or {},
        "prior_structure": snapshot.get("prior_structure") or {},
    }


async def _already_processed(
    db: AsyncSession,
    *,
    account_id: int,
    account_name: str,
    run_id: str,
    signal_token: str,
    quote_round_id: str,
) -> bool:
    """Terminal/pending orders are immutable; technical waits retry next round."""
    submitted_order_id = await db.scalar(
        select(TradeOrder.id).where(
            TradeOrder.broker == "paper",
            TradeOrder.account_id == account_name,
            TradeOrder.signal_id == signal_token,
            TradeOrder.side == "buy",
            TradeOrder.status.in_(("pending", "submitted", "partial", "filled", "canceled", "risk_blocked")),
        ).limit(1)
    )
    if submitted_order_id is not None:
        # Covers a crash after submit_order committed but before its audit log.
        return True
    trade_id = await db.scalar(
        select(PaperTradeLog.id).where(
            PaperTradeLog.account_id == account_id,
            PaperTradeLog.signal_id == signal_token,
        ).limit(1)
    )
    if trade_id is not None:
        return True
    logs = list((await db.scalars(
        select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.account_id == account_id,
            PaperAutoTradeLog.run_id == run_id,
        ).order_by(PaperAutoTradeLog.id.desc())
    )).all())
    terminal_actions = {"buy", "deferred_buy", "skip_terminal", "dry_run"}
    for row in logs:
        if (
            row.executed_trade_id is not None
            or row.action in terminal_actions
            or row.decision in {"dry_run", "skipped"}
        ):
            return True
        logged_round_id = str(row.quote_round_id or "")
        if not logged_round_id:
            logged_round_id = str(
                _json_dict(row.candidate_json).get("decision_round_id") or ""
            )
        if row.decision == "wait" and logged_round_id == quote_round_id:
            return True
    return False


async def _record_terminal_log(
    db: AsyncSession,
    *,
    paper: Any,
    account_id: int,
    event: PaperShadowEvent,
    run_id: str,
    action: str,
    decision: str,
    reason: str,
    price: float | None,
    amount: int | None,
    candidate: dict[str, Any],
    risk: dict[str, Any] | None = None,
    executed_trade_id: int | None = None,
) -> PaperAutoTradeLog:
    row = await paper._add_auto_log(
        db,
        account_id=account_id,
        run_id=run_id,
        trade_date=event.trade_date,
        trigger="challenger-shadow-confirmed",
        source=event.route_id,
        code=event.code,
        name=event.name or event.code,
        action=action,
        decision=decision,
        reason=reason,
        price=price,
        amount=amount,
        risk_level=str((risk or {}).get("final_level") or ""),
        risk=risk,
        candidate=candidate,
        executed_trade_id=executed_trade_id,
    )
    await db.commit()
    return row


def _conservative_entry_price(
    *,
    event: PaperShadowEvent,
    snapshot: dict[str, Any],
    spot: StockSpot,
) -> tuple[float | None, str]:
    quote = snapshot.get("quote") if isinstance(snapshot.get("quote"), dict) else {}
    event_fill = _safe_float(event.assumed_fill_price)
    price = _safe_float(getattr(spot, "price", None))
    ask = _safe_float(getattr(spot, "ask1_price", None))
    ask_volume = _safe_float(getattr(spot, "ask1_volume", None))
    prev_close = _safe_float(getattr(spot, "prev_close", None)) or _safe_float(quote.get("prev_close"))
    if event_fill is None or event_fill <= 0:
        return None, "confirmed事件缺少可审计的假设成交价"
    if (
        price is None
        or price <= 0
        or ask is None
        or ask <= 0
        or ask_volume is None
        or ask_volume <= 0
    ):
        return None, "当前卖一价格或卖一量不可成交，禁止为隔离候选策略虚构买入"
    slippage = max(_safe_float(settings.PAPER_EXECUTION_SLIPPAGE_PCT, 0.0) or 0.0, 0.0)
    live_fill = max(price, ask) * (1.0 + slippage / 100.0)
    entry_price = round(max(event_fill, live_fill), 4)
    drift_pct = (entry_price / event_fill - 1.0) * 100.0
    if drift_pct > challenger_execution_policy(event.route_id)["max_entry_drift_pct"]:
        return None, f"确认后价格漂移{drift_pct:.2f}%超过隔离候选策略上限"

    limit_up = _safe_float(getattr(spot, "limit_up", None)) or _safe_float(quote.get("limit_up"))
    if (limit_up is None or limit_up <= 0) and prev_close and prev_close > 0:
        rule = price_limit_rule(event.code, name=event.name, trade_date=event.trade_date)
        limit_up = round(prev_close * (1.0 + rule.nominal_limit_pct / 100.0), 2)
    if limit_up is not None and limit_up > 0 and entry_price >= limit_up - 0.005:
        return None, "已到涨停或缺少可成交卖盘，隔离候选策略不把排队当成交"
    return entry_price, ""


async def _legacy_challenger_exit_reasons(
    db: AsyncSession,
    *,
    account_id: int,
    position_versions: dict[str, str],
    expected_strategy_version: str,
) -> dict[str, str]:
    """Quarantine only unversioned or explicitly unsafe Challenger positions.

    A normal immutable route upgrade must freeze old queued fills and prevent
    cross-version add-ons, but the holding keeps its original version and follows
    the account's ordinary sell rules.  Forcing every superseded version out at
    the next opening quote bypasses the opening-noise guard and creates a
    deterministic sell-low path.
    """

    codes = {str(code) for code in position_versions if str(code)}
    if not codes:
        return {}
    buys = list(
        (
            await db.scalars(
                select(PaperTradeLog)
                .where(
                    PaperTradeLog.account_id == account_id,
                    PaperTradeLog.trade_type == "buy",
                    PaperTradeLog.code.in_(codes),
                )
                .order_by(
                    PaperTradeLog.code,
                    PaperTradeLog.trade_time.desc(),
                    PaperTradeLog.id.desc(),
                )
            )
        ).all()
    )
    latest_by_code: dict[str, PaperTradeLog] = {}
    for row in buys:
        latest_by_code.setdefault(str(row.code), row)

    reasons: dict[str, str] = {}
    for code in codes:
        row = latest_by_code.get(code)
        recorded_version = str(position_versions.get(code) or "")
        if not recorded_version and row is not None:
            recorded_version = str(getattr(row, "strategy_version", "") or "")
        if recorded_version == expected_strategy_version:
            continue
        explicitly_unsafe = any(
            recorded_version.startswith(prefix)
            for prefix in _UNSAFE_POSITION_VERSION_PREFIXES
        )
        if recorded_version and not explicitly_unsafe:
            # Keep the immutable entry version on the holding.  The executor has
            # no scale-in path for an already-held code, while the shared paper
            # broker separately rejects stale queued fills, so liquidation is
            # neither required nor a valid substitute for version isolation.
            continue
        unsafe_label = (
            "known_unsafe_single_frame"
            if explicitly_unsafe
            else "legacy_unversioned"
        )
        reasons[code] = (
            "旧版或未标版本仓位隔离退出："
            f"持仓版本={recorded_version or unsafe_label}，"
            f"当前版本={expected_strategy_version}；"
            "仅未标版本或已知不合格单帧版本强制退出"
        )
    return reasons


async def _today_buy_count(db: AsyncSession, account_id: int, trade_date: date) -> int:
    start = datetime.combine(trade_date, time.min)
    end = datetime.combine(trade_date, time.max)
    return int(
        await db.scalar(
            select(func.count(func.distinct(PaperTradeLog.code))).where(
                PaperTradeLog.account_id == account_id,
                PaperTradeLog.trade_type == "buy",
                PaperTradeLog.trade_time >= start,
                PaperTradeLog.trade_time <= end,
            )
        )
        or 0
    )


async def run_strategy_iteration_challenger_accounts(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    event_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Execute fresh confirmed events in isolated paper accounts and manage exits.

    The function is idempotent per shadow event.  It intentionally refuses stale
    confirmations, sealed limit-ups, absent ask quotes and all warn/block risk
    outcomes.  No non-paper broker can be selected by callers.
    """

    from app.api.v1 import paper
    from app.trading.service import SubmitOrderCommand, submit_order

    now = now or datetime.now()
    if not settings.PAPER_CHALLENGER_ACCOUNT_ENABLED:
        return {"enabled": False, "entries": 0, "sells": 0, "skipped": 0, "blocked": 0}
    order_window_ok, order_window_reason = await paper._paper_order_window_status(now)
    if not order_window_ok:
        return {
            "enabled": True,
            "entries": 0,
            "sells": 0,
            "skipped": 1,
            "blocked": 0,
            "reason": order_window_reason,
        }

    sell_logs: list[PaperAutoTradeLog] = []
    route_by_account = {
        account_name: route_id
        for route_id, account_name in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.items()
    }
    # A2/B2/C2/D2/F2均由confirmed事件驱动并在此管理退出；E2由独立高标主循环管理。
    for account_name, route_id in route_by_account.items():
        account = await paper._get_or_create_account(db, account_name)
        account = await paper._refresh_account(db, account)
        open_positions = await paper._open_positions(db, account.id)
        current_strategy_version = paper._strategy_version(account_name)
        forced_exit_reasons = await _legacy_challenger_exit_reasons(
            db,
            account_id=account.id,
            position_versions={
                str(position.code): str(
                    getattr(position, "strategy_version", "") or ""
                )
                for position in open_positions
            },
            expected_strategy_version=current_strategy_version,
        )
        sell_logs.extend(
            await paper._run_auto_sells(
                db,
                account=account,
                run_id=f"challenger-exit-{now.strftime('%Y%m%d%H%M%S')}-{account_name[-2:]}",
                trade_date=now.date(),
                trigger="challenger-account-cycle",
                execute=True,
                order_strategy_id="paper-challenger-forward",
                order_signal_prefix="chlg-exit",
                order_source=route_id,
                forced_exit_reason_by_code=forced_exit_reasons,
                quote_now=now,
            )
        )
    await db.commit()

    quote_context = paper._quote_round_context()
    quote_round_id = str(quote_context.get("round_id") or "")
    scan_round_id = quote_round_id or now.strftime("%Y%m%d%H%M%S")
    execution_round_id = quote_round_id or scan_round_id
    for route_id, account_name in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.items():
        account = await paper._get_or_create_account(db, account_name)
        strategy_version = paper._strategy_version(account_name)
        heartbeat_exists = await db.scalar(
            select(PaperAutoTradeLog.id).where(
                PaperAutoTradeLog.account_id == account.id,
                PaperAutoTradeLog.strategy_version == strategy_version,
                PaperAutoTradeLog.quote_round_id == (quote_round_id or None),
                PaperAutoTradeLog.run_id == f"scan-{scan_round_id[:24]}-{account_name[-2:]}",
            ).limit(1)
        )
        if heartbeat_exists is None:
            await paper._add_auto_log(
                db,
                account_id=account.id,
                strategy_version=strategy_version,
                run_id=f"scan-{scan_round_id[:24]}-{account_name[-2:]}",
                trade_date=now.date(),
                trigger="challenger-account-cycle",
                source=route_id,
                action="scan",
                decision="wait",
                reason="本轮已扫描前向confirmed事件；无信号也保留运行证据",
                candidate={
                    "scan_heartbeat": True, "quote_round_id": quote_round_id,
                    "runtime_evidence_version": "account_scan_v1",
                    "global_auto_enabled": bool(settings.PAPER_AUTO_TRADE_ENABLED),
                    "account_auto_buy_enabled": bool(_route_auto_order_enabled(route_id, now=now)),
                    "order_window_open": bool(order_window_ok),
                    "buy_attempt_allowed": bool(_route_auto_order_enabled(route_id, now=now)
                        and account.status == "active"
                        and paper._is_intraday_buy_window(now, start_value=_route_entry_not_before(route_id).strftime("%H:%M"))),
                },
                stage_code="runtime_scan", reason_code="account_scan_entered",
                created_at=now,
            )
    await db.commit()

    route_versions = {
        route_id: _route_shadow_version(route_id)
        for route_id in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
    }
    version_scope = or_(*(
        and_(
            PaperShadowEvent.route_id == route_id,
            PaperShadowEvent.route_version == route_version,
        )
        for route_id, route_version in route_versions.items()
    ))
    query = select(PaperShadowEvent).where(
        version_scope,
        PaperShadowEvent.event_type == "confirmed",
        PaperShadowEvent.trade_date == now.date(),
        PaperShadowEvent.observed_at <= now,
        # A same-round scan may finish after the round's decision clock. Wait for
        # a later visible round instead of submitting/tombstoning that event early.
        PaperShadowEvent.created_at <= now,
    )
    if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED:
        activation_at = experiment_activation_at()
        if activation_at is None:
            query = query.where(False)
        else:
            query = query.where(PaperShadowEvent.observed_at >= activation_at)
    normalized_keys = [str(key) for key in (event_keys or []) if str(key)]
    if normalized_keys:
        query = query.where(PaperShadowEvent.event_key.in_(normalized_keys))
    events = list(
        (
            await db.scalars(
                query.order_by(
                    PaperShadowEvent.route_id,
                    PaperShadowEvent.code,
                    PaperShadowEvent.observed_at,
                    PaperShadowEvent.id,
                )
            )
        ).all()
    )
    ranked_events: list[
        tuple[PaperShadowEvent, dict[str, Any], dict[str, Any], float]
    ] = []
    for event in events:
        snapshot = _json_dict(event.snapshot_json)
        candidate = _event_candidate(event, snapshot)
        priority_score, priority_components = _event_priority(event, snapshot)
        candidate["pre_entry_priority_score"] = priority_score
        candidate["pre_entry_priority_components"] = priority_components
        candidate["ranking_is_point_in_time_only"] = True
        ranked_events.append((event, snapshot, candidate, priority_score))
    ranked_events.sort(
        key=lambda item: (
            str(item[0].route_id),
            -item[3],
            item[0].observed_at,
            str(item[0].code),
            int(item[0].id or 0),
        )
    )
    rank_by_route: dict[str, int] = defaultdict(int)
    for ranked_event, _snapshot, ranked_candidate, _score in ranked_events:
        route_id = str(ranked_event.route_id)
        rank_by_route[route_id] += 1
        ranked_candidate["pre_entry_rank_within_route"] = rank_by_route[route_id]

    entries = 0
    skipped = 0
    blocked = 0
    deferred = 0
    for event, snapshot, candidate, _priority_score in ranked_events:
        # A prior definite rejection rolls back/expires the shared identity map.
        await paper._refresh_expired_paper_rows(db, event)
        account_name = paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.get(event.route_id)
        if not account_name:
            continue
        account = await paper._get_or_create_account(db, account_name)
        run_id = _run_id(event.event_key)
        signal_token = _signal_token(event.event_key, event.route_id)
        candidate["decision_round_id"] = execution_round_id
        if await _already_processed(
            db,
            account_id=account.id,
            account_name=account_name,
            run_id=run_id,
            signal_token=signal_token,
            quote_round_id=execution_round_id,
        ):
            continue

        entry_not_before = _route_entry_not_before(event.route_id)
        if now.time() < entry_not_before:
            # 不写终态日志，保留事件给统一批次；否则09:30先到的低质候选
            # 会按主键顺序占满单日名额，优质候选永远没有比较机会。
            deferred += 1
            continue

        account = await paper._refresh_account(db, account)
        if not _route_auto_order_enabled(event.route_id, now=now):
            skipped += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="skip_buy",
                decision="dry_run",
                reason="该隔离候选路线自动撮合已暂停；继续采集前向证据，但不新增模拟持仓",
                price=_safe_float(event.assumed_fill_price),
                amount=None,
                candidate=candidate,
            )
            continue

        delay_seconds = (now - event.observed_at).total_seconds()
        if delay_seconds < 0 or delay_seconds > challenger_execution_policy(event.route_id)["max_execution_delay_sec"]:
            skipped += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="skip_terminal",
                decision="skipped",
                reason=f"confirmed事件距执行{delay_seconds:.0f}秒，超过前向成交时效；不按旧价回填",
                price=_safe_float(event.assumed_fill_price),
                amount=None,
                candidate=candidate,
            )
            continue

        # 原确认的源证据不可被后来的好行情补齐；只追加执行诊断，不改历史事件。
        if event.route_id == "momentum_first_retest":
            source_quote = snapshot.get("quote") if isinstance(snapshot.get("quote"), dict) else {}
            source_rules = snapshot.get("rule_snapshot") if isinstance(snapshot.get("rule_snapshot"), dict) else {}
            source_issues = momentum_liquidity_gate_issues(source_quote, source_rules)
            candidate["confirmation_liquidity_contract"] = MOMENTUM_LIQUIDITY_CONTRACT_VERSION
            candidate["confirmation_liquidity_issues"] = source_issues
            if source_issues:
                skipped += 1
                candidate["execution_block_class"] = "invalid_confirmation_evidence"
                await _record_terminal_log(
                    db, paper=paper, account_id=account.id, event=event, run_id=run_id,
                    action="skip_terminal", decision="skipped",
                    reason=f"confirmed原始流动性证据不合格：{source_issues[0]['reason']}；禁止用后续行情补证",
                    price=_safe_float(event.assumed_fill_price), amount=None, candidate=candidate,
                )
                continue

        spot = await paper._spot_by_code(db, event.code)
        quote_ok, quote_reason = paper._execution_quote_status(
            spot,
            now.date(),
            now=now,
        )
        if not quote_ok:
            skipped += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="wait_buy",
                decision="wait",
                reason=f"当前不可变行情轮次暂不可执行：{quote_reason}；仅允许下一新鲜轮次重评",
                price=_safe_float(event.assumed_fill_price),
                amount=None,
                candidate=candidate,
            )
            continue

        entry_price, entry_reason = _conservative_entry_price(
            event=event,
            snapshot=snapshot,
            spot=spot,
        )
        if entry_price is None:
            skipped += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action=(
                    "skip_terminal"
                    if "价格漂移" in entry_reason or "confirmed事件缺少" in entry_reason
                    else "wait_buy"
                ),
                decision=("skipped" if "价格漂移" in entry_reason or "confirmed事件缺少" in entry_reason else "wait"),
                reason=entry_reason,
                price=_safe_float(event.assumed_fill_price),
                amount=None,
                candidate=candidate,
            )
            continue

        live_valid, live_reason = _live_route_confirmation_valid(
            event,
            snapshot,
            spot,
            audit=candidate,
        )
        if not live_valid:
            skipped += 1
            liquidity_issues = candidate.get("execution_liquidity_issues") or []
            recoverable = (all(issue["recoverable"] for issue in liquidity_issues)
                           if liquidity_issues else live_reason.startswith("暂缺"))
            candidate["execution_block_class"] = "recoverable_data_wait" if recoverable else "signal_invalidated"
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="wait_buy" if recoverable else "skip_terminal",
                decision="wait" if recoverable else "skipped",
                reason=live_reason,
                price=entry_price,
                amount=None,
                candidate=candidate,
            )
            continue

        # 完整confirmed事件+本轮价格/量比/盘口复核通过才记买点，先于现金/持仓约束。
        from app.push.paper_buy_points import record_buy_point, event_reason
        await record_buy_point(
            db, account=account, strategy_version=paper._strategy_version(account_name),
            label=paper._strategy_display_meta(account)["label"], code=event.code,
            name=str(event.name or getattr(spot, "name", "") or event.code),
            source=event.route_id, signal_key=event.event_key,
            reason=event_reason(ROUTE_META[event.route_id]["hypothesis"], event, spot),
            price=float(spot.price), observed_at=now, decision_run_id=run_id,
            quote_round_id=quote_round_id, as_of_at=quote_context.get("as_of_at"),
            code_version=str(quote_context.get("code_version") or ""),
            market_context={
                **{key: getattr(spot, key, None) for key in (
                    "price", "prev_close", "open", "high", "low", "change_pct",
                    "avg_price", "volume_ratio", "turnover_rate", "amount",
                    "circ_market_cap", "limit_up", "limit_down",
                    "orderbook_imbalance", "bid_ask_spread",
                )},
                "source_quote_at": str(getattr(spot, "source_quote_at", "") or ""),
                "quote_round_id": str(getattr(spot, "quote_round_id", "") or ""),
            },
        )
        positions = await paper._open_positions(db, account.id)
        position_codes = {position.code for position in positions}
        pending_orders = list((await db.scalars(select(TradeOrder).where(
            TradeOrder.broker == "paper", TradeOrder.account_id == account_name,
            TradeOrder.side == "buy", TradeOrder.trade_date == now.date(),
            TradeOrder.status.in_(("pending", "submitted", "partial")),
        ))).all())
        pending_codes = {order.code for order in pending_orders}
        reserved_cash = sum(
            max(0, int(order.quantity or 0) - int(order.filled_quantity or 0)) * float(order.price)
            + paper._commission(max(0, int(order.quantity or 0) - int(order.filled_quantity or 0)) * float(order.price))
            for order in pending_orders
        )
        candidate["pending_buy_codes"] = sorted(pending_codes)
        candidate["reserved_buy_cash"] = round(reserved_cash, 2)
        max_daily_buys, max_positions = paper._strategy_buy_limits(account_name)
        if event.code in position_codes | pending_codes:
            skipped += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="skip_terminal",
                decision="skipped",
                reason="隔离候选子账户已持有或已委托该股，不重复建仓",
                price=entry_price,
                amount=None,
                candidate=candidate,
            )
            continue
        daily_buys = await _today_buy_count(db, account.id, now.date()) + len(pending_codes - position_codes)
        if len(position_codes | pending_codes) >= max_positions or daily_buys >= max_daily_buys:
            skipped += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="wait_buy",
                decision="wait",
                reason="本轮达到单日开仓数或最大持仓数；事件有效期内仅在新行情轮次重评",
                price=entry_price,
                amount=None,
                candidate=candidate,
            )
            continue

        policy = _entry_policy(event.route_id)
        position_factor = 1.0
        opening_risk_end = _parse_hhmm(
            challenger_execution_policy(event.route_id)["opening_risk_end"],
            time(9, 35),
        )
        if now.time() < opening_risk_end:
            position_factor = min(
                max(float(challenger_execution_policy(event.route_id)["opening_position_factor"]), 0.0),
                1.0,
            )
        candidate["opening_position_factor"] = position_factor
        budget = (
            float(account.total_assets or account.initial_capital or 0)
            * float(policy["position_pct"])
            * position_factor
        )
        cash_cap = max(0.0, float(account.current_capital or 0) - reserved_cash) * (
            1.0 - max(float(challenger_execution_policy(event.route_id)["cash_buffer_pct"]), 0.0)
        )
        amount = int(min(budget, cash_cap) / entry_price / 100) * 100
        if amount < 100:
            skipped += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="wait_buy",
                decision="wait",
                reason="隔离账户本轮预算或可用现金不足一手；事件有效期内可在新轮重评",
                price=entry_price,
                amount=amount,
                candidate=candidate,
            )
            continue

        risk = await paper._risk_check_for_buy(db, account, event.code, entry_price, amount)
        if risk.get("final_level") != "pass":
            blocked += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="wait_buy",
                decision="wait",
                reason="隔离候选策略仍执行完整风控；本轮风控警告或阻断，事件有效期内可在新轮重评",
                price=entry_price,
                amount=amount,
                candidate=candidate,
                risk=risk,
            )
            continue

        stop_loss_price = round(
            entry_price * (1.0 - float(policy["stop_loss_pct"]) / 100.0),
            2,
        )
        reason = (
            f"隔离候选策略前向模拟成交：{ROUTE_META[event.route_id]['hypothesis']}；"
            f"事件编号={event.event_key}；不连接真实券商"
        )
        quote_context = paper._quote_round_context()
        decision_round_id = execution_round_id
        result = await submit_order(
            db,
            SubmitOrderCommand(
                code=event.code,
                side="buy",
                price=entry_price,
                quantity=amount,
                broker="paper",
                account_id=account_name,
                strategy_id="paper-challenger-forward",
                strategy_version=paper._strategy_version(account_name),
                signal_id=signal_token,
                source=event.route_id,
                reason=reason,
                execute=True,
                decision_round_id=decision_round_id,
                decision_at=now,
                as_of_at=(
                    quote_context.get("as_of_at")
                    if isinstance(quote_context.get("as_of_at"), datetime)
                    else None
                ),
                idempotency_key=(
                    f"challenger:{signal_token}:buy:"
                    f"{hashlib.sha1(decision_round_id.encode()).hexdigest()[:12]}"
                ),
                defer_until_next_round=bool(
                    settings.PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND
                ),
                deferred_metadata={
                    "candidate": candidate,
                    "stop_loss_price": stop_loss_price,
                    "block_warn": True,
                },
            ),
        )
        await paper._refresh_expired_paper_rows(db, account, event)
        order = result.get("order") or {}
        fills = result.get("fills") or []
        if str(order.get("status") or "") == "filled" and fills:
            entries += 1
            candidate["stop_loss_price"] = stop_loss_price
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="buy",
                decision="executed",
                reason=reason,
                price=entry_price,
                amount=amount,
                candidate=candidate,
                risk=result.get("risk") or risk,
                executed_trade_id=int(fills[0].get("broker_trade_id") or 0) or None,
            )
            # paper broker has already created the position; retain the route stop.
            position = next(
                (
                    item
                    for item in await paper._open_positions(db, account.id)
                    if item.code == event.code
                ),
                None,
            )
            if position is not None:
                position.stop_loss_price = stop_loss_price
                await db.commit()
        elif str(order.get("status") or "") in {"submitted", "partial"}:
            deferred += 1
            candidate["stop_loss_price"] = stop_loss_price
            candidate["decision_round_id"] = decision_round_id
            candidate["pending_order_id"] = order.get("order_id")
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="deferred_buy",
                decision="wait",
                reason=str(
                    order.get("error_message")
                    or "已通过完整风控，等待下一健康行情轮次按五档深度撮合"
                ),
                price=entry_price,
                amount=amount,
                candidate=candidate,
                risk=result.get("risk") or risk,
            )
        else:
            blocked += 1
            await _record_terminal_log(
                db,
                paper=paper,
                account_id=account.id,
                event=event,
                run_id=run_id,
                action="wait_buy",
                decision="wait",
                reason=str(order.get("error_message") or "paper委托本轮未形成可验证成交；仅在新轮重评"),
                price=entry_price,
                amount=amount,
                candidate=candidate,
                risk=result.get("risk") or risk,
            )

    await paper._refresh_expired_paper_rows(db, *sell_logs)
    return {
        "enabled": True,
        "entries": entries,
        "sells": sum(1 for item in sell_logs if item.action == "sell" and item.decision == "executed"),
        "skipped": skipped,
        "blocked": blocked,
        "deferred": deferred,
        "events_considered": len(events),
        "broker": "paper",
        "real_order_connected": False,
    }


async def build_strategy_iteration_challenger_comparison(
    db: AsyncSession,
    *,
    horizon_days: int = 3,
    recent_limit: int = 50,
    account_name: str | None = None,
) -> dict[str, Any]:
    """Return account-scoped simulated results and forward-evidence comparison."""

    from app.api.v1 import paper
    from app.paper.strategy_iteration_shadow import (
        ROUTE_C3,
        _first_board_activation_date,
        build_strategy_iteration_evidence_summary,
        build_first_board_current_pool,
    )

    horizon_days = horizon_days if horizon_days in {1, 3, 5} else 3
    recent_limit = min(max(int(recent_limit), 1), 200)
    all_route_ids = tuple(ROUTE_META)
    route_versions = {
        route_id: _route_shadow_version(route_id) for route_id in all_route_ids
    }

    def base_account_for_route(route_id: str) -> str | None:
        challenger_name = paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.get(route_id)
        if challenger_name is not None:
            return paper.PAPER_CHALLENGER_BASE_ACCOUNT[challenger_name]
        value = ROUTE_META.get(route_id, {}).get("base_account_name")
        return str(value) if value else None

    route_ids = tuple(
        route_id
        for route_id in all_route_ids
        if account_name is None or base_account_for_route(route_id) == account_name
    )

    count_rows = (
        await db.execute(
            select(
                PaperShadowEvent.route_id,
                PaperShadowEvent.route_version,
                PaperShadowEvent.event_type,
                func.count(PaperShadowEvent.id),
            )
            .where(PaperShadowEvent.route_id.in_(all_route_ids))
            .group_by(
                PaperShadowEvent.route_id,
                PaperShadowEvent.route_version,
                PaperShadowEvent.event_type,
            )
        )
    ).all()
    event_counts: dict[str, dict[str, int]] = defaultdict(dict)
    for route_id, route_version, event_type, count in count_rows:
        normalized_route = str(route_id)
        if str(route_version) != route_versions.get(normalized_route):
            continue
        event_counts[normalized_route][str(event_type)] = int(count or 0)

    outcome_count_rows = (
        await db.execute(
            select(
                PaperShadowEvent.route_id,
                PaperShadowEvent.route_version,
                PaperShadowEvent.status,
                func.count(PaperShadowEvent.id),
            )
            .where(
                PaperShadowEvent.route_id.in_(all_route_ids),
                PaperShadowEvent.event_type == "session_outcome",
            )
            .group_by(
                PaperShadowEvent.route_id,
                PaperShadowEvent.route_version,
                PaperShadowEvent.status,
            )
        )
    ).all()
    outcome_status_counts: dict[str, dict[str, int]] = defaultdict(dict)
    for route_id, route_version, status, count in outcome_count_rows:
        normalized_route = str(route_id)
        if str(route_version) != route_versions.get(normalized_route):
            continue
        outcome_status_counts[normalized_route][str(status)] = int(count or 0)

    confirmed_rows = (
        await db.execute(
            select(
                PaperShadowEvent.route_id,
                PaperShadowEvent.route_version,
                PaperShadowEvent.event_key,
                PaperShadowEvent.trade_date,
                PaperShadowEvent.observed_at,
            ).where(
                PaperShadowEvent.route_id.in_(all_route_ids),
                PaperShadowEvent.event_type == "confirmed",
            )
        )
    ).all()
    confirmed_by_route: dict[str, list[Any]] = defaultdict(list)
    for row in confirmed_rows:
        normalized_route = str(row.route_id)
        if str(row.route_version) == route_versions.get(normalized_route):
            confirmed_by_route[normalized_route].append(row)

    evaluated_rows = (
        await db.execute(
            select(
                PaperShadowEvaluation.route_id,
                PaperShadowEvaluation.route_version,
                PaperShadowEvaluation.signal_event_key,
            ).where(
                PaperShadowEvaluation.route_id.in_(all_route_ids),
                PaperShadowEvaluation.horizon_days == horizon_days,
            )
        )
    ).all()
    confirmed_keys_by_route = {
        route_id: {str(row.event_key) for row in rows}
        for route_id, rows in confirmed_by_route.items()
    }
    evaluated_counts: dict[str, int] = defaultdict(int)
    for route_id, route_version, signal_event_key in evaluated_rows:
        normalized_route = str(route_id)
        if (
            str(route_version) == route_versions.get(normalized_route)
            and str(signal_event_key)
            in confirmed_keys_by_route.get(normalized_route, set())
        ):
            evaluated_counts[normalized_route] += 1

    evaluations = list(
        (
            await db.scalars(
                select(PaperShadowEvaluation)
                .where(
                    PaperShadowEvaluation.route_id.in_(route_ids),
                    PaperShadowEvaluation.horizon_days == horizon_days,
                )
                .order_by(
                    PaperShadowEvaluation.signal_trade_date,
                    PaperShadowEvaluation.signal_time,
                )
            )
        ).all()
    )
    evaluations = [
        row
        for row in evaluations
        if str(row.route_version) == route_versions.get(str(row.route_id))
    ]
    pairs: list[dict[str, Any]] = []
    for route_id in all_route_ids:
        if route_id not in route_ids:
            continue
        challenger_name = paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.get(route_id)
        champion_name = base_account_for_route(route_id)
        if champion_name is None:
            continue
        champion = await paper._get_or_create_account(db, champion_name)
        challenger = (
            await paper._get_or_create_account(db, challenger_name)
            if challenger_name is not None
            else None
        )
        champion = await paper._refresh_account(db, champion)
        if challenger is not None:
            challenger = await paper._refresh_account(db, challenger)
        champion_positions = await paper._open_positions(db, champion.id)
        challenger_positions = (
            await paper._open_positions(db, challenger.id)
            if challenger is not None
            else []
        )
        route_version = route_versions[route_id]
        evidence = build_strategy_iteration_evidence_summary(
            evaluations,
            route_id=route_id,
            route_version=route_version,
            horizon_days=horizon_days,
        )
        route_confirmed = confirmed_by_route.get(route_id, [])
        route_evaluated_keys = {
            str(item.signal_event_key)
            for item in evaluations
            if item.route_id == route_id
        }
        pending_signals = [
            item
            for item in route_confirmed
            if str(item.event_key) not in route_evaluated_keys
        ]
        confirmed_sessions = {item.trade_date for item in route_confirmed}
        pending_sessions = {item.trade_date for item in pending_signals}
        min_samples = max(int(evidence.get("minimum_required_samples") or 100), 1)
        min_sessions = max(int(evidence.get("minimum_required_sessions") or 20), 1)
        collection_progress = min(
            len(route_confirmed) / min_samples,
            len(confirmed_sessions) / min_sessions,
        )
        settled_progress = min(
            int(evidence.get("sample_count") or 0) / min_samples,
            int(evidence.get("independent_sessions") or 0) / min_sessions,
        )
        evidence.update({
            "confirmed_signal_count": len(route_confirmed),
            "confirmed_signal_sessions": len(confirmed_sessions),
            "pending_settlement_count": len(pending_signals),
            "pending_settlement_sessions": len(pending_sessions),
            "first_pending_signal_trade_date": (
                min(pending_sessions).isoformat() if pending_sessions else None
            ),
            "latest_pending_signal_trade_date": (
                max(pending_sessions).isoformat() if pending_sessions else None
            ),
            "collection_progress_pct": round(min(collection_progress, 1.0) * 100),
            "settled_progress_pct": round(min(settled_progress, 1.0) * 100),
            "settlement_state": (
                "partially_settled"
                if evidence.get("sample_count") and pending_signals
                else "settled"
                if evidence.get("sample_count")
                else "waiting_horizon"
                if pending_signals
                else "no_confirmed_signal"
            ),
            "settlement_rule": (
                f"盘中确认后观察{horizon_days}个后续交易日，"
                "到期且正式日K完整后才计入已结算样本与独立交易日"
            ),
        })
        max_account_drawdown_pct = float(
            settings.PAPER_STRATEGY_ITERATION_EVAL_MAX_ACCOUNT_DRAWDOWN_PCT
        )
        # Statistical evidence is route-version scoped. Challenger account NAV
        # and max drawdown are lifetime, potentially cross-version facts, so they
        # must not be injected into evidence.gates as current-rule measurements.
        # Keep them as a separately labelled execution guardrail.
        evidence["statistical_gates"] = dict(evidence.get("gates") or {})
        evidence["evidence_gate_passed"] = all(
            evidence["statistical_gates"].values()
        )
        evidence["maximum_account_drawdown_pct"] = max_account_drawdown_pct
        evidence.setdefault("thresholds", {})[
            "maximum_account_drawdown_pct"
        ] = max_account_drawdown_pct
        if challenger is not None:
            account_drawdown_pct = abs(float(challenger.max_drawdown or 0))
            account_drawdown_acceptable = (
                account_drawdown_pct <= max_account_drawdown_pct
            )
            evidence["account_max_drawdown_pct"] = round(account_drawdown_pct, 4)
            evidence["account_drawdown_available"] = True
            evidence["account_drawdown_applicable"] = True
        else:
            account_drawdown_pct = None
            account_drawdown_acceptable = True
            evidence["account_max_drawdown_pct"] = None
            evidence["account_drawdown_available"] = False
            evidence["account_drawdown_applicable"] = False
        drawdown_tag_only = bool(challenger is not None and paper.experiment_active(challenger_name))
        evidence["account_drawdown_applicable"] = bool(challenger is not None and not drawdown_tag_only)
        evidence["execution_guardrails"] = {
            "account_drawdown_applicable": bool(challenger is not None and not drawdown_tag_only),
            "tag_only": drawdown_tag_only,
            "account_drawdown_scope": "账户成立以来，可能跨策略版本",
            "account_max_drawdown_pct": (
                round(account_drawdown_pct, 4)
                if account_drawdown_pct is not None
                else None
            ),
            "maximum_account_drawdown_pct": max_account_drawdown_pct,
            "account_drawdown_acceptable": account_drawdown_acceptable,
        }
        evidence["execution_guardrails_passed"] = drawdown_tag_only or account_drawdown_acceptable
        evidence["manual_review_gate_passed"] = bool(
            evidence["evidence_gate_passed"] and evidence["execution_guardrails_passed"]
        )
        champion_nav_rows = list(
            (
                await db.scalars(
                    select(PaperNav)
                    .where(PaperNav.account_id == champion.id)
                    .order_by(PaperNav.trade_date)
                )
            ).all()
        )
        challenger_nav_rows = (
            list(
                (
                    await db.scalars(
                        select(PaperNav)
                        .where(PaperNav.account_id == challenger.id)
                        .order_by(PaperNav.trade_date)
                    )
                ).all()
            )
            if challenger is not None
            else []
        )
        champion_nav_rows = await paper._filter_reporting_nav(db, champion_nav_rows)
        challenger_nav_rows = await paper._filter_reporting_nav(db, challenger_nav_rows)
        common_period = _common_period_comparison(
            champion_nav_rows,
            challenger_nav_rows,
        )
        if challenger is None:
            common_period["note"] = "该路线当前只采集证据，未创建撮合账户，不能展示账户净值差"
        inception_delta = (
            round(
                float(challenger.total_return or 0)
                - float(champion.total_return or 0),
                4,
            )
            if challenger is not None
            else None
        )
        counts = event_counts.get(route_id, {})
        activation_date = (
            str(settings.PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE or "")
            if route_id == ROUTE_C3
            else None
        )
        activation_config_valid = bool(
            route_id != ROUTE_C3 or _first_board_activation_date() is not None
        )
        evidence["activation_config_valid"] = activation_config_valid
        evidence_status = (
            "configuration_blocked"
            if not activation_config_valid
            else "scheduled"
            if activation_date and date.today().isoformat() < activation_date
            else "manual_review_eligible"
            if evidence.get("manual_review_gate_passed")
            else "execution_guardrail_blocked"
            if evidence.get("evidence_gate_passed")
            else "collecting"
            if evidence.get("sample_count") or counts.get("confirmed")
            else "waiting_for_forward_signal"
        )
        if challenger is not None:
            execution_strategy_version = paper._strategy_version(challenger_name)
            current_version_execution = await _current_version_execution_summary(
                db,
                account_id=challenger.id,
                strategy_version=execution_strategy_version,
                open_positions=challenger_positions,
            )
            challenger_payload = {
                **paper._account_payload(challenger),
                "return_breakdown": await _account_return_breakdown(db, challenger),
                "account_configured": True,
                "open_position_count": len(challenger_positions),
                "positions": [
                    paper._position_payload(item) for item in challenger_positions
                ],
                "execution_enabled": _route_auto_order_enabled(route_id),
                "evidence_collection_enabled": bool(
                    settings.PAPER_STRATEGY_ITERATION_SHADOW_ENABLED
                ),
                "nav": _nav_payload(challenger_nav_rows),
                "current_version_execution": current_version_execution,
            }
        else:
            challenger_payload = {
                "id": None,
                "account_name": None,
                "account_configured": False,
                "strategy_label": "仅前向采证（未创建撮合账户）",
                "strategy_desc": "记录完整候选分母、未确认对照与固定观察期收益；不生成任何模拟买单",
                "strategy_short": "C3",
                "strategy_version": route_version,
                "total_assets": None,
                "total_return": None,
                "max_drawdown": None,
                "trade_count": 0,
                "open_position_count": 0,
                "positions": [],
                "execution_enabled": False,
                "evidence_collection_enabled": bool(
                    settings.PAPER_FIRST_BOARD_SHADOW_ENABLED
                ),
                "nav": [],
                "current_version_execution": None,
            }
        same_day_funnel_available = route_id == ROUTE_C3
        outcomes = (
            outcome_status_counts.get(route_id, {})
            if same_day_funnel_available
            else {}
        )
        confirmed_first_board = int(outcomes.get("confirmed_first_board", 0))
        confirmed_not_board = int(outcomes.get("confirmed_not_board", 0))
        unconfirmed_first_board = int(outcomes.get("unconfirmed_first_board", 0))
        unconfirmed_not_board = int(outcomes.get("unconfirmed_not_board", 0))
        outcome_denominator = sum(outcomes.values())
        confirmed_outcomes = confirmed_first_board + confirmed_not_board
        final_first_boards = confirmed_first_board + unconfirmed_first_board
        same_day_funnel = {
            "available": same_day_funnel_available,
            "outcome_count": outcome_denominator if same_day_funnel_available else None,
            "confirmed_outcome_count": confirmed_outcomes if same_day_funnel_available else None,
            "closed_first_board_count": final_first_boards if same_day_funnel_available else None,
            "confirmed_first_board_count": confirmed_first_board if same_day_funnel_available else None,
            "unconfirmed_first_board_count": unconfirmed_first_board if same_day_funnel_available else None,
            "unconfirmed_not_board_count": unconfirmed_not_board if same_day_funnel_available else None,
            "same_day_precision": (
                round(confirmed_first_board / confirmed_outcomes, 4)
                if same_day_funnel_available and confirmed_outcomes
                else None
            ),
            "same_day_recall": (
                round(confirmed_first_board / final_first_boards, 4)
                if same_day_funnel_available and final_first_boards
                else None
            ),
            "promotion_metric": False,
            "note": (
                "收盘首板命中仅用于检查C3漏斗覆盖，不等于可交易收益或晋级证据"
                if same_day_funnel_available
                else "该路线未记录完整的同日首板结果分母；空值不是0命中"
            ),
        }

        pairs.append(
            {
                "route_id": route_id,
                **ROUTE_META[route_id],
                "execution_mode": (
                    "isolated_paper" if challenger is not None else "evidence_only"
                ),
                "activation_date": activation_date,
                "activation_config_valid": activation_config_valid,
                "champion": {
                    **paper._account_payload(champion),
                    "return_breakdown": await _account_return_breakdown(db, champion),
                    "open_position_count": len(champion_positions),
                    "auto_buy_enabled": paper._strategy_auto_order_enabled(champion_name),
                    "nav": _nav_payload(champion_nav_rows),
                },
                "challenger": challenger_payload,
                # 成立以来收益起点可能不同，只保留为账户事实，不能当作公平对照。
                "inception_return_delta_pct": inception_delta,
                "common_period": common_period,
                "return_delta_pct": common_period["return_delta_pct"],
                "event_counts": counts,
                "same_day_funnel": same_day_funnel,
                "current_pool": (
                    await build_first_board_current_pool(db) if route_id == ROUTE_C3 else None
                ),
                "evidence": evidence,
                "evidence_status": evidence_status,
            }
        )

    recent_event_query = select(PaperShadowEvent).where(
        PaperShadowEvent.route_id.in_(route_ids),
        PaperShadowEvent.event_type.in_((
            "confirmed",
            "coverage_blocked",
            "evidence_blocked",
            "session_blocked",
            "outcome_blocked",
        )),
    )
    if route_ids:
        # Apply immutable route-version scope before LIMIT. Filtering in Python
        # after LIMIT lets a burst of newer superseded rows hide current events.
        recent_event_query = recent_event_query.where(
            or_(*(
                and_(
                    PaperShadowEvent.route_id == route_id,
                    PaperShadowEvent.route_version == route_versions[route_id],
                )
                for route_id in route_ids
            ))
        )
    recent_events = list(
        (
            await db.scalars(
                recent_event_query
                .order_by(PaperShadowEvent.observed_at.desc(), PaperShadowEvent.id.desc())
                .limit(recent_limit)
            )
        ).all()
    )
    recent_signal_keys = [row.event_key for row in recent_events if row.event_type == "confirmed"]
    recent_evaluations = list(
        (
            await db.scalars(
                select(PaperShadowEvaluation).where(
                    PaperShadowEvaluation.signal_event_key.in_(recent_signal_keys or ["__none__"]),
                )
            )
        ).all()
    )
    eval_by_signal: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in recent_evaluations:
        eval_by_signal[row.signal_event_key][str(row.horizon_days)] = {
            "net_return_pct": row.net_return_pct,
            "excess_return_pct": row.excess_return_pct,
            "max_favorable_pct": row.max_favorable_pct,
            "max_adverse_pct": row.max_adverse_pct,
            "exit_trade_date": row.exit_trade_date.isoformat(),
        }

    # Keep account and strategy-version paired in SQL. Independent IN lists
    # form a cross product and can leak a different route's rows when more than
    # one strategy is requested or malformed historical data reuses a version.
    current_action_scopes = list(dict.fromkeys(
        (
            int(pair["challenger"]["id"]),
            str(pair["challenger"].get("strategy_version") or ""),
        )
        for pair in pairs
        if pair["challenger"].get("id") is not None
        and pair["challenger"].get("strategy_version")
    ))
    action_rows = (
        list(
            (
                await db.scalars(
                    select(PaperAutoTradeLog)
                    .where(or_(*(
                        and_(
                            PaperAutoTradeLog.account_id == scoped_account_id,
                            PaperAutoTradeLog.strategy_version == scoped_version,
                        )
                        for scoped_account_id, scoped_version in current_action_scopes
                    )))
                    .order_by(PaperAutoTradeLog.created_at.desc(), PaperAutoTradeLog.id.desc())
                    .limit(recent_limit)
                )
            ).all()
        )
        if current_action_scopes
        else []
    )

    routes_by_champion: dict[str, list[str]] = defaultdict(list)
    for route_id in all_route_ids:
        champion_name = base_account_for_route(route_id)
        if champion_name:
            routes_by_champion[champion_name].append(route_id)
    strategy_code_by_account = {
        paper.PAPER_ACCOUNT_DEFAULT: "A",
        paper.PAPER_ACCOUNT_PROMOTION: "B",
        paper.PAPER_ACCOUNT_MAINLINE: "C",
        paper.PAPER_ACCOUNT_AUCTION: "D",
        paper.PAPER_ACCOUNT_TENBAGGER: "E",
        paper.PAPER_ACCOUNT_REVERSAL: "F",
    }
    not_configured_reasons = {
        paper.PAPER_ACCOUNT_DEFAULT: (
            "本轮只修复策略A的基准执行链路，没有提出可证伪的A2候选假设；"
            "因此不创建空壳子账户，也不把0%伪装成验证结果。"
        ),
        paper.PAPER_ACCOUNT_TENBAGGER: (
            "策略E保留严格的高标接力与真实排队门禁，本轮没有新增E2候选假设；"
            "因此当前没有可对照的隔离子账户。"
        ),
    }
    pair_by_route = {pair["route_id"]: pair for pair in pairs}
    strategy_coverage: list[dict[str, Any]] = []
    for champion_name in paper.PAPER_ALL_ACCOUNTS:
        coverage_champion = await paper._get_or_create_account(db, champion_name)
        coverage_champion = await paper._refresh_account(db, coverage_champion)
        coverage_positions = await paper._open_positions(db, coverage_champion.id)
        strategy_route_ids = routes_by_champion.get(champion_name, [])
        control_challenger_name = paper.PAPER_CONTROL_CHALLENGER_BY_BASE.get(
            champion_name
        )
        control_challenger = (
            await paper._get_or_create_account(db, control_challenger_name)
            if control_challenger_name
            else None
        )
        control_sample_count = (
            int(
                await db.scalar(
                    select(func.count(PaperControlSample.id)).where(
                        PaperControlSample.account_id == coverage_champion.id
                    )
                )
                or 0
            )
            if control_challenger is not None
            else 0
        )
        control_evaluated_count = (
            int(
                await db.scalar(
                    select(func.count(PaperControlSample.id)).where(
                        PaperControlSample.account_id == coverage_champion.id,
                        PaperControlSample.finalized_at.is_not(None),
                    )
                )
                or 0
            )
            if control_challenger is not None
            else 0
        )
        execution_challenger_name = (
            paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.get(strategy_route_ids[0])
            if strategy_route_ids
            else paper.PAPER_ACCOUNT_CHALLENGER_E
            if champion_name == paper.PAPER_ACCOUNT_TENBAGGER
            else None
        )
        execution_challenger = (
            await paper._get_or_create_account(db, execution_challenger_name)
            if execution_challenger_name
            else None
        )
        if execution_challenger is not None:
            execution_challenger = await paper._refresh_account(db, execution_challenger)
        execution_positions = (
            await paper._open_positions(db, execution_challenger.id)
            if execution_challenger is not None
            else []
        )
        configured = bool(strategy_route_ids or execution_challenger)
        global_confirmed_count = sum(
            int(event_counts.get(route_id, {}).get("confirmed", 0))
            for route_id in strategy_route_ids
        )
        global_evaluated_count = sum(
            int(evaluated_counts.get(route_id, 0))
            for route_id in strategy_route_ids
        )
        route_details = [
            {
                "route_id": route_id,
                "route_label": ROUTE_META[route_id]["label"],
                "route_version": route_versions[route_id],
                "execution_mode": (
                    "isolated_paper"
                    if route_id in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
                    else "evidence_only"
                ),
                "execution_enabled": _route_auto_order_enabled(route_id),
                "confirmed_count": int(
                    event_counts.get(route_id, {}).get("confirmed", 0)
                ),
                "evaluated_count": int(evaluated_counts.get(route_id, 0)),
            }
            for route_id in strategy_route_ids
        ]
        if champion_name == paper.PAPER_ACCOUNT_TENBAGGER and execution_challenger is not None:
            route_details.append({
                "route_id": "e2_highboard_independent",
                "route_label": "策略E2 · 独立高标主循环",
                "route_version": paper._strategy_version(execution_challenger.account_name),
                "execution_mode": "independent_paper_loop",
                "execution_enabled": paper._strategy_auto_order_enabled(
                    execution_challenger.account_name
                ),
                "confirmed_count": 0,
                "evaluated_count": 0,
                "excluded_from_performance": False,
            })
        scoped_pairs = [
            pair_by_route[route_id]
            for route_id in strategy_route_ids
            if route_id in pair_by_route
        ]
        if any(
            pair.get("evidence_status") == "manual_review_eligible"
            for pair in scoped_pairs
        ):
            implementation_status = "manual_review_eligible"
        elif any(
            pair.get("evidence_status") == "execution_guardrail_blocked"
            for pair in scoped_pairs
        ):
            implementation_status = "execution_guardrail_blocked"
        elif any(pair.get("evidence_status") == "collecting" for pair in scoped_pairs):
            implementation_status = "collecting"
        elif any(pair.get("evidence_status") == "scheduled" for pair in scoped_pairs):
            implementation_status = "scheduled"
        elif global_confirmed_count or global_evaluated_count:
            implementation_status = "collecting"
        elif execution_challenger is not None and not strategy_route_ids:
            implementation_status = "independent_execution"
        elif configured:
            implementation_status = "waiting_for_forward_signal"
        else:
            implementation_status = "not_configured"
        independent_execution = (
            champion_name == paper.PAPER_ACCOUNT_TENBAGGER
            and execution_challenger is not None
        )
        account_backed_count = sum(
            route_id in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
            for route_id in strategy_route_ids
        ) + int(independent_execution)
        execution_enabled_count = sum(
            _route_auto_order_enabled(route_id) for route_id in strategy_route_ids
        ) + int(
            independent_execution
            and paper._strategy_auto_order_enabled(execution_challenger.account_name)
        )
        execution_paused_count = account_backed_count - execution_enabled_count
        evidence_only_count = sum(
            route_id not in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
            for route_id in strategy_route_ids
        )
        primary_route = route_details[0]["route_id"] if route_details else None
        strategy_coverage.append({
            "strategy_code": strategy_code_by_account[champion_name],
            "account_name": champion_name,
            "champion": {
                **paper._account_payload(coverage_champion),
                "return_breakdown": await _account_return_breakdown(db, coverage_champion),
                "open_position_count": len(coverage_positions),
                "auto_buy_enabled": paper._strategy_auto_order_enabled(champion_name),
            },
            "challenger_configured": configured,
            "challenger": (
                {
                    **paper._account_payload(execution_challenger),
                    "return_breakdown": await _account_return_breakdown(db, execution_challenger),
                    "open_position_count": len(execution_positions),
                    "positions": [
                        paper._position_payload(item) for item in execution_positions
                    ],
                    "execution_mode": (
                        "independent_paper_loop"
                        if champion_name == paper.PAPER_ACCOUNT_TENBAGGER
                        else "isolated_paper"
                    ),
                    "control_sample_only": False,
                }
                if execution_challenger is not None
                else None
            ),
            # Backward-compatible primary route plus the complete multi-route list.
            "route_id": primary_route,
            "route_label": (
                route_details[0].get("route_label")
                if len(route_details) == 1
                else f"{len(route_details)}条独立候选路线"
                if route_details
                else None
            ),
            "route_count": len(route_details),
            "routes": route_details,
            "control_sample_only": False,
            "control_samples_attached": control_challenger is not None,
            "control_sample_count": control_sample_count,
            "control_evaluated_count": control_evaluated_count,
            "implementation_status": implementation_status,
            "confirmed_count": global_confirmed_count,
            "evaluated_count": global_evaluated_count,
            "challenger_execution_enabled": execution_enabled_count > 0,
            "account_backed_route_count": account_backed_count,
            "execution_enabled_route_count": execution_enabled_count,
            "execution_paused_route_count": execution_paused_count,
            "evidence_only_route_count": evidence_only_count,
            "reason": (
                "E2由独立高标主循环执行模拟撮合；控制样本仅为附属对照，不覆盖真实账户状态或收益。"
                if independent_execution
                else (
                    f"已配置{len(strategy_route_ids)}条独立候选路线；"
                    f"{account_backed_count}条使用独立模拟账户（"
                    f"{execution_enabled_count}条开启撮合、{execution_paused_count}条暂停撮合），"
                    f"{evidence_only_count}条只采集完整分母与前向证据。"
                    "所有路线均按自身版本结算，不会自动修改基准策略。"
                    if configured
                    else not_configured_reasons.get(
                        champion_name,
                        "当前未配置隔离候选路线。",
                    )
                )
            ),
        })
    selected_strategy = next(
        (
            item
            for item in strategy_coverage
            if account_name is not None and item["account_name"] == account_name
        ),
        None,
    )

    return {
        "generated_at": datetime.now().isoformat(),
        "horizon_days": horizon_days,
        "selected_account_name": account_name,
        "selected_strategy": selected_strategy,
        "coverage_summary": {
            "configured": sum(1 for item in strategy_coverage if item["challenger_configured"]),
            "total": len(strategy_coverage),
            # route_count historically means shadow routes; E2 is a separate
            # account-backed main loop and is therefore exposed explicitly.
            "route_count": len(all_route_ids),
            "shadow_route_count": len(all_route_ids),
            "independent_execution_route_count": 1,
            "total_route_count": len(all_route_ids) + 1,
            "control_sample_route_count": 0,
            "control_sample_account_count": len(
                paper.PAPER_CONTROL_CHALLENGER_BY_BASE
            ),
            "shadow_account_backed_route_count": sum(
                route_id in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
                for route_id in all_route_ids
            ),
            "account_backed_route_count": sum(
                route_id in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
                for route_id in all_route_ids
            ) + 1,
            "independent_execution_enabled": paper._strategy_auto_order_enabled(
                paper.PAPER_ACCOUNT_CHALLENGER_E
            ),
            "execution_enabled_route_count": sum(
                _route_auto_order_enabled(route_id) for route_id in all_route_ids
            ) + int(paper._strategy_auto_order_enabled(paper.PAPER_ACCOUNT_CHALLENGER_E)),
            "execution_paused_route_count": sum(
                route_id in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
                and not _route_auto_order_enabled(route_id)
                for route_id in all_route_ids
            ) + int(not paper._strategy_auto_order_enabled(paper.PAPER_ACCOUNT_CHALLENGER_E)),
            "evidence_only_route_count": sum(
                route_id not in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
                for route_id in all_route_ids
            ),
        },
        "evidence_purpose": {
            "question": "候选规则在未知未来结果时发出的信号，经过固定持有期后是否仍有正净收益与超额收益",
            "does": "记录盘中结构、形态、连续确认和到期收益，形成可复核的样本外晋级门槛",
            "does_not": "代码自测通过不等于策略有效；单日涨停命中或隔离账户盈利也不会自动启用基准策略，更不会连接真实券商",
            "settlement": f"当前选择{horizon_days}日口径；确认信号需等待{horizon_days}个后续交易日后才进入已结算统计",
        },
        "strategy_coverage": strategy_coverage,
        "data_provenance": {
            "metrics_are_hardcoded": False,
            "account_tables": [
                "paper_account",
                "paper_position",
                "paper_trade_log",
                "paper_nav",
            ],
            "evidence_tables": [
                "paper_shadow_event",
                "paper_shadow_evaluation",
                "paper_control_sample",
                "paper_daily_outcome",
            ],
            "account_return_basis": "各账户成立以来",
            "fair_comparison_basis": "双方首个共同净值日归一化",
            "benchmark_basis": "同期全市场个股等权平均涨跌幅",
            "route_versions": route_versions,
            "evidence_version_scope": "每条路线仅统计自身当前route_version",
            "recent_action_version_scope": "仅当前Challenger strategy_version",
            "account_metrics_scope": "账户成立以来（可能跨版本，仅作账户事实与执行护栏）",
            "current_version_execution_scope": "按当前Challenger strategy_version过滤，旧版仓位强制退出另列",
            "statistical_gate_scope": "仅当前route_version的固定观察期结算样本",
        },
        "pairs": pairs,
        "recent_signals": [
            {
                "event_key": row.event_key,
                "route_id": row.route_id,
                "route_version": row.route_version,
                "route_label": ROUTE_META[row.route_id]["label"],
                "trade_date": row.trade_date.isoformat(),
                "observed_at": row.observed_at.isoformat(),
                "code": row.code,
                "name": row.name,
                "event_type": row.event_type,
                "status": row.status,
                "assumed_fill_price": row.assumed_fill_price,
                "change_pct": row.change_pct,
                "prior_structure": (
                    _json_dict(row.snapshot_json).get("prior_structure") or {}
                ),
                "evaluations": eval_by_signal.get(row.event_key, {}),
            }
            for row in recent_events
        ],
        "recent_actions": [paper._auto_log_payload(row) for row in action_rows],
        "isolation": {
            "broker": "paper",
            "real_order_connected": False,
            "shares_cash_positions_with_champion": False,
            "promotion_requires_manual_review": True,
            "minimum_sessions": settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_SESSIONS,
            "minimum_samples": settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_SAMPLES,
            "route_versions": route_versions,
            "evidence_only_routes": [
                route_id
                for route_id in all_route_ids
                if route_id not in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE
            ],
            "maximum_account_drawdown_pct": (
                settings.PAPER_STRATEGY_ITERATION_EVAL_MAX_ACCOUNT_DRAWDOWN_PCT
            ),
            "account_drawdown_scope": "账户成立以来跨版本执行护栏，不计入当前路由统计证据",
        },
    }
