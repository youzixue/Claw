"""Forward-only morphology challengers for strategies B/C/C3/D/F.

The production A-F Champion accounts are intentionally untouched by this module.
It records structural eligibility and only promotes a reclaim after a persisted,
multi-frame confirmation streak, so negative-open, zero-axis relaunch,
auction-recovery, fresh-mainline-first-board and short high-board-break
hypotheses accumulate an honest denominator.  A separate service may consume a
fresh confirmed event from account-backed routes into an isolated paper-only
Challenger account; C3 is evidence-only.  No real broker or Champion order path
is reachable from here.
"""

from __future__ import annotations

import json
import hashlib
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Any, Iterable, Mapping

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.data.auction_evidence import auction_evidence_status
from app.data.fund_flow_clock import local_clock
from app.paper.account_policy import route_signal_policy, challenger_execution_policy
from app.core.price_limit_rules import is_limit_up_change
from app.core.stock_tagger import stock_tagger
from app.models.paper import PaperShadowEvaluation, PaperShadowEvent
from app.models.stock import (
    AuctionData,
    BrokenLimitPool,
    LimitUpPool,
    SectorPersistence,
    StockBlacklist,
    StockKline,
    StockSectorMapping,
    StockSpot,
    StockTag,
)


ROUTE_B = "b_weak_open_second_board"
ROUTE_C = "c_recent_limit_relaunch"
ROUTE_C3 = "c3_mainline_first_board"
ROUTE_D = "d_auction_recovery"
ROUTE_F2 = "f2_highboard_break_reclaim"
ROUTE_IDS = (ROUTE_B, ROUTE_C, ROUTE_C3, ROUTE_D, ROUTE_F2)
_EVENT_BATCH_SIZE = 50
# A股价格按分报价；相邻交易日“本日昨收”与“前日收盘”偏差超过一分，
# 视为除权或数据断点，相关收益窗口右删失而不是用绝对价制造伪收益。
from app.data.price_chain import (
    PRICE_CHAIN_MAX_ABS_GAP as _PRICE_CHAIN_MAX_ABS_GAP,
    FORMAL_CLOSE_SOURCES as _FORMAL_CLOSE_SOURCES,
)


def _capture_candidate_projection(route_id, scan_at, *, code="MARKET", quote=None,
                                  stage, reason, original_candidate=False,
                                  original_confirmed=None, original_gate=None,
                                  gate_inputs=None, identities=None, rules=None,
                                  version=None, evidence_ref=None, metrics=None,
                                  source_contract=None):
    """Best-effort leaf handoff only; never mutate production events or transactions."""
    try:
        from app.paper.candidate_shadow import capture_frame

        route, account = {
            ROUTE_B: ("B2", "challenger_b"), ROUTE_C: ("C2", "challenger_c"),
            ROUTE_D: ("D2", "challenger_d"), ROUTE_F2: ("F2", "challenger_f2"),
            ROUTE_C3: ("C3", None),
        }[route_id]
        q = quote or {}
        scan_id = str(q.get("quote_round_id") or f"shadow:{scan_at.isoformat()}")
        at = datetime.now()  # actual predicate completion, never the quote/source clock
        production_version = version or route_version_for(route_id)
        identity = dict(identities or {})
        if stage == "confirmed" and original_confirmed is True:
            identity["original_confirmed_at"] = at
        capture_frame({
            "route": route, "account_id": None, "account_name": account,
            "production_version": production_version,
            # Original structure is day/route/version scoped; scan is only a receipt.
            # Resets clear the experiment segment, never manufacture a new origin.
            "episode_id": f"{route}:{production_version}:{scan_at.date().isoformat()}:{code}",
            "code": code, "name": str(q.get("name") or code),
            "observed_at": at, "producer_reported_at": scan_at, "scan_id": scan_id,
            "evidence_ref": evidence_ref or f"{scan_id}:{route}:{code}:{stage}",
            "stage": stage, "reason": reason,
            "original_candidate": original_candidate,
            "original_confirmed": original_confirmed, "original_gate": original_gate,
            "quote": q, "gate_inputs": gate_inputs or {},
            "rule_snapshot": rules if rules is not None else _rules(route_id),
            "identities": identity, "source_contract": source_contract,
            "relative_strength_pct": (metrics or {}).get("relative_strength_pct")
                if route_id != ROUTE_C3 else None,
            "sector_relative_strength_pct": (metrics or {}).get("relative_strength_pct")
                if route_id == ROUTE_C3 else None,
        })
    except Exception:
        # Includes an unavailable/disabled optional runtime and malformed audit input.
        pass


def route_version_for(route_id: str) -> str:
    """Return the immutable evidence version owned by one route."""

    if route_id == ROUTE_C3:
        identity = {
            "base": str(settings.PAPER_FIRST_BOARD_SHADOW_VERSION),
            "semantics": "c3_continuity_v2",
            "rules": _rules(ROUTE_C3),
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
        return f"c3_continuity_v2:{digest}"
    # 信号参数改变只旋转本路由；旧多帧证据不能被新门槛消费。
    rules = _rules(route_id)
    signal_rules = {key: value for key, value in rules.items() if key not in {
        "shadow_only", "champion_order_connected", "challenger_paper_account_connected",
        "evidence_only_without_execution_account", "real_order_connected",
        "promotion_min_sessions", "promotion_min_samples",
    }}
    prefix = {ROUTE_B: "PAPER_STRATEGY_B_", ROUTE_C: "PAPER_STRATEGY_C_",
              ROUTE_D: "PAPER_STRATEGY_D_", ROUTE_F2: "PAPER_STRATEGY_F2_"}[route_id]
    identity = {
        "rules": signal_rules, "route_id": route_id,
        "confirmation_semantics": "persistent_observed_segments_v3",
        "parameters": {key: getattr(settings, key) for key in type(settings).model_fields
                       if key.startswith(prefix) and key != prefix + "VERSION"},
        "min_quote_coverage": settings.PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return f"{settings.PAPER_STRATEGY_ITERATION_SHADOW_VERSION[:20]}:{digest}"


def _first_board_activation_date() -> date | None:
    raw = str(settings.PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
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


def _parse_hhmm(value: Any, fallback: time) -> time:
    raw = str(value or "").strip()
    try:
        hour, minute = raw.split(":", 1)
        return time(int(hour), int(minute[:2]))
    except (TypeError, ValueError):
        return fallback


def _auction_time(value: Any) -> time | None:
    raw = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%H%M%S", "%H%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _quote_clock_valid(quote: Mapping[str, Any], observed_at: datetime) -> bool:
    explicit_clock = local_clock(
        _parse_datetime(quote.get("source_quote_at"))
        or _parse_datetime(quote.get("received_at"))
        or _parse_datetime(quote.get("updated_at"))
    )
    if explicit_clock is None:
        # Direct callers may submit an already-frozen batch without leaf clocks;
        # the batch observed_at remains the declared cutoff in the event payload.
        return True
    tolerance = timedelta(
        seconds=max(int(settings.ANOMALY_QUOTE_ROUND_TOLERANCE_SEC), 1)
    )
    if explicit_clock.date() != observed_at.date() or explicit_clock > observed_at + tolerance:
        return False
    return (observed_at - explicit_clock).total_seconds() <= max(
        int(settings.ANOMALY_QUOTE_MAX_AGE_SEC),
        1,
    )


def _pct(price: Any, prev_close: Any) -> float | None:
    value = _safe_float(price)
    anchor = _safe_float(prev_close)
    if value is None or anchor is None or value <= 0 or anchor <= 0:
        return None
    return (value / anchor - 1.0) * 100.0


def _quote_leaf(quote: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: quote.get(key)
        for key in (
            "code",
            "name",
            "price",
            "prev_close",
            "open",
            "high",
            "low",
            "change_pct",
            "amount",
            "volume_ratio",
            "avg_price",
            "main_net_inflow",
            "ask1_price",
            "ask1_volume",
            "bid1_price",
            "bid1_volume",
            "limit_up",
            "limit_down",
            "orderbook_imbalance",
            "source_quote_at",
            "received_at",
            "updated_at",
        )
    }


def _assumed_fill_price(quote: Mapping[str, Any]) -> float | None:
    price = _safe_float(quote.get("price"))
    ask = _safe_float(quote.get("ask1_price"))
    if price is None or price <= 0:
        return None
    base = max(price, ask or 0.0)
    slippage = max(_safe_float(settings.PAPER_EXECUTION_SLIPPAGE_PCT, 0.0) or 0.0, 0.0)
    return round(base * (1.0 + slippage / 100.0), 4)


def _generic_confirmation_metrics(
    quote: Mapping[str, Any],
    market_change_pct: float | None,
) -> dict[str, Any]:
    """Return only owned leaves used by the generic B2/C2/D2/F2 gate."""

    price = _safe_float(quote.get("price"))
    prev_close = _safe_float(quote.get("prev_close"))
    high = _safe_float(quote.get("high"))
    avg_price = _safe_float(quote.get("avg_price"))
    computed_change_pct = _pct(price, prev_close)
    relative_strength_pct = (
        computed_change_pct - market_change_pct
        if computed_change_pct is not None and market_change_pct is not None
        else None
    )
    pullback_pct = (
        (high - price) / high * 100.0
        if high is not None and price is not None and high > 0 and high >= price
        else None
    )
    return {
        "computed_change_pct": (
            round(computed_change_pct, 4)
            if computed_change_pct is not None
            else None
        ),
        "market_median_change_pct": (
            round(market_change_pct, 4)
            if market_change_pct is not None
            else None
        ),
        "relative_strength_pct": (
            round(relative_strength_pct, 4)
            if relative_strength_pct is not None
            else None
        ),
        "avg_price": avg_price,
        "pullback_from_high_pct": (
            round(pullback_pct, 4) if pullback_pct is not None else None
        ),
        "volume_ratio": _safe_float(quote.get("volume_ratio")),
        "orderbook_imbalance": _safe_float(quote.get("orderbook_imbalance")),
        "valid_offer": bool(
            (_safe_float(quote.get("ask1_price")) or 0.0) > 0
            and (_safe_float(quote.get("ask1_volume")) or 0.0) > 0
        ),
    }


def _reclaim_confirmed(
    quote: Mapping[str, Any],
    *,
    min_change_pct: float,
    max_change_pct: float,
    market_change_pct: float | None,
    min_open_pct: float | None = None,
    max_open_pct: float | None = None,
    max_low_pct: float | None = None,
    route_id: str = ROUTE_B,
    evidence: dict | None = None,
) -> bool:
    """Fail closed unless a reclaim is both strong and currently fillable."""

    policy = route_signal_policy(route_id)
    price = _safe_float(quote.get("price"))
    prev_close = _safe_float(quote.get("prev_close"))
    avg_price = _safe_float(quote.get("avg_price"))
    high = _safe_float(quote.get("high"))
    limit_up = _safe_float(quote.get("limit_up"))
    ask = _safe_float(quote.get("ask1_price"))
    ask_volume = _safe_float(quote.get("ask1_volume"))
    change_pct = _pct(price, prev_close)
    volume_ratio = _safe_float(quote.get("volume_ratio"))
    orderbook = _safe_float(quote.get("orderbook_imbalance"))
    relative_strength_pct = (
        change_pct - market_change_pct
        if change_pct is not None and market_change_pct is not None
        else None
    )
    pullback_from_high_pct = (
        (high - price) / high * 100.0
        if high is not None and price is not None and high > 0 and high >= price
        else None
    )
    if evidence is not None:
        evidence.update(relative_strength_pct=relative_strength_pct,
                        pullback_from_high_pct=pullback_from_high_pct,
                        market_change_pct=market_change_pct)
    if (
        price is None
        or prev_close is None
        or price <= 0
        or prev_close <= 0
        or avg_price is None
        or avg_price <= 0
        or high is None
        or high <= 0
        or high < price
        or limit_up is None
        or limit_up <= 0
        or price >= limit_up * 0.998
        or ask is None
        or ask <= 0
        or ask_volume is None
        or ask_volume <= 0
        or pullback_from_high_pct is None
        or pullback_from_high_pct
        > policy["max_pullback_from_high_pct"]
        or price < prev_close
        or price < avg_price
        or change_pct is None
        or not min_change_pct <= change_pct <= max_change_pct
        or market_change_pct is None
        or relative_strength_pct is None
        or relative_strength_pct
        < policy["min_relative_strength_pct"]
        or volume_ratio is None
        or volume_ratio < policy["min_volume_ratio"]
        or orderbook is None
        or orderbook < policy["min_orderbook_imbalance"]
    ):
        return False
    open_pct = _pct(quote.get("open"), prev_close)
    low_pct = _pct(quote.get("low"), prev_close)
    if min_open_pct is not None and (open_pct is None or open_pct < min_open_pct):
        return False
    if max_open_pct is not None and (open_pct is None or open_pct > max_open_pct):
        return False
    if max_low_pct is not None and (low_pct is None or low_pct > max_low_pct):
        return False
    return True


async def _first_board_sector_contexts(
    db: AsyncSession,
    *,
    codes: set[str],
    trade_date: date,
    observed_at: datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Resolve one point-in-time causal sector per code without outcome labels."""

    if not codes:
        return {}, {
            "quote_code_count": 0,
            "mapped_code_count": 0,
            "persistence_context_code_count": 0,
            "causal_context_code_count": 0,
            "structural_context_code_count": 0,
            "reason_counts": {},
        }
    mappings = list(
        (
            await db.scalars(
                select(StockSectorMapping).where(
                    StockSectorMapping.code.in_(codes),
                    StockSectorMapping.source == "pywencai",
                    StockSectorMapping.sector_type.in_(("concept", "industry")),
                    or_(
                        StockSectorMapping.observed_at.is_(None),
                        StockSectorMapping.observed_at <= observed_at,
                    ),
                )
            )
        ).all()
    )
    mapped_codes = {str(row.code or "") for row in mappings}
    sector_codes = {str(row.sector_code or "") for row in mappings if row.sector_code}
    if not sector_codes:
        return {}, {
            "quote_code_count": len(codes),
            "mapped_code_count": len(mapped_codes),
            "persistence_context_code_count": 0,
            "causal_context_code_count": 0,
            "structural_context_code_count": 0,
            "reason_counts": {
                "missing_mapping": len(codes - mapped_codes),
                "missing_current_sector_persistence": len(mapped_codes),
                "non_causal_context_only": 0,
                "sector_clock_unproven": 0,
                "below_structural_sector_floor": 0,
            },
        }
    persistence_rows = list(
        (
            await db.scalars(
                select(SectorPersistence).where(
                    SectorPersistence.trade_date == trade_date,
                    SectorPersistence.sector_code.in_(sector_codes),
                )
            )
        ).all()
    )
    persistence_by_code = {str(row.sector_code or ""): row for row in persistence_rows}

    # Keep the same strict, explainable mapping policy already used by the live
    # anomaly scanner.  The import is local to avoid widening module startup work.
    from app.signal.anomaly_scanner import _is_causal_trade_driver_sector

    contexts_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    persistence_context_codes: set[str] = set()
    causal_context_codes: set[str] = set()
    structural_context_codes: set[str] = set()
    sector_clock_unproven_codes: set[str] = set()
    for mapping in mappings:
        mapping_code = str(mapping.code or "")
        sector_code = str(mapping.sector_code or "")
        persistence = persistence_by_code.get(sector_code)
        sector_name = str(
            getattr(persistence, "sector_name", None)
            or mapping.sector_name
            or ""
        ).strip()
        mapping_leaf = {
            "sector_code": sector_code,
            "sector_name": sector_name,
            "sector_type": str(mapping.sector_type or ""),
            "source": str(mapping.source or ""),
        }
        if persistence is None:
            continue
        persistence_context_codes.add(mapping_code)
        if not _is_causal_trade_driver_sector(mapping_leaf):
            continue
        causal_context_codes.add(mapping_code)
        sector_at = local_clock(getattr(persistence, "observed_at", None))
        if (sector_at is None or sector_at.date() != trade_date
                or sector_at > observed_at):
            # A trade-date upsert may already contain a later intraday/close
            # state. Never splice it into an earlier quote confirmation.
            sector_clock_unproven_codes.add(mapping_code)
            continue
        strength = _safe_float(persistence.strength_score)
        change_pct = _safe_float(persistence.change_pct)
        fund_flow = _safe_float(persistence.fund_flow)
        limit_up_count = int(persistence.limit_up_count or 0)
        if (
            strength is None
            or strength < settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_STRENGTH
            or change_pct is None
            or change_pct <= settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_CHANGE_PCT
            or fund_flow is None
            or fund_flow <= 0
            or limit_up_count
            < settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_LIMIT_UP_COUNT
        ):
            continue
        structural_context_codes.add(mapping_code)
        contexts_by_code[mapping_code].append({
            **mapping_leaf,
            "sector_trade_date": trade_date.isoformat(),
            "sector_observed_at": sector_at.isoformat(),
            "sector_visibility_contract": "c3_sector_observed_clock_v1",
            "sector_strength": round(strength, 4),
            "sector_change_pct": round(change_pct, 4),
            "sector_fund_flow": round(fund_flow, 4),
            "sector_limit_up_count": limit_up_count,
            "sector_consecutive_days": int(persistence.consecutive_days or 0),
            "mapping_source_version": str(mapping.source_version or ""),
            "mapping_observed_at": (
                mapping.observed_at.isoformat()
                if isinstance(mapping.observed_at, datetime)
                else None
            ),
        })

    selected: dict[str, dict[str, Any]] = {}
    for code, contexts in contexts_by_code.items():
        selected[code] = max(
            contexts,
            key=lambda item: (
                _safe_float(item.get("sector_strength"), 0.0) or 0.0,
                int(item.get("sector_limit_up_count") or 0),
                _safe_float(item.get("sector_fund_flow"), 0.0) or 0.0,
                _safe_float(item.get("sector_change_pct"), 0.0) or 0.0,
            ),
        )
    return selected, {
        "quote_code_count": len(codes),
        "mapped_code_count": len(mapped_codes),
        "persistence_context_code_count": len(persistence_context_codes),
        "causal_context_code_count": len(causal_context_codes),
        "structural_context_code_count": len(structural_context_codes),
        "mapping_coverage": round(len(mapped_codes) / max(len(codes), 1), 6),
        "persistence_context_coverage": round(
            len(persistence_context_codes) / max(len(codes), 1),
            6,
        ),
        "reason_counts": {
            "missing_mapping": len(codes - mapped_codes),
            "missing_current_sector_persistence": len(
                mapped_codes - persistence_context_codes
            ),
            "non_causal_context_only": len(
                persistence_context_codes - causal_context_codes
            ),
            "sector_clock_unproven": len(sector_clock_unproven_codes),
            "below_structural_sector_floor": len(
                causal_context_codes - structural_context_codes - sector_clock_unproven_codes
            ),
        },
    }


def _first_board_eligibility(
    quote: Mapping[str, Any],
    sector: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Check pre-confirmation C3 eligibility and return frozen audit leaves."""

    price = _safe_float(quote.get("price"))
    prev_close = _safe_float(quote.get("prev_close"))
    high = _safe_float(quote.get("high"))
    ask = _safe_float(quote.get("ask1_price"))
    ask_volume = _safe_float(quote.get("ask1_volume"))
    limit_up = _safe_float(quote.get("limit_up"))
    change_pct = _pct(price, prev_close)
    volume_ratio = _safe_float(quote.get("volume_ratio"))
    sector_strength = _safe_float(sector.get("sector_strength"))
    sector_change = _safe_float(sector.get("sector_change_pct"))
    sector_flow = _safe_float(sector.get("sector_fund_flow"))
    sector_limit_ups = int(sector.get("sector_limit_up_count") or 0)
    metrics = {
        "computed_change_pct": round(change_pct, 4) if change_pct is not None else None,
        "volume_ratio": volume_ratio,
        "sector_strength": sector_strength,
        "sector_change_pct": sector_change,
        "sector_fund_flow": sector_flow,
        "sector_limit_up_count": sector_limit_ups,
        "valid_offer": bool(ask is not None and ask > 0 and ask_volume is not None and ask_volume > 0),
        "already_touched_limit": bool(
            high is not None and limit_up is not None and limit_up > 0 and high >= limit_up * 0.998
        ),
    }
    # The predicate and its diagnostics have one source of truth. Keep the
    # original thresholds/rounding; these codes are not a looser entry route.
    checks = (
        ("price_invalid", price is not None and price > 0),
        ("prev_close_invalid", prev_close is not None and prev_close > 0),
        ("high_invalid", high is not None and price is not None and high >= price),
        ("limit_up_invalid", limit_up is not None and limit_up > 0),
        ("already_touched_limit", not metrics["already_touched_limit"]),
        ("offer_unavailable", metrics["valid_offer"]),
        ("change_pct_gate", change_pct is not None
         and settings.PAPER_FIRST_BOARD_SHADOW_MIN_CHANGE_PCT <= change_pct
         <= settings.PAPER_FIRST_BOARD_SHADOW_MAX_CHANGE_PCT),
        ("volume_ratio_gate", volume_ratio is not None
         and volume_ratio >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_VOLUME_RATIO),
        ("sector_strength_gate", sector_strength is not None
         and sector_strength >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_STRENGTH),
        ("sector_change_gate", sector_change is not None
         and sector_change >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_CHANGE_PCT),
        ("sector_flow_gate", sector_flow is not None and sector_flow > 0),
        ("sector_limit_up_count_gate",
         sector_limit_ups >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_LIMIT_UP_COUNT),
    )
    metrics["eligibility_gate_issues"] = [code for code, passed in checks if not passed]
    return not metrics["eligibility_gate_issues"], metrics


def _first_board_confirmation_frame(
    quote: Mapping[str, Any],
    sector: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    eligible, metrics = _first_board_eligibility(quote, sector)
    price = _safe_float(quote.get("price"))
    avg_price = _safe_float(quote.get("avg_price"))
    high = _safe_float(quote.get("high"))
    change_pct = _safe_float(metrics.get("computed_change_pct"))
    sector_change = _safe_float(sector.get("sector_change_pct"))
    orderbook = _safe_float(quote.get("orderbook_imbalance"))
    pullback_pct = (
        (high - price) / high * 100.0
        if high is not None and price is not None and high > 0 and high >= price
        else None
    )
    relative_strength_pct = (
        change_pct - sector_change
        if change_pct is not None and sector_change is not None
        else None
    )
    metrics.update({
        "avg_price": avg_price,
        "pullback_from_high_pct": (
            round(pullback_pct, 4) if pullback_pct is not None else None
        ),
        "relative_strength_pct": (
            round(relative_strength_pct, 4)
            if relative_strength_pct is not None
            else None
        ),
        "orderbook_imbalance": orderbook,
    })
    checks = (
        ("vwap_gate", price is not None and avg_price is not None
         and avg_price > 0 and price >= avg_price),
        ("pullback_gate", pullback_pct is not None
         and pullback_pct <= settings.PAPER_FIRST_BOARD_SHADOW_MAX_PULLBACK_FROM_HIGH_PCT),
        ("relative_strength_gate", relative_strength_pct is not None
         and relative_strength_pct >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_RELATIVE_STRENGTH_PCT),
        ("orderbook_gate", orderbook is not None
         and orderbook >= settings.PAPER_STRATEGY_ITERATION_MIN_ORDERBOOK_IMBALANCE),
    )
    metrics["confirmation_gate_issues"] = [
        *metrics["eligibility_gate_issues"],
        *(code for code, passed in checks if not passed),
    ]
    return eligible and not metrics["confirmation_gate_issues"], metrics


def _first_board_confirmation_time(at: datetime) -> bool:
    end = _parse_hhmm(settings.PAPER_FIRST_BOARD_SHADOW_CONFIRM_END, time(14, 30))
    return time(9, 30) <= at.time() <= end and not time(11, 30) < at.time() < time(13, 0)


def _c3_confirmation_history_seconds() -> int:
    # Retain source-age headroom in addition to the proof span and gap margin.
    gap = int(settings.PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC)
    return max(int(settings.ANOMALY_QUOTE_MAX_AGE_SEC), 1) + max(
        int(settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC),
        int(settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES) * gap,
    ) + gap + 1


def _confirmation_streak_status(
    sample_times: Iterable[datetime],
    current_at: datetime,
    *, route_id: str = ROUTE_C3,
) -> dict[str, Any]:
    """Evaluate a persisted, gap-bounded confirmation streak including current_at."""
    policy = route_signal_policy(route_id) if route_id != ROUTE_C3 else {
        "min_samples": settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES,
        "min_persistence_sec": settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC,
        "max_sample_gap_sec": settings.PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC,
        "clock_jitter_sec": settings.PAPER_CONFIRMATION_CLOCK_JITTER_SEC,
    }

    timeline = sorted({
        item
        for item in sample_times
        if isinstance(item, datetime)
        and item.date() == current_at.date()
        and item <= current_at
    })
    if current_at not in timeline:
        timeline.append(current_at)
        timeline.sort()

    max_gap = max(
        int(policy["max_sample_gap_sec"]),
        1,
    )
    streak = [timeline[-1]]
    for item in reversed(timeline[:-1]):
        gap = (streak[-1] - item).total_seconds()
        if gap > max_gap:
            break
        streak.append(item)
    streak.reverse()

    sample_count = len(streak)
    persistence_sec = max(
        (streak[-1] - streak[0]).total_seconds(),
        0.0,
    )
    min_samples = max(
        int(policy["min_samples"]),
        1,
    )
    min_persistence = max(
        int(policy["min_persistence_sec"]),
        0,
    )
    clock_jitter = max(
        0.0,
        float(policy["clock_jitter_sec"]),
    )
    return {
        "ready": (
            sample_count >= min_samples
            and persistence_sec + clock_jitter >= min_persistence
        ),
        "sample_count": sample_count,
        "persistence_sec": round(persistence_sec, 3),
        "min_samples": min_samples,
        "min_persistence_sec": min_persistence,
        "max_sample_gap_sec": max_gap,
        "first_sample_at": streak[0].isoformat(),
        "last_sample_at": streak[-1].isoformat(),
    }


def _first_board_session_observation_health(
    frame_times: Iterable[datetime],
    trade_day: date,
) -> dict[str, Any]:
    """Prove C3 coverage, using a real bounded bracket at the route cutoff.

    A post-cutoff frame proves the endpoint only; it cannot extend confirmation
    time or supply missing in-window samples. Historical versions are not relabeled.
    """

    timeline = sorted({
        item
        for item in frame_times
        if isinstance(item, datetime) and item.date() == trade_day
    })
    first_deadline = _parse_hhmm(
        settings.PAPER_FIRST_BOARD_SHADOW_FIRST_FRAME_DEADLINE,
        time(9, 35),
    )
    morning_last_required = _parse_hhmm(
        settings.PAPER_FIRST_BOARD_SHADOW_MORNING_LAST_NOT_BEFORE,
        time(11, 25),
    )
    afternoon_first_deadline = _parse_hhmm(
        settings.PAPER_FIRST_BOARD_SHADOW_AFTERNOON_FIRST_DEADLINE,
        time(13, 5),
    )
    last_required = _parse_hhmm(
        settings.PAPER_FIRST_BOARD_SHADOW_LAST_FRAME_NOT_BEFORE,
        time(14, 30),
    )
    min_frames = max(
        int(settings.PAPER_FIRST_BOARD_SHADOW_MIN_SESSION_FRAMES),
        1,
    )
    min_morning_frames = max(
        int(settings.PAPER_FIRST_BOARD_SHADOW_MIN_MORNING_FRAMES),
        0,
    )
    min_afternoon_frames = max(
        int(settings.PAPER_FIRST_BOARD_SHADOW_MIN_AFTERNOON_FRAMES),
        0,
    )
    max_gap_allowed = max(
        int(settings.PAPER_FIRST_BOARD_SHADOW_MAX_FRAME_GAP_SEC),
        1,
    )

    morning_frames = [
        item for item in timeline if time(9, 25) <= item.time() <= time(11, 30)
    ]
    afternoon_frames = [
        item for item in timeline if time(13, 0) <= item.time() <= last_required
    ]

    def maximum_gap(values: list[datetime]) -> float | None:
        return max(
            (
                (right - left).total_seconds()
                for left, right in zip(values, values[1:])
            ),
            default=None,
        )

    morning_max_gap = maximum_gap(morning_frames)
    afternoon_max_gap = maximum_gap(afternoon_frames)
    maximum_intrasegment_gap = max(
        (
            value
            for value in (morning_max_gap, afternoon_max_gap)
            if value is not None
        ),
        default=None,
    )
    first_frame_at = morning_frames[0] if morning_frames else None
    morning_last_frame_at = morning_frames[-1] if morning_frames else None
    afternoon_first_frame_at = afternoon_frames[0] if afternoon_frames else None
    last_frame_at = afternoon_frames[-1] if afternoon_frames else None
    cutoff = datetime.combine(trade_day, last_required)
    # Polling generally lands between clock boundaries. Requiring an exact
    # 14:30:00 tick after excluding all later frames made coverage unattainable.
    # The first real later healthy frame may bracket the cutoff, but only across
    # the SAME already-configured maximum gap. It never adds to sample counts.
    after_cutoff = next((
        item for item in timeline
        if cutoff < item <= datetime.combine(trade_day, time(15, 0))
    ), None)
    route_end_bracket_gap = (
        (after_cutoff - last_frame_at).total_seconds()
        if after_cutoff is not None and last_frame_at is not None else None
    )
    route_end_coverage_at = (
        last_frame_at if last_frame_at == cutoff else after_cutoff
        if route_end_bracket_gap is not None and route_end_bracket_gap <= max_gap_allowed
        else None
    )
    observed_frame_count = len(morning_frames) + len(afternoon_frames)
    morning_required = min_morning_frames > 0
    afternoon_required = min_afternoon_frames > 0
    checks = {
        "enough_frames": observed_frame_count >= min_frames,
        "enough_morning_frames": len(morning_frames) >= min_morning_frames,
        "enough_afternoon_frames": len(afternoon_frames) >= min_afternoon_frames,
        "started_on_time": bool(
            not morning_required
            or (first_frame_at is not None and first_frame_at.time() <= first_deadline)
        ),
        "covered_morning_end": bool(
            not morning_required
            or (
                morning_last_frame_at is not None
                and morning_last_frame_at.time() >= morning_last_required
            )
        ),
        "afternoon_started_on_time": bool(
            not afternoon_required
            or (
                afternoon_first_frame_at is not None
                and afternoon_first_frame_at.time() <= afternoon_first_deadline
            )
        ),
        "covered_route_end": bool(
            not afternoon_required
            or route_end_coverage_at is not None
        ),
        "morning_gap_acceptable": bool(
            not morning_required
            or (morning_max_gap is not None and morning_max_gap <= max_gap_allowed)
        ),
        "afternoon_gap_acceptable": bool(
            not afternoon_required
            or (
                afternoon_max_gap is not None
                and afternoon_max_gap <= max_gap_allowed
            )
        ),
    }
    return {
        "complete": all(checks.values()),
        "frame_count": observed_frame_count,
        "provided_frame_count": len(timeline),
        "coverage_semantics": "bracketed_route_end_v2",
        "minimum_frame_count": min_frames,
        "morning_frame_count": len(morning_frames),
        "minimum_morning_frame_count": min_morning_frames,
        "afternoon_frame_count": len(afternoon_frames),
        "minimum_afternoon_frame_count": min_afternoon_frames,
        "first_frame_at": first_frame_at.isoformat() if first_frame_at else None,
        "first_frame_deadline": first_deadline.isoformat(timespec="minutes"),
        "morning_last_frame_at": (
            morning_last_frame_at.isoformat() if morning_last_frame_at else None
        ),
        "morning_last_not_before": morning_last_required.isoformat(timespec="minutes"),
        "afternoon_first_frame_at": (
            afternoon_first_frame_at.isoformat() if afternoon_first_frame_at else None
        ),
        "afternoon_first_deadline": afternoon_first_deadline.isoformat(
            timespec="minutes"
        ),
        "last_frame_at": last_frame_at.isoformat() if last_frame_at else None,
        "route_end_coverage_at": (
            route_end_coverage_at.isoformat() if route_end_coverage_at else None
        ),
        "route_end_bracket_gap_sec": (
            0.0 if last_frame_at == cutoff else route_end_bracket_gap
        ),
        "last_frame_not_before": last_required.isoformat(timespec="minutes"),
        "morning_maximum_gap_sec": morning_max_gap,
        "afternoon_maximum_gap_sec": afternoon_max_gap,
        "maximum_intrasegment_gap_sec": maximum_intrasegment_gap,
        "maximum_allowed_gap_sec": max_gap_allowed,
        "checks": checks,
    }


def _rules(route_id: str) -> dict[str, Any]:
    common = {
        "quote_numeric_contract": "finite_nonbool_quote_v1",
        "shadow_only": True,
        "champion_order_connected": False,
        "challenger_paper_account_connected": bool(
            settings.PAPER_CHALLENGER_ACCOUNT_ENABLED and route_id != ROUTE_C3
        ),
        "evidence_only_without_execution_account": route_id == ROUTE_C3,
        "real_order_connected": False,
        "min_volume_ratio": settings.PAPER_STRATEGY_ITERATION_MIN_VOLUME_RATIO,
        "min_orderbook_imbalance": (
            settings.PAPER_STRATEGY_ITERATION_MIN_ORDERBOOK_IMBALANCE
        ),
        "valid_offer_required": route_id != ROUTE_C3,
        "relative_strength_benchmark": (
            "same_frame_tradeable_quote_median"
            if route_id != ROUTE_C3
            else "same_frame_live_mainline_sector"
        ),
        "min_relative_strength_pct": (
            settings.PAPER_STRATEGY_ITERATION_MIN_RELATIVE_STRENGTH_PCT
            if route_id != ROUTE_C3
            else settings.PAPER_FIRST_BOARD_SHADOW_MIN_RELATIVE_STRENGTH_PCT
        ),
        "min_vwap_slope_pct": (
            settings.PAPER_STRATEGY_ITERATION_MIN_VWAP_SLOPE_PCT
            if route_id != ROUTE_C3
            else None
        ),
        "max_pullback_from_high_pct": (
            settings.PAPER_STRATEGY_ITERATION_MAX_PULLBACK_FROM_HIGH_PCT
        ),
        "confirmation_min_samples": (
            settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES
        ),
        "confirmation_min_persistence_sec": (
            settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC
        ),
        "confirmation_max_sample_gap_sec": (
            settings.PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC
        ),
        "promotion_min_sessions": settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_SESSIONS,
        "promotion_min_samples": settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_SAMPLES,
    }
    route_rules = {
        ROUTE_B: {
            "structure": "previous_session_first_board",
            "open_pct": [
                settings.PAPER_STRATEGY_B_WEAK_OPEN_MIN_PCT,
                settings.PAPER_STRATEGY_B_WEAK_OPEN_MAX_PCT,
            ],
            "reclaim_pct": [
                settings.PAPER_STRATEGY_B_RECLAIM_MIN_PCT,
                settings.PAPER_STRATEGY_B_RECLAIM_MAX_PCT,
            ],
            "confirm_end": settings.PAPER_STRATEGY_B_WEAK_OPEN_CONFIRM_END,
        },
        ROUTE_C: {
            "confirmed_path_contract": "c2_confirmed_path_terminal_v1",
            "structure": "latest_limit_up_then_1_to_4_break_sessions",
            "gap_sessions": [
                settings.PAPER_STRATEGY_C_RELAUNCH_MIN_GAP_SESSIONS,
                settings.PAPER_STRATEGY_C_RELAUNCH_MAX_GAP_SESSIONS,
            ],
            "max_open_pct": settings.PAPER_STRATEGY_C_RELAUNCH_MAX_OPEN_PCT,
            "max_low_pct": settings.PAPER_STRATEGY_C_RELAUNCH_MAX_LOW_PCT,
            "reclaim_pct": [
                settings.PAPER_STRATEGY_C_RELAUNCH_MIN_RECLAIM_PCT,
                settings.PAPER_STRATEGY_C_RELAUNCH_MAX_RECLAIM_PCT,
            ],
        },
        ROUTE_C3: {
            "continuity_semantics": "c3_continuity_v2",
            "sector_visibility_contract": "c3_sector_observed_clock_v1",
            "source_quote_clock_required": True,
            "source_quote_max_age_sec": settings.ANOMALY_QUOTE_MAX_AGE_SEC,
            "confirmation_clock_jitter_sec": settings.PAPER_CONFIRMATION_CLOCK_JITTER_SEC,
            "valid_offer_required": True,
            "orderbook_required": True,
            "confirm_start": "09:30",
            "lunch_excluded": ["11:30", "13:00"],
            "structure": "fresh_first_board_member_of_live_mainline_sector",
            "activation_date": settings.PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE,
            "recent_limit_lookback_sessions": (
                settings.PAPER_FIRST_BOARD_SHADOW_RECENT_LIMIT_LOOKBACK_SESSIONS
            ),
            "minimum_quote_coverage": (
                settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE
            ),
            "structural_sector_floor": {
                "strength": settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_STRENGTH,
                "change_pct": settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_CHANGE_PCT,
                "limit_up_count": settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_LIMIT_UP_COUNT,
                "fund_flow_positive": True,
            },
            "eligible_sector_floor": {
                "strength": settings.PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_STRENGTH,
                "change_pct": settings.PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_CHANGE_PCT,
                "limit_up_count": settings.PAPER_FIRST_BOARD_SHADOW_MIN_SECTOR_LIMIT_UP_COUNT,
                "fund_flow_positive": True,
            },
            "confirm_change_pct": [
                settings.PAPER_FIRST_BOARD_SHADOW_MIN_CHANGE_PCT,
                settings.PAPER_FIRST_BOARD_SHADOW_MAX_CHANGE_PCT,
            ],
            "min_volume_ratio": settings.PAPER_FIRST_BOARD_SHADOW_MIN_VOLUME_RATIO,
            "min_relative_strength_pct": (
                settings.PAPER_FIRST_BOARD_SHADOW_MIN_RELATIVE_STRENGTH_PCT
            ),
            "max_pullback_from_high_pct": (
                settings.PAPER_FIRST_BOARD_SHADOW_MAX_PULLBACK_FROM_HIGH_PCT
            ),
            "confirm_end": settings.PAPER_FIRST_BOARD_SHADOW_CONFIRM_END,
            "session_observation": {
                "coverage_semantics": "bracketed_route_end_v2",
                "minimum_frames": settings.PAPER_FIRST_BOARD_SHADOW_MIN_SESSION_FRAMES,
                "minimum_morning_frames": settings.PAPER_FIRST_BOARD_SHADOW_MIN_MORNING_FRAMES,
                "minimum_afternoon_frames": settings.PAPER_FIRST_BOARD_SHADOW_MIN_AFTERNOON_FRAMES,
                "first_frame_deadline": settings.PAPER_FIRST_BOARD_SHADOW_FIRST_FRAME_DEADLINE,
                "morning_last_not_before": settings.PAPER_FIRST_BOARD_SHADOW_MORNING_LAST_NOT_BEFORE,
                "afternoon_first_deadline": settings.PAPER_FIRST_BOARD_SHADOW_AFTERNOON_FIRST_DEADLINE,
                "last_frame_not_before": settings.PAPER_FIRST_BOARD_SHADOW_LAST_FRAME_NOT_BEFORE,
                "maximum_frame_gap_sec": settings.PAPER_FIRST_BOARD_SHADOW_MAX_FRAME_GAP_SEC,
            },
            "control_definition": "eligible_without_persistent_confirmation_by_route_cutoff",
            "same_day_outcome_is_not_promotion_evidence": True,
        },
        ROUTE_D: {
            "structure": "observed_pre0920_negative_auction_then_0925_recovery",
            "path_selection": "verified_source_clock_first_v2",
            "baseline_max_pct": settings.PAPER_STRATEGY_D_AUCTION_BASELINE_MAX_PCT,
            "final_min_pct": settings.PAPER_STRATEGY_D_AUCTION_FINAL_MIN_PCT,
            "min_recovery_ppt": settings.PAPER_STRATEGY_D_AUCTION_MIN_RECOVERY_PPT,
            "min_cancel_phase_samples": (
                settings.PAPER_STRATEGY_D_AUCTION_MIN_CANCEL_PHASE_SAMPLES
            ),
            "requires_positive_volume_path": True,
            "auction_evidence_contract": "auction_provenance_v1",
            "auction_source_max_age_sec": settings.AUCTION_SOURCE_MAX_AGE_SEC,
            "reclaim_pct": [
                settings.PAPER_STRATEGY_D_AUCTION_RECLAIM_MIN_PCT,
                settings.PAPER_STRATEGY_D_AUCTION_RECLAIM_MAX_PCT,
            ],
            "confirm_end": settings.PAPER_STRATEGY_D_AUCTION_CONFIRM_END,
        },
        ROUTE_F2: {
            "structure": "high_board_then_1_to_3_break_sessions_reclaim",
            "min_highboard": settings.PAPER_STRATEGY_F2_MIN_HIGHBOARD,
            "break_sessions": [
                settings.PAPER_STRATEGY_F2_MIN_BREAK_SESSIONS,
                settings.PAPER_STRATEGY_F2_MAX_BREAK_SESSIONS,
            ],
            "max_open_pct": settings.PAPER_STRATEGY_F2_MAX_OPEN_PCT,
            "max_low_pct": settings.PAPER_STRATEGY_F2_MAX_LOW_PCT,
            "reclaim_pct": [
                settings.PAPER_STRATEGY_F2_MIN_RECLAIM_PCT,
                settings.PAPER_STRATEGY_F2_MAX_RECLAIM_PCT,
            ],
        },
    }
    if route_id != ROUTE_C3:
        policy = route_signal_policy(route_id)
        common.update({
            key: policy[key] for key in ("min_volume_ratio", "min_orderbook_imbalance",
                "min_relative_strength_pct", "min_vwap_slope_pct", "max_pullback_from_high_pct")
        })
        common.update({
            "confirmation_min_samples": policy["min_samples"],
            "confirmation_min_persistence_sec": policy["min_persistence_sec"],
            "confirmation_max_sample_gap_sec": policy["max_sample_gap_sec"],
            "confirmation_clock_jitter_sec": policy["clock_jitter_sec"],
        })
    return {**common, **route_rules[route_id]}


def _event(
    *,
    route_id: str,
    trade_date: date,
    observed_at: datetime,
    code: str,
    name: str,
    event_type: str,
    status: str,
    quote: Mapping[str, Any] | None,
    prior: Mapping[str, Any],
) -> dict[str, Any]:
    version = route_version_for(route_id)
    event_suffix = event_type
    if event_type in {"confirmation_sample", "universe_audit"}:
        # Static structural/eligible/confirmed events remain one-per-day, while
        # every confirmation or healthy-universe frame is append-only and
        # survives restarts.  Universe frames prove that "unconfirmed" did not
        # merely mean the scanner was offline for the rest of the session.
        prefix = "cs" if event_type == "confirmation_sample" else "ua"
        event_suffix = f"{prefix}:{observed_at:%H%M%S}"
    if event_type in {"confirmation_sample", "confirmation_reset", "coverage_blocked"}:
        event_suffix = f"{event_type}:{observed_at:%H%M%S%f}"
    if route_id == ROUTE_C3 and event_type in {"universe_audit", "coverage_blocked"}:
        # v2 must not lose a negative/unknown audit behind a same-second
        # positive frame; first-hit and current-pool boundaries must agree.
        prefix = "cb" if event_type == "coverage_blocked" else "ua"
        event_suffix = f"{prefix}:{observed_at:%H%M%S%f}"
    event_key = (
        f"{route_id}:{version}:{trade_date.isoformat()}:{code}:{event_suffix}"
    )
    quote_payload = _quote_leaf(quote or {})
    snapshot = {
        "as_of_at": observed_at.isoformat(),
        "analysis_trade_date": trade_date.isoformat(),
        "point_in_time_only": True,
        "causal_status": "forward_shadow_hypothesis",
        "eligible_for_production": False,
        "route_id": route_id,
        "route_version": version,
        "rule_snapshot": _rules(route_id),
        "quote": quote_payload,
        "prior_structure": dict(prior),
        "guardrail": (
            "事件只用于候选分母和前向收益评估；不得从单日涨停结果反推生产阈值。"
        ),
    }
    confirmed = event_type == "confirmed"
    return {
        "event_key": event_key,
        "route_id": route_id,
        "route_version": version,
        "trade_date": trade_date,
        "observed_at": observed_at,
        "code": code,
        "name": name[:20],
        "event_type": event_type,
        "status": status,
        "price": _safe_float((quote or {}).get("price")),
        "assumed_fill_price": _assumed_fill_price(quote or {}) if confirmed else None,
        "change_pct": _safe_float((quote or {}).get("change_pct")),
        "snapshot_json": json.dumps(snapshot, ensure_ascii=False, default=str),
        "created_at": datetime.now(),
    }


async def _allowed_codes(db: AsyncSession, trade_date: date) -> set[str]:
    tags = await db.scalars(
        select(StockTag.code).where(
            StockTag.board_tag == "tradeable",
            StockTag.is_st.is_(False),
            StockTag.is_suspended.is_(False),
            StockTag.is_delisting.is_(False),
        )
    )
    blocked = await db.scalars(
        select(StockBlacklist.code).where(
            StockBlacklist.start_date <= trade_date,
            or_(
                StockBlacklist.end_date.is_(None),
                StockBlacklist.end_date >= trade_date,
            ),
        )
    )
    blocked_codes = {str(code) for code in blocked.all()}
    return {
        str(code)
        for code in tags.all()
        if str(code) not in blocked_codes
        and stock_tagger.is_tradeable(str(code))
    }


async def _previous_trade_dates(
    db: AsyncSession,
    trade_date: date,
    count: int,
) -> list[date]:
    result = await db.scalars(
        select(StockKline.trade_date)
        .where(StockKline.trade_date < trade_date)
        .distinct()
        .order_by(desc(StockKline.trade_date))
        .limit(max(count, 1))
    )
    return list(result.all())


async def _persist_events(
    db: AsyncSession,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not events:
        return []
    event_keys = [item["event_key"] for item in events]
    existing = set(
        (
            await db.scalars(
                select(PaperShadowEvent.event_key).where(
                    PaperShadowEvent.event_key.in_(event_keys)
                )
            )
        ).all()
    )
    new_rows = [item for item in events if item["event_key"] not in existing]
    for offset in range(0, len(new_rows), _EVENT_BATCH_SIZE):
        batch = new_rows[offset:offset + _EVENT_BATCH_SIZE]
        statement = sqlite_insert(PaperShadowEvent).values(batch)
        statement = statement.on_conflict_do_nothing(index_elements=["event_key"])
        await db.execute(statement)
    if new_rows:
        await db.commit()
    return new_rows


async def _load_confirmation_state(
    db: AsyncSession,
    trade_date: date,
    observed_at: datetime,
    route_versions: dict[str, str],
    c3_history_sec: int,
):
    """Replay complete confirmation state; close history before C3 identity read.

    Only local state changes here. Any read/processing/cleanup failure propagates;
    transaction ownership and event persistence remain with the caller.
    """
    import asyncio

    prior_confirmation_events = await db.stream(
        select(
            PaperShadowEvent.id,
            PaperShadowEvent.route_id,
            PaperShadowEvent.route_version,
            PaperShadowEvent.code,
            PaperShadowEvent.event_type,
            PaperShadowEvent.observed_at,
            PaperShadowEvent.snapshot_json,
        ).where(
            PaperShadowEvent.trade_date == trade_date,
            or_(*(and_(
                PaperShadowEvent.route_id == route_id,
                PaperShadowEvent.route_version == version,
            ) for route_id, version in route_versions.items())),
            PaperShadowEvent.observed_at <= observed_at,
            or_(
                PaperShadowEvent.route_id != ROUTE_C3,
                and_(
                    PaperShadowEvent.route_version == route_versions[ROUTE_C3],
                    PaperShadowEvent.observed_at >= observed_at - timedelta(seconds=c3_history_sec),
                ),
            ),
            PaperShadowEvent.event_type.in_(
                ("confirmation_sample", "confirmation_reset", "confirmed")
            ),
        ).order_by(PaperShadowEvent.observed_at, PaperShadowEvent.id).execution_options(yield_per=128)
    )
    try:
        confirmation_samples: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        confirmation_avg_prices: dict[
            tuple[str, str], dict[datetime, float]
        ] = defaultdict(dict)
        confirmation_sources: dict[tuple[str, str], dict[datetime, datetime]] = defaultdict(dict)
        latest_sources: dict[tuple[str, str], datetime] = {}
        confirmed_route_codes: set[tuple[str, str]] = set()
        async for partition in prior_confirmation_events.partitions(128):
            for event_row in partition:
                route_code = (str(event_row.route_id), str(event_row.code))
                if event_row.event_type == "confirmation_reset":
                    confirmation_samples[route_code].clear()
                    confirmation_avg_prices[route_code].clear()
                    confirmation_sources[route_code].clear()
                    boundary = local_clock(_parse_datetime(
                        _json_dict(_json_dict(event_row.snapshot_json).get("prior_structure")).get("source_boundary_at")
                    ))
                    if boundary is not None:
                        latest_sources[route_code] = max(boundary, latest_sources.get(route_code, boundary))
                elif event_row.event_type == "confirmation_sample":
                    confirmation_samples[route_code].append(event_row.observed_at)
                    snapshot = _json_dict(event_row.snapshot_json)
                    quote_leaf = (
                        snapshot.get("quote")
                        if isinstance(snapshot.get("quote"), dict)
                        else {}
                    )
                    source_at = local_clock(_parse_datetime(quote_leaf.get("source_quote_at")))
                    if source_at is not None:
                        confirmation_sources[route_code][event_row.observed_at] = source_at
                        latest_sources[route_code] = max(source_at, latest_sources.get(route_code, source_at))
                    average = _safe_float(quote_leaf.get("avg_price"))
                    if average is not None and average > 0:
                        confirmation_avg_prices[route_code][event_row.observed_at] = average
                elif event_row.event_type == "confirmed":
                    confirmed_route_codes.add(route_code)
            await asyncio.sleep(0)
    except BaseException:
        try:
            await prior_confirmation_events.close()
        except BaseException:
            pass  # Preserve the active read/processing/cancellation exception.
        raise
    else:
        await prior_confirmation_events.close()
    confirmed_route_codes.update(
        (ROUTE_C3, str(code)) for code in (await db.scalars(
            select(PaperShadowEvent.code).where(
                PaperShadowEvent.route_id == ROUTE_C3,
                PaperShadowEvent.route_version == route_versions[ROUTE_C3],
                PaperShadowEvent.trade_date == trade_date,
                PaperShadowEvent.observed_at <= observed_at,
                PaperShadowEvent.event_type == "confirmed",
            )
        )).all()
    )
    return confirmation_samples, confirmation_avg_prices, confirmation_sources, latest_sources, confirmed_route_codes


async def scan_strategy_iteration_shadow(
    db: AsyncSession,
    quotes: Iterable[Mapping[str, Any]],
    observed_at: datetime,
) -> dict[str, Any]:
    """Append B/C/C3/D/F structural and confirmation events; never return orders."""

    observed_at = local_clock(observed_at)
    if observed_at is None:
        for route_id in ROUTE_IDS:
            _capture_candidate_projection(route_id, datetime.now(), stage="not_scanned",
                                          reason="invalid_observation_clock")
        return {"events": 0, "confirmed": 0, "routes": {}}
    if not (
        settings.PAPER_STRATEGY_ITERATION_SHADOW_ENABLED
        or settings.PAPER_FIRST_BOARD_SHADOW_ENABLED
    ):
        for route_id in ROUTE_IDS:
            _capture_candidate_projection(route_id, observed_at, stage="not_scanned",
                                          reason="producer_disabled")
        return {"events": 0, "confirmed": 0, "routes": {}}
    if observed_at.time() < time(9, 25) or observed_at.time() > time(14, 50):
        for route_id in ROUTE_IDS:
            _capture_candidate_projection(route_id, observed_at, stage="not_scanned",
                                          reason="outside_producer_window")
        return {"events": 0, "confirmed": 0, "routes": {}}

    trade_date = observed_at.date()
    allowed = await _allowed_codes(db, trade_date)
    submitted_quotes = {
        str(item.get("code") or "").strip(): item for item in quotes
    }
    quote_by_code = {
        code: item for code, item in submitted_quotes.items()
        if code in allowed and _quote_clock_valid(item, observed_at)
    }
    # Even an empty/invalid batch must invalidate previously active generic
    # streaks. Do not return before their durable reset events are written.
    market_change_values = [
        value
        for quote in quote_by_code.values()
        if (value := _pct(quote.get("price"), quote.get("prev_close"))) is not None
        and -31.0 <= value <= 31.0
    ]
    quote_coverage = len(quote_by_code) / max(len(allowed), 1)
    market_frame_usable = bool(
        market_change_values
        and quote_coverage >= settings.PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE
    )
    market_change_pct = (
        float(median(market_change_values)) if market_frame_usable else None
    )
    market_frame_audit = {
        "allowed_code_count": len(allowed),
        "valid_quote_count": len(quote_by_code),
        "valid_change_count": len(market_change_values),
        "quote_coverage": round(quote_coverage, 6),
        "minimum_quote_coverage": (
            settings.PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE
        ),
        "benchmark": "same_frame_tradeable_quote_median",
        "market_median_change_pct": (
            round(market_change_pct, 4)
            if market_change_pct is not None
            else None
        ),
        "usable": market_frame_usable,
    }

    route_versions = {route_id: route_version_for(route_id) for route_id in ROUTE_IDS}
    # C3 needs only enough recent state to establish a bounded streak. Keep
    # immutable first-hit identities separately; never replay all-day universe JSON.
    # The window exceeds source freshness: a source at/before an excluded reset
    # cannot become admissible merely because that reset fell outside this query.
    c3_history_sec = _c3_confirmation_history_seconds()
    (
        confirmation_samples,
        confirmation_avg_prices,
        confirmation_sources,
        latest_sources,
        confirmed_route_codes,
    ) = await _load_confirmation_state(
        db, trade_date, observed_at, route_versions, c3_history_sec,
    )

    # Only track still-executable first C2 tokens. Read small identity leaves,
    # not the all-day universe, and stop after TTL or a durable segment reset.
    active_confirmed_c = {}
    if any(route == ROUTE_C for route, _code in confirmed_route_codes):
        ttl = challenger_execution_policy(ROUTE_C)["max_execution_delay_sec"]
        confirmed_rows = await db.execute(select(
            PaperShadowEvent.code, PaperShadowEvent.event_key, PaperShadowEvent.observed_at,
        ).where(
            PaperShadowEvent.route_id == ROUTE_C,
            PaperShadowEvent.route_version == route_versions[ROUTE_C],
            PaperShadowEvent.trade_date == trade_date,
            PaperShadowEvent.event_type == "confirmed",
            PaperShadowEvent.observed_at <= observed_at,
            PaperShadowEvent.observed_at >= observed_at - timedelta(seconds=ttl),
        ))
        active_confirmed_c = {str(row.code): row for row in confirmed_rows
                              if confirmation_samples.get((ROUTE_C, str(row.code)))}

    events: list[dict[str, Any]] = []
    pending_keys: set[str] = set()
    handled_route_codes: set[tuple[str, str]] = set()
    research_rules = {route_id: _rules(route_id) for route_id in ROUTE_IDS}

    def project(route_id, code, *, stage, reason, gate=None, candidate=True,
                confirmed=None, inputs=None, metrics=None, evidence_ref=None,
                source_contract=None):
        _capture_candidate_projection(
            route_id, observed_at, code=code, quote=submitted_quotes.get(code),
            stage=stage, reason=reason, original_candidate=candidate,
            original_confirmed=confirmed, original_gate=gate, gate_inputs=inputs,
            identities={"broken_board_identity": True} if route_id == ROUTE_F2 and candidate else {},
            rules=research_rules[route_id], version=route_versions[route_id],
            metrics=metrics, evidence_ref=evidence_ref, source_contract=source_contract,
        )

    def emit(item: dict[str, Any]) -> None:
        if item["event_key"] not in pending_keys:
            pending_keys.add(item["event_key"])
            events.append(item)
            if item["event_type"] in {"confirmed", "coverage_blocked", "evidence_blocked"}:
                # Event existence is not the static gate. Confirmation metrics are
                # consumed only from the already-created event, never re-evaluated.
                snapshot = _json_dict(item["snapshot_json"])
                prior_evidence = snapshot.get("prior_structure") or {}
                project(item["route_id"], item["code"], stage=item["event_type"],
                        reason=item["status"], candidate=item["code"] != "MARKET",
                        confirmed=True if item["event_type"] == "confirmed" else None,
                        inputs=prior_evidence,
                        metrics=prior_evidence.get("confirmation_metrics"),
                        evidence_ref=item["event_key"])

    def reset_confirmation(route_code: tuple[str, str], reason: str) -> None:
        route_id, code = route_code
        confirmed_event = active_confirmed_c.get(code) if route_id == ROUTE_C else None
        if route_id == ROUTE_C and route_code in confirmed_route_codes:
            if (confirmed_event is None or not confirmation_samples.get(route_code)
                    or observed_at <= max(confirmation_samples[route_code])):
                # Expired/ended tokens and duplicate observations are not new boundaries.
                return
        quote = submitted_quotes.get(code)
        project(route_id, code, stage="reset", reason=reason, confirmed=False,
                inputs={"reset_reason": reason})
        # After a negative/unknown observation, delayed source frames from
        # before that boundary must not start a supposedly recovered segment.
        source_at = local_clock(_parse_datetime((quote or {}).get("source_quote_at")))
        boundary = source_at if reason == "observation_or_source_gap" else observed_at
        latest_sources[route_code] = max(boundary, latest_sources.get(route_code, boundary))
        emit(_event(
            route_id=route_id, trade_date=trade_date, observed_at=observed_at,
            code=code, name=str((quote or {}).get("name") or code),
            event_type="confirmation_reset", status="reset", quote=quote,
            prior={"reason": reason, "market_frame_audit": market_frame_audit,
                   **({"confirmed_path_contract": "c2_confirmed_path_terminal_v1",
                       "invalidates_event_key": confirmed_event.event_key,
                       "confirmed_at": confirmed_event.observed_at.isoformat(),
                       # Missing coverage breaks proof, not a claim the market fell.
                       "invalidation_kind": "path_continuity_lost"}
                      if confirmed_event is not None else {}),
                   "source_boundary_at": boundary.isoformat(),
                   "confirmation_version": (
                       "c3_continuity_v2" if route_id == ROUTE_C3 else "persistent_observed_segments_v3"
                   )},
        ))
        confirmation_samples[route_code].clear()
        confirmation_avg_prices[route_code].clear()
        confirmation_sources[route_code].clear()

    # Preserve C3's empty-frame audit even when there is no historical K-line
    # context; unlike the old early return, generic resets still get persisted.
    activation = _first_board_activation_date()
    if (not quote_by_code and settings.PAPER_FIRST_BOARD_SHADOW_ENABLED
            and activation is not None and trade_date >= activation
            and trade_date == date.today()):
        emit(_event(
            route_id=ROUTE_C3, trade_date=trade_date, observed_at=observed_at,
            code="MARKET", name="首板路线行情覆盖审计",
            event_type="coverage_blocked", status="coverage_blocked", quote=None,
            prior={"allowed_count": len(allowed), "valid_quote_count": 0,
                   "quote_coverage": 0.0, "reason": "无有效同时点行情，当前池失效"},
        ))

    lookback_count = max(
        int(settings.PAPER_STRATEGY_C_RELAUNCH_LOOKBACK_SESSIONS),
        int(settings.PAPER_STRATEGY_F2_MAX_BREAK_SESSIONS) + 2,
        int(settings.PAPER_FIRST_BOARD_SHADOW_RECENT_LIMIT_LOOKBACK_SESSIONS),
    )
    previous_dates = await _previous_trade_dates(db, trade_date, lookback_count)
    if not previous_dates:
        for route_id in ROUTE_IDS:
            project(route_id, "MARKET", stage="not_scanned", reason="missing_prior_trade_dates",
                    candidate=False, inputs={"valid_quote_count": len(quote_by_code)})
        for route_code in list(confirmation_samples):
            if (route_code not in confirmed_route_codes
                    or (route_code[0] == ROUTE_C and route_code[1] in active_confirmed_c)):
                reset_confirmation(route_code, "missing_prior_trade_dates")
        added = await _persist_events(db, events)
        return {"events": len(added), "confirmed": 0, "routes": {}}
    previous_date = previous_dates[0]
    date_gap = {trade_day: index + 1 for index, trade_day in enumerate(previous_dates)}

    limit_rows = list(
        (
            await db.scalars(
                select(LimitUpPool)
                .where(
                    LimitUpPool.trade_date.in_(previous_dates),
                    LimitUpPool.quarantined.is_(False),
                )
                .order_by(desc(LimitUpPool.trade_date), desc(LimitUpPool.id))
            )
        ).all()
    )
    latest_limit_by_code: dict[str, LimitUpPool] = {}
    for row in limit_rows:
        latest_limit_by_code.setdefault(str(row.code or ""), row)

    previous_first_boards = {
        str(row.code or ""): row
        for row in limit_rows
        if row.trade_date == previous_date and int(row.consecutive_days or 1) == 1
    }

    if (
        settings.PAPER_STRATEGY_ITERATION_SHADOW_ENABLED
        and not market_frame_usable
    ):
        for route_id in (ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_F2):
            emit(
                _event(
                    route_id=route_id,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    code="MARKET",
                    name="横截面行情覆盖审计",
                    event_type="coverage_blocked",
                    status="coverage_blocked",
                    quote=None,
                    prior={
                        **market_frame_audit,
                        "reason": "同一时点可交易行情覆盖不足，禁止生成相对强度确认",
                    },
                )
            )

    def emit_confirmation_frame(
        *,
        route_id: str,
        code: str,
        name: str,
        quote: Mapping[str, Any],
        prior: Mapping[str, Any],
    ) -> None:
        """Persist one qualifying frame, then confirm only a durable streak."""

        route_code = (route_id, code)
        already_confirmed = route_code in confirmed_route_codes
        # C2 keeps observing the original path after its first confirmation.
        # A reset clears its segment permanently; never mint/revive the day token.
        if already_confirmed and (route_id != ROUTE_C or code not in active_confirmed_c):
            return
        source_at = local_clock(_parse_datetime(quote.get("source_quote_at")))
        handled_route_codes.add(route_code)
        # Receipt/scan time is not a provider sample. Unknown or future
        # source clocks cannot manufacture persistence.
        if (source_at is None or source_at.date() != trade_date
                or source_at > observed_at
                or (route_id == ROUTE_C3 and not _first_board_confirmation_time(source_at))):
            reset_confirmation(route_code, "unknown_or_future_source_clock")
            return
        latest_source = latest_sources.get(route_code)
        if latest_source is not None and source_at <= latest_source:
            if (already_confirmed and confirmation_samples.get(route_code)
                    and (observed_at - max(confirmation_samples[route_code])).total_seconds()
                    > route_signal_policy(route_id)["max_sample_gap_sec"]):
                reset_confirmation(route_code, "repeated_source_continuity_gap")
                return
            if source_at < latest_source and confirmation_samples.get(route_code):
                reset_confirmation(route_code, "source_clock_regressed")
            # An empty segment is waiting for its persisted recovery
            # boundary, not observing a regression of an active streak.
            # Reject old frames without moving that boundary: otherwise
            # allowed source latency longer than one poll can livelock.
            # Re-fetching a source frame neither counts nor extends duration.
            return
        samples = confirmation_samples.get(route_code, [])
        if samples:
            last_at = max(samples)
            last_source = confirmation_sources[route_code].get(last_at)
            max_gap = (
                settings.PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC
                if route_id == ROUTE_C3 else route_signal_policy(route_id)["max_sample_gap_sec"]
            )
            if (last_source is None or observed_at <= last_at
                    or (observed_at - last_at).total_seconds() > max_gap
                    or (source_at - last_source).total_seconds() > max_gap):
                reset_confirmation(route_code, "observation_or_source_gap")
                if already_confirmed:
                    return
        status = _confirmation_streak_status(
            confirmation_samples.get(route_code, []),
            observed_at,
            route_id=route_id,
        )
        source_status = _confirmation_streak_status(
            confirmation_sources[route_code].values(), source_at, route_id=route_id,
        )
        status["ready"] = bool(status["ready"] and source_status["ready"])
        status["source_persistence_sec"] = source_status["persistence_sec"]
        status["source_sample_count"] = source_status["sample_count"]
        prior_payload = dict(prior)
        generic_route = route_id != ROUTE_C3
        current_average = _safe_float(quote.get("avg_price"))
        averages = dict(confirmation_avg_prices.get(route_code, {}))
        if current_average is not None and current_average > 0:
            averages[observed_at] = current_average
        first_sample_at = _parse_datetime(status.get("first_sample_at"))
        first_average = averages.get(first_sample_at) if first_sample_at else None
        vwap_slope_pct = (
            (current_average / first_average - 1.0) * 100.0
            if generic_route
            and current_average is not None
            and current_average > 0
            and first_average is not None
            and first_average > 0
            else None
        )
        vwap_slope_passed = bool(
            not generic_route
            or (
                vwap_slope_pct is not None
                and vwap_slope_pct
                >= route_signal_policy(route_id)["min_vwap_slope_pct"]
            )
        )
        if already_confirmed and not vwap_slope_passed:
            reset_confirmation(route_code, "confirmed_vwap_slope_not_met")
            return
        ready = bool(status["ready"] and vwap_slope_passed)
        confirmation = {
            **status,
            "ready": ready,
            "time_streak_ready": bool(status["ready"]),
            "vwap_slope_pct": (
                round(vwap_slope_pct, 6)
                if vwap_slope_pct is not None
                else None
            ),
            "minimum_vwap_slope_pct": (
                route_signal_policy(route_id)["min_vwap_slope_pct"]
                if generic_route
                else None
            ),
            "vwap_slope_passed": vwap_slope_passed,
            "confirmation_version": (
                "persistent_observed_segments_v3"
                if generic_route
                else "c3_continuity_v2"
            ),
        }
        if generic_route:
            existing_metrics = (
                prior_payload.get("confirmation_metrics")
                if isinstance(prior_payload.get("confirmation_metrics"), dict)
                else {}
            )
            prior_payload["confirmation_metrics"] = {
                **existing_metrics,
                **_generic_confirmation_metrics(quote, market_change_pct),
                "vwap_slope_pct": confirmation["vwap_slope_pct"],
            }
            prior_payload["market_frame_audit"] = market_frame_audit
        emit(
            _event(
                route_id=route_id,
                trade_date=trade_date,
                observed_at=observed_at,
                code=code,
                name=name,
                event_type="confirmation_sample",
                status="ready" if ready else "waiting",
                quote=quote,
                prior={**prior_payload, "confirmation": confirmation},
            )
        )
        if observed_at not in confirmation_samples[route_code]:
            confirmation_samples[route_code].append(observed_at)
        confirmation_sources[route_code][observed_at] = source_at
        latest_sources[route_code] = source_at
        if current_average is not None and current_average > 0:
            confirmation_avg_prices[route_code][observed_at] = current_average
        if not ready or already_confirmed:
            return
        emit(
            _event(
                route_id=route_id,
                trade_date=trade_date,
                observed_at=observed_at,
                code=code,
                name=name,
                event_type="confirmed",
                status="confirmed",
                quote=quote,
                prior={**prior_payload, "confirmation": confirmation},
            )
        )
        confirmed_route_codes.add(route_code)

    # B: yesterday's first board, but today's official open is weak/negative and
    # the live quote later reclaims both the zero axis and VWAP.
    b_end = _parse_hhmm(
        settings.PAPER_STRATEGY_B_WEAK_OPEN_CONFIRM_END,
        time(10, 30),
    )
    for code, prior_row in previous_first_boards.items():
        quote = quote_by_code.get(code)
        if quote is None:
            project(ROUTE_B, code, stage="unknown", reason="missing_or_invalid_quote",
                    inputs={"signal_trade_date": previous_date.isoformat(),
                            "previous_consecutive_days": 1})
            continue
        prior = {
            "signal_trade_date": previous_date.isoformat(),
            "previous_consecutive_days": 1,
            "previous_limit_up_time": prior_row.limit_up_time,
            "previous_break_count": int(prior_row.break_count or 0),
        }
        emit(
            _event(
                route_id=ROUTE_B,
                trade_date=trade_date,
                observed_at=observed_at,
                code=code,
                name=str(quote.get("name") or prior_row.name or code),
                event_type="structural_pool",
                status="observed",
                quote=quote,
                prior=prior,
            )
        )
        open_pct = _pct(quote.get("open"), quote.get("prev_close"))
        weak_open_setup = bool(
            open_pct is not None
            and settings.PAPER_STRATEGY_B_WEAK_OPEN_MIN_PCT
            <= open_pct
            <= settings.PAPER_STRATEGY_B_WEAK_OPEN_MAX_PCT
        )
        if weak_open_setup:
            emit(
                _event(
                    route_id=ROUTE_B,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    code=code,
                    name=str(quote.get("name") or prior_row.name or code),
                    event_type="eligible",
                    status="eligible",
                    quote=quote,
                    prior={**prior, "official_open_pct": round(open_pct, 4)},
                )
            )
        frame_metrics = {}
        frame_gate = weak_open_setup and observed_at.time() <= b_end and _reclaim_confirmed(
            quote,
            route_id=ROUTE_B, min_change_pct=settings.PAPER_STRATEGY_B_RECLAIM_MIN_PCT,
            max_change_pct=settings.PAPER_STRATEGY_B_RECLAIM_MAX_PCT,
            market_change_pct=market_change_pct,
            min_open_pct=settings.PAPER_STRATEGY_B_WEAK_OPEN_MIN_PCT,
            max_open_pct=settings.PAPER_STRATEGY_B_WEAK_OPEN_MAX_PCT,
            evidence=frame_metrics,
        )
        project(ROUTE_B, code, stage="static_gate", reason="passed" if frame_gate else "failed",
                gate=frame_gate, confirmed=((ROUTE_B, code) in confirmed_route_codes),
                inputs={"structure": prior, "setup_passed": weak_open_setup,
                        "market_frame_audit": market_frame_audit}, metrics=frame_metrics)
        if frame_gate:
            emit_confirmation_frame(
                route_id=ROUTE_B,
                code=code,
                name=str(quote.get("name") or prior_row.name or code),
                quote=quote,
                prior=prior,
            )

    # C/F2 share the same latest historical limit-up row but remain disjoint:
    # C consumes one/two-board memory; F2 consumes high-board memory.
    missing_structure_coverage = {
        route: {"missing_quote_count": 0, "missing_gap_count": 0,
                "unknown_structure_count": 0, "unknown_detail_count": 0,
                "unknown_aggregated_count": 0}
        for route in (ROUTE_C, ROUTE_F2)
    }
    for code, prior_row in latest_limit_by_code.items():
        quote = quote_by_code.get(code)
        gap_sessions = date_gap.get(prior_row.trade_date)
        if quote is None or gap_sessions is None:
            # Research-only receipt: a historical board family is NOT proof of
            # today's structural eligibility. Bound detail, retain all unknowns.
            try:
                boards = int(prior_row.consecutive_days or 1)
                missing_route = (ROUTE_C if boards <= 2 else ROUTE_F2
                    if boards >= settings.PAPER_STRATEGY_F2_MIN_HIGHBOARD else None)
                if missing_route is not None:
                    coverage = missing_structure_coverage[missing_route]
                    coverage["missing_quote_count"] += int(quote is None)
                    coverage["missing_gap_count"] += int(gap_sessions is None)
                    coverage["unknown_structure_count"] += 1
                    if coverage["unknown_detail_count"] < 16:
                        coverage["unknown_detail_count"] += 1
                        project(missing_route, code, stage="unknown",
                                reason="missing_quote_or_gap", candidate=False,
                                inputs={"missing_quote": quote is None,
                                        "missing_gap": gap_sessions is None,
                                        "last_consecutive_days": boards,
                                        "structural_eligibility": "unknown"})
                    else:
                        coverage["unknown_aggregated_count"] += 1
            except Exception:
                pass
            continue
        consecutive = int(prior_row.consecutive_days or 1)
        if (
            consecutive <= 2
            and settings.PAPER_STRATEGY_C_RELAUNCH_MIN_GAP_SESSIONS
            <= gap_sessions
            <= settings.PAPER_STRATEGY_C_RELAUNCH_MAX_GAP_SESSIONS
        ):
            break_sessions = gap_sessions - 1
            prior = {
                "last_limit_trade_date": prior_row.trade_date.isoformat(),
                "last_consecutive_days": consecutive,
                "break_sessions_before_today": break_sessions,
                "last_limit_up_time": prior_row.limit_up_time,
            }
            emit(
                _event(
                    route_id=ROUTE_C,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    code=code,
                    name=str(quote.get("name") or prior_row.name or code),
                    event_type="structural_pool",
                    status="observed",
                    quote=quote,
                    prior=prior,
                )
            )
            open_pct = _pct(quote.get("open"), quote.get("prev_close"))
            low_pct = _pct(quote.get("low"), quote.get("prev_close"))
            relaunch_setup = bool(
                open_pct is not None
                and low_pct is not None
                and open_pct <= settings.PAPER_STRATEGY_C_RELAUNCH_MAX_OPEN_PCT
                and low_pct <= settings.PAPER_STRATEGY_C_RELAUNCH_MAX_LOW_PCT
            )
            if relaunch_setup:
                emit(
                    _event(
                        route_id=ROUTE_C,
                        trade_date=trade_date,
                        observed_at=observed_at,
                        code=code,
                        name=str(quote.get("name") or prior_row.name or code),
                        event_type="eligible",
                        status="eligible",
                        quote=quote,
                        prior={
                            **prior,
                            "official_open_pct": round(open_pct, 4),
                            "intraday_low_pct": round(low_pct, 4),
                        },
                    )
                )
            frame_metrics = {}
            frame_gate = relaunch_setup and (observed_at.time() <= time(14, 30)
                or code in active_confirmed_c) and _reclaim_confirmed(
                quote,
                route_id=ROUTE_C, min_change_pct=settings.PAPER_STRATEGY_C_RELAUNCH_MIN_RECLAIM_PCT,
                max_change_pct=settings.PAPER_STRATEGY_C_RELAUNCH_MAX_RECLAIM_PCT,
                market_change_pct=market_change_pct,
                max_open_pct=settings.PAPER_STRATEGY_C_RELAUNCH_MAX_OPEN_PCT,
                max_low_pct=settings.PAPER_STRATEGY_C_RELAUNCH_MAX_LOW_PCT,
                evidence=frame_metrics,
            )
            project(ROUTE_C, code, stage="static_gate", reason="passed" if frame_gate else "failed",
                    gate=frame_gate, confirmed=((ROUTE_C, code) in confirmed_route_codes),
                    inputs={"structure": prior, "setup_passed": relaunch_setup,
                            "market_frame_audit": market_frame_audit}, metrics=frame_metrics)
            if frame_gate:
                emit_confirmation_frame(
                    route_id=ROUTE_C,
                    code=code,
                    name=str(quote.get("name") or prior_row.name or code),
                    quote=quote,
                    prior=prior,
                )

        break_sessions = gap_sessions - 1
        if (
            consecutive >= settings.PAPER_STRATEGY_F2_MIN_HIGHBOARD
            and settings.PAPER_STRATEGY_F2_MIN_BREAK_SESSIONS
            <= break_sessions
            <= settings.PAPER_STRATEGY_F2_MAX_BREAK_SESSIONS
        ):
            prior = {
                "last_highboard_trade_date": prior_row.trade_date.isoformat(),
                "last_consecutive_days": consecutive,
                "break_sessions_before_today": break_sessions,
                "last_limit_up_time": prior_row.limit_up_time,
            }
            emit(
                _event(
                    route_id=ROUTE_F2,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    code=code,
                    name=str(quote.get("name") or prior_row.name or code),
                    event_type="structural_pool",
                    status="observed",
                    quote=quote,
                    prior=prior,
                )
            )
            open_pct = _pct(quote.get("open"), quote.get("prev_close"))
            low_pct = _pct(quote.get("low"), quote.get("prev_close"))
            reclaim_setup = bool(
                open_pct is not None
                and low_pct is not None
                and open_pct <= settings.PAPER_STRATEGY_F2_MAX_OPEN_PCT
                and low_pct <= settings.PAPER_STRATEGY_F2_MAX_LOW_PCT
            )
            if reclaim_setup:
                emit(
                    _event(
                        route_id=ROUTE_F2,
                        trade_date=trade_date,
                        observed_at=observed_at,
                        code=code,
                        name=str(quote.get("name") or prior_row.name or code),
                        event_type="eligible",
                        status="eligible",
                        quote=quote,
                        prior={
                            **prior,
                            "official_open_pct": round(open_pct, 4),
                            "intraday_low_pct": round(low_pct, 4),
                        },
                    )
                )
            frame_metrics = {}
            frame_gate = reclaim_setup and observed_at.time() <= time(14, 30) and _reclaim_confirmed(
                quote,
                route_id=ROUTE_F2, min_change_pct=settings.PAPER_STRATEGY_F2_MIN_RECLAIM_PCT,
                max_change_pct=settings.PAPER_STRATEGY_F2_MAX_RECLAIM_PCT,
                market_change_pct=market_change_pct,
                max_open_pct=settings.PAPER_STRATEGY_F2_MAX_OPEN_PCT,
                max_low_pct=settings.PAPER_STRATEGY_F2_MAX_LOW_PCT,
                evidence=frame_metrics,
            )
            project(ROUTE_F2, code, stage="static_gate", reason="passed" if frame_gate else "failed",
                    gate=frame_gate, confirmed=((ROUTE_F2, code) in confirmed_route_codes),
                    inputs={"structure": prior, "setup_passed": reclaim_setup,
                            "market_frame_audit": market_frame_audit}, metrics=frame_metrics)
            if frame_gate:
                emit_confirmation_frame(
                    route_id=ROUTE_F2,
                    code=code,
                    name=str(quote.get("name") or prior_row.name or code),
                    quote=quote,
                    prior=prior,
                )

    # C3: all fresh first-board candidates inside an already observable live
    # mainline sector form the denominator.  Eligibility and persistent
    # confirmation are narrower subsets; final-board outcomes are appended only
    # after formal close bars become available.
    activation_date = _first_board_activation_date()
    c3_active = bool(
        settings.PAPER_FIRST_BOARD_SHADOW_ENABLED
        # Missing or malformed activation configuration is fail-closed.
        and activation_date is not None
        and trade_date >= activation_date
        # SectorPersistence has no immutable intraday observation timestamp.
        # Therefore C3 is live-only in production: accepting a replayed clock
        # could pair an old quote with a sector row updated after that cutoff.
        and trade_date == date.today()
    )
    if c3_active:
        current_members: list[dict[str, Any]] = []
        quote_coverage = len(quote_by_code) / max(len(allowed), 1)
        if quote_coverage < settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE:
            emit(
                _event(
                    route_id=ROUTE_C3,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    code="MARKET",
                    name="首板路线行情覆盖审计",
                    event_type="coverage_blocked",
                    status="coverage_blocked",
                    quote=None,
                    prior={
                        "allowed_count": len(allowed),
                        "valid_quote_count": len(quote_by_code),
                        "quote_coverage": round(quote_coverage, 6),
                        "minimum_quote_coverage": (
                            settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE
                        ),
                        "reason": "可交易池同时点行情覆盖不足，禁止生成不完整候选分母",
                    },
                )
            )
        else:
            sector_contexts, sector_audit = await _first_board_sector_contexts(
                db,
                codes=set(quote_by_code),
                trade_date=trade_date,
                observed_at=observed_at,
            )
            mapped_code_count = int(sector_audit.get("mapped_code_count") or 0)
            mapping_coverage = mapped_code_count / max(len(quote_by_code), 1)
            persistence_context_coverage = _safe_float(
                sector_audit.get("persistence_context_coverage"),
                0.0,
            ) or 0.0
            if mapping_coverage < settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE:
                emit(
                    _event(
                        route_id=ROUTE_C3,
                        trade_date=trade_date,
                        observed_at=observed_at,
                        code="MARKET",
                        name="首板路线板块映射审计",
                        event_type="coverage_blocked",
                        status="coverage_blocked",
                        quote=None,
                        prior={
                            "valid_quote_count": len(quote_by_code),
                            "mapped_code_count": mapped_code_count,
                            "mapping_coverage": round(mapping_coverage, 6),
                            "minimum_mapping_coverage": (
                                settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE
                            ),
                            "context_audit": sector_audit,
                            "reason": "可交易池板块映射覆盖不足，禁止生成偏置分母",
                        },
                    )
                )
            elif (
                persistence_context_coverage
                < settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE
            ):
                emit(
                    _event(
                        route_id=ROUTE_C3,
                        trade_date=trade_date,
                        observed_at=observed_at,
                        code="MARKET",
                        name="首板路线板块截面审计",
                        event_type="coverage_blocked",
                        status="coverage_blocked",
                        quote=None,
                        prior={
                            "valid_quote_count": len(quote_by_code),
                            "persistence_context_coverage": round(
                                persistence_context_coverage,
                                6,
                            ),
                            "minimum_context_coverage": (
                                settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE
                            ),
                            "context_audit": sector_audit,
                            "reason": "当日板块持续性截面覆盖不足，禁止静默收缩候选分母",
                        },
                    )
                )
            else:
                emit(
                    _event(
                        route_id=ROUTE_C3,
                        trade_date=trade_date,
                        observed_at=observed_at,
                        code="MARKET",
                        name="首板路线完整分母审计",
                        event_type="universe_audit",
                        status="complete",
                        quote=None,
                        prior={
                            "allowed_count": len(allowed),
                            "valid_quote_count": len(quote_by_code),
                            "quote_coverage": round(quote_coverage, 6),
                            "context_audit": sector_audit,
                            "outcome_fields_used": False,
                        },
                    )
                )
                confirm_end = _parse_hhmm(
                    settings.PAPER_FIRST_BOARD_SHADOW_CONFIRM_END,
                    time(14, 30),
                )
                recent_limit_lookback = max(
                    int(
                        settings.PAPER_FIRST_BOARD_SHADOW_RECENT_LIMIT_LOOKBACK_SESSIONS
                    ),
                    1,
                )
                for code, sector in sorted(sector_contexts.items()):
                    quote = quote_by_code.get(code)
                    if quote is None:
                        continue
                    latest_limit = latest_limit_by_code.get(code)
                    if (
                        latest_limit is not None
                        and 1 <= int(date_gap.get(latest_limit.trade_date) or 0)
                        <= recent_limit_lookback
                    ):
                        # Recent-board memories belong to C2, not this fresh-board
                        # hypothesis.  Excluding them before any same-day outcome
                        # is known keeps the route definitions disjoint.
                        continue
                    prior = {
                        "denominator_role": "fresh_first_board_active_sector",
                        "recent_limit_lookback_sessions": recent_limit_lookback,
                        "recent_limit_found": False,
                        "quote_universe_count": len(quote_by_code),
                        "quote_coverage": round(quote_coverage, 6),
                        "mapping_coverage": round(mapping_coverage, 6),
                        "persistence_context_coverage": round(
                            persistence_context_coverage,
                            6,
                        ),
                        **sector,
                    }
                    emit(
                        _event(
                            route_id=ROUTE_C3,
                            trade_date=trade_date,
                            observed_at=observed_at,
                            code=code,
                            name=str(quote.get("name") or code),
                            event_type="structural_pool",
                            status="observed",
                            quote=quote,
                            prior=prior,
                        )
                    )
                    eligible, eligibility = _first_board_eligibility(quote, sector)
                    member = {
                        "code": code, "name": str(quote.get("name") or code),
                        "eligible": eligible, "confirmation_frame": False,
                        "gate_issues": eligibility["eligibility_gate_issues"],
                        "source_quote_at": (
                            local_clock(_parse_datetime(quote.get("source_quote_at"))).isoformat()
                            if local_clock(_parse_datetime(quote.get("source_quote_at"))) is not None else None
                        ),
                        "reason": (
                            "卖一价格/数量无效" if not eligibility["valid_offer"]
                            else "今日已触涨停，不再满足首板前资格" if eligibility["already_touched_limit"]
                            else "个股/板块资格门槛未满足" if not eligible
                            else "等待确认门槛"
                        ),
                    }
                    current_members.append(member)
                    if not eligible:
                        project(ROUTE_C3, code, stage="static_gate", reason="eligibility_failed",
                                gate=False, confirmed=((ROUTE_C3, code) in confirmed_route_codes),
                                inputs={"eligibility": eligibility, "structure": prior})
                        continue
                    eligible_prior = {
                        **prior,
                        "eligibility": eligibility,
                        "control_anchor_if_unconfirmed": True,
                    }
                    emit(
                        _event(
                            route_id=ROUTE_C3,
                            trade_date=trade_date,
                            observed_at=observed_at,
                            code=code,
                            name=str(quote.get("name") or code),
                            event_type="eligible",
                            status="eligible",
                            quote=quote,
                            prior=eligible_prior,
                        )
                    )
                    confirmed_frame, confirmation_metrics = (
                        _first_board_confirmation_frame(quote, sector)
                    )
                    member["confirmation_frame"] = bool(
                        _first_board_confirmation_time(observed_at)
                        and confirmed_frame
                        # v2 first-hit and current view both reject unknown book.
                        and _safe_float(quote.get("orderbook_imbalance")) is not None
                    )
                    member["gate_issues"] = [
                        *confirmation_metrics["confirmation_gate_issues"],
                        *([] if _first_board_confirmation_time(observed_at)
                          else ["outside_confirmation_window"]),
                    ]
                    member["reason"] = (
                        "等待逐帧连续确认" if member["confirmation_frame"]
                        else "超出确认时段" if observed_at.time() > confirm_end
                        else "本帧未通过确认门槛"
                    )
                    project(ROUTE_C3, code, stage="static_gate", reason=member["reason"],
                            gate=member["confirmation_frame"],
                            confirmed=((ROUTE_C3, code) in confirmed_route_codes),
                            inputs={"eligibility": eligibility, "confirmation_metrics": confirmation_metrics,
                                    "gate_issues": member["gate_issues"], "structure": prior},
                            metrics=confirmation_metrics)
                    if member["confirmation_frame"]:
                        emit_confirmation_frame(
                            route_id=ROUTE_C3,
                            code=code,
                            name=str(quote.get("name") or code),
                            quote=quote,
                            prior={
                                **eligible_prior,
                                "confirmation_metrics": confirmation_metrics,
                            },
                        )

        # Enrich only the new audit before INSERT; never rewrite stored evidence.
        for item in events:
            if item["route_id"] == ROUTE_C3 and item["event_type"] == "universe_audit":
                snapshot = _json_dict(item["snapshot_json"])
                snapshot["prior_structure"]["current_pool"] = {
                    "read_model_version": "c3_current_pool_v1",
                    "members": current_members,
                    "quote_round_ids": sorted({
                        str(q["quote_round_id"]) for q in quote_by_code.values()
                        if q.get("quote_round_id")
                    }),
                }
                item["snapshot_json"] = json.dumps(snapshot, ensure_ascii=False, default=str)

    # D requires a real indicative path.  Official open alone is explicitly not
    # sufficient; missing pre-09:20 plus final-09:25 coverage produces one market
    # audit event instead of silently labelling every negative open as recovery.
    auction_rows = list(
        (
            await db.scalars(
                select(AuctionData)
                .where(
                    AuctionData.trade_date == trade_date,
                    AuctionData.code.in_(set(quote_by_code)),
                )
                .order_by(AuctionData.code, AuctionData.auction_time, AuctionData.id)
            )
        ).all()
    )
    auctions_by_code: dict[str, list[AuctionData]] = defaultdict(list)
    for row in auction_rows:
        auctions_by_code[str(row.code or "")].append(row)

    path_covered_codes = 0
    verified_path_codes = 0
    d_end = _parse_hhmm(
        settings.PAPER_STRATEGY_D_AUCTION_CONFIRM_END,
        time(10, 0),
    )
    for code, rows in auctions_by_code.items():
        early: list[tuple[AuctionData, float]] = []
        cancel_phase: list[tuple[AuctionData, float]] = []
        final: list[tuple[AuctionData, float]] = []
        for row in rows:
            status = auction_evidence_status(row, decision_at=observed_at, require_ratio=False)
            if status == "future":
                continue
            source_clock = local_clock(row.source_quote_at)
            row_time = (
                source_clock.time() if status == "ok" else _auction_time(row.auction_time)
            )
            # 上游普通 spot 在正式开盘前可能把“缺失”写成价格 0、涨幅 -100；
            # 必须以有效价格和昨收重算，绝不能把缺失值冒充跌停竞价。
            row_change = _pct(row.auction_price, row.prev_close)
            if row_time is None or row_change is None or not time(9, 15) <= row_time <= time(9, 25, 30):
                continue
            if row_time < time(9, 20):
                early.append((row, row_change))
            elif time(9, 20) <= row_time < time(9, 25):
                cancel_phase.append((row, row_change))
            elif time(9, 25) <= row_time < time(9, 30):
                final.append((row, row_change))
        if not early or not final:
            continue
        path_covered_codes += 1
        # 普通spot占位行只保留作诊断；不得以更低的伪指示价或更晚的接收
        # 标签挤掉已有的真实竞价证据。有效末段按源时钟排序。
        verified_early = [item for item in early if auction_evidence_status(
            item[0], decision_at=observed_at, require_ratio=False) == "ok"]
        verified_final = [item for item in final if auction_evidence_status(
            item[0], decision_at=observed_at, require_ratio=False) == "ok"]
        baseline_row, baseline_pct = min(verified_early or early, key=lambda item: item[1])
        final_row, final_pct = max(
            verified_final or final,
            key=lambda item: (
                (
                    local_clock(item[0].source_quote_at).time()
                    if verified_final else _auction_time(item[0].auction_time) or time.min
                ),
                # Independent source frames can share a provider second. Keep
                # source-clock priority; break ties by actual observation only.
                local_clock(item[0].observed_at) or datetime.min,
                local_clock(item[0].received_at) or datetime.min,
                item[0].id or 0,
            ),
        )
        recovery_ppt = final_pct - baseline_pct
        quote = quote_by_code.get(code)
        if quote is None:
            continue

        baseline_volume = _safe_float(baseline_row.auction_volume, 0.0) or 0.0
        final_volume = _safe_float(final_row.auction_volume, 0.0) or 0.0
        positive_cancel_rows = [
            row
            for row, _ in cancel_phase
            if auction_evidence_status(row, decision_at=observed_at, require_ratio=False) == "ok"
        ]
        distinct_source_samples = len({
            local_clock(row.source_quote_at) for row in positive_cancel_rows
        })
        cancel_phase_verified = distinct_source_samples >= max(
            int(settings.PAPER_STRATEGY_D_AUCTION_MIN_CANCEL_PHASE_SAMPLES),
            1,
        )
        volume_path_verified = bool(
            auction_evidence_status(baseline_row, decision_at=observed_at, require_ratio=False) == "ok"
            and auction_evidence_status(final_row, decision_at=observed_at, require_ratio=False) == "ok"
            and cancel_phase_verified
        )
        verified_path_codes += int(volume_path_verified)
        prior = {
            "baseline_time": baseline_row.auction_time,
            "baseline_change_pct": round(baseline_pct, 4),
            "baseline_volume": int(baseline_volume),
            "final_time": final_row.auction_time,
            "final_change_pct": round(final_pct, 4),
            "final_volume": int(final_volume),
            "recovery_ppt": round(recovery_ppt, 4),
            "indicative_path_coverage": (
                "price_and_positive_volume_path"
                if volume_path_verified
                else "price_only_unverified_volume"
            ),
            "auction_volume_path_verified": volume_path_verified,
            # 保留旧API键名；09:20-09:25为不可撤单阶段，快照不证明撤单行为。
            "cancel_phase_verified": cancel_phase_verified,
            "cancel_phase_positive_samples": distinct_source_samples,
            "auction_evidence_contract": "auction_provenance_v1",
            "baseline_evidence_status": auction_evidence_status(
                baseline_row, decision_at=observed_at, require_ratio=False
            ),
            "final_evidence_status": auction_evidence_status(
                final_row, decision_at=observed_at, require_ratio=False
            ),
            "baseline_volume_unit": baseline_row.volume_unit,
            "final_volume_unit": final_row.volume_unit,
            "cancel_action_observed": any(
                bool(getattr(row, "is_cancelled", False))
                for row, _ in cancel_phase
            ),
        }
        emit(
            _event(
                route_id=ROUTE_D,
                trade_date=trade_date,
                observed_at=observed_at,
                code=code,
                name=str(quote.get("name") or code),
                event_type="structural_pool",
                status="observed",
                quote=quote,
                prior=prior,
            )
        )
        # Reuse the source decisions above; no query or auction predicate replay.
        try:
            source_rows = [baseline_row, *positive_cancel_rows, final_row]
            source_clocks = [local_clock(row.source_quote_at) for row in source_rows]
            visibility = [local_clock(row.observed_at) for row in source_rows]
            source_ref = "auction:" + ":".join(str(row.id) for row in source_rows)
            project(ROUTE_D, code, stage="source_contract", reason=prior["indicative_path_coverage"],
                    inputs=prior, source_contract={
                        "verified_early": bool(verified_early),
                        "two_distinct_verified_middle": cancel_phase_verified,
                        "verified_final": bool(verified_final),
                        "evidence_ref": source_ref,
                        "evidence_at": max(source_clocks) if all(source_clocks) else None,
                        "observed_at": max(visibility) if all(visibility) else None,
                    })
        except Exception:
            pass
        if not volume_path_verified:
            emit(
                _event(
                    route_id=ROUTE_D,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    code=code,
                    name=str(quote.get("name") or code),
                    event_type="evidence_blocked",
                    status="evidence_blocked",
                    quote=quote,
                    prior={
                        **prior,
                        "reason": "竞价路径未验证：缺少早段/不可撤单阶段/最终段的源时钟及匹配量价证据，无法判定修复形态",
                    },
                )
            )
            continue
        if (
            baseline_pct > settings.PAPER_STRATEGY_D_AUCTION_BASELINE_MAX_PCT
            or final_pct < settings.PAPER_STRATEGY_D_AUCTION_FINAL_MIN_PCT
            or recovery_ppt < settings.PAPER_STRATEGY_D_AUCTION_MIN_RECOVERY_PPT
        ):
            continue
        emit(
            _event(
                route_id=ROUTE_D,
                trade_date=trade_date,
                observed_at=observed_at,
                code=code,
                name=str(quote.get("name") or code),
                event_type="eligible",
                status="eligible",
                quote=quote,
                prior=prior,
            )
        )
        frame_metrics = {}
        frame_gate = observed_at.time() <= d_end and _reclaim_confirmed(
            quote,
            route_id=ROUTE_D, min_change_pct=settings.PAPER_STRATEGY_D_AUCTION_RECLAIM_MIN_PCT,
            max_change_pct=settings.PAPER_STRATEGY_D_AUCTION_RECLAIM_MAX_PCT,
            market_change_pct=market_change_pct,
            evidence=frame_metrics,
        )
        project(ROUTE_D, code, stage="static_gate", reason="passed" if frame_gate else "failed",
                gate=frame_gate, confirmed=((ROUTE_D, code) in confirmed_route_codes),
                inputs={"structure": prior, "setup_passed": volume_path_verified,
                        "market_frame_audit": market_frame_audit}, metrics=frame_metrics)
        if frame_gate:
            emit_confirmation_frame(
                route_id=ROUTE_D,
                code=code,
                name=str(quote.get("name") or code),
                quote=quote,
                prior=prior,
            )

    if verified_path_codes == 0:
        emit(
            _event(
                route_id=ROUTE_D,
                trade_date=trade_date,
                observed_at=observed_at,
                code="MARKET",
                name="竞价覆盖审计",
                event_type="coverage_blocked",
                status="coverage_blocked",
                quote=None,
                prior={
                    "auction_row_count": len(auction_rows),
                    "quote_universe_count": len(quote_by_code),
                    "raw_price_path_codes": path_covered_codes,
                    "verified_path_codes": verified_path_codes,
                    "reason": "缺少同股09:20前、09:20-09:25及最终段完整可验证竞价路径",
                    "official_open_is_not_indicative_path": True,
                },
            )
        )

    # Every previously active or currently eligible candidate must have
    # passed this frame's complete route gate. Missing quotes, coverage, route
    # structure or reclaim evidence are durable boundaries, not skipped samples.
    watched = set(confirmation_samples) | {
        (item["route_id"], item["code"]) for item in events
        if item["event_type"] == "eligible"
    }
    terminal_or_untracked = {
        key for key in confirmed_route_codes
        if key[0] != ROUTE_C or key[1] not in active_confirmed_c
        or not confirmation_samples.get(key)
    }
    for route_code in sorted(watched - handled_route_codes - terminal_or_untracked):
        reason = (
            "missing_or_invalid_quote" if route_code[1] not in quote_by_code
            else "market_coverage_unknown" if not market_frame_usable
            else "route_condition_failed_or_unknown"
        )
        reset_confirmation(route_code, reason)

    for route_id in ROUTE_IDS:
        c3_disabled = route_id == ROUTE_C3 and not settings.PAPER_FIRST_BOARD_SHADOW_ENABLED
        project(route_id, "MARKET", stage="not_scanned" if c3_disabled else "scan_complete",
                reason="producer_disabled" if c3_disabled else "producer_scan_completed",
                candidate=False, inputs={
                    **missing_structure_coverage.get(route_id, {}),
                    "valid_quote_count": len(quote_by_code),
                    "structural_count": sum(item["route_id"] == route_id
                        and item["event_type"] == "structural_pool" for item in events),
                    "events_attempted": sum(item["route_id"] == route_id for item in events),
                })
    added_rows = await _persist_events(db, events)
    confirmed = sum(
        1
        for item in added_rows
        if item["event_type"] == "confirmed"
    )
    universe_audit_frames = sum(
        item["event_type"] == "universe_audit" for item in added_rows
    )
    route_counts: dict[str, int] = defaultdict(int)
    for item in events:
        route_counts[item["route_id"]] += 1
    return {
        "events": len(added_rows),
        "material_events": len(added_rows) - universe_audit_frames,
        "universe_audit_frames": universe_audit_frames,
        "events_attempted": len(events),
        "confirmed": confirmed,
        "routes": dict(route_counts),
        "execution_enabled": False,
    }


def project_first_board_current_pool(
    frames: Iterable[PaperShadowEvent],
    *,
    now: datetime,
    cumulative_confirmed_codes: Iterable[str] = (),
    invalid_quote_times: Iterable[datetime] = (),
    history_start_at: datetime | None = None,
    history_complete: bool = True,
) -> dict[str, Any]:
    """Versioned read-only projection, never an execution or evaluation input.

    Unlike frozen first-hit evidence, confirmation here requires *every* intervening
    audit to pass. Missing membership, coverage failure and stale gaps reset it.
    """
    rows = sorted(
        (r for r in frames if r.route_id == ROUTE_C3
         and r.route_version == route_version_for(ROUTE_C3)
         and r.trade_date == now.date() and r.observed_at <= now
         and r.event_type in {"universe_audit", "coverage_blocked"}),
        key=lambda r: (r.observed_at, r.id or 0),
    )
    cumulative = set(cumulative_confirmed_codes)
    latest = rows[-1] if rows else None
    as_of = latest.observed_at if latest else None
    max_age = min(
        max(int(settings.ANOMALY_QUOTE_MAX_AGE_SEC), 1),
        max(int(settings.PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC), 1),
    )
    # Decode each universe once, not once per candidate per frame.
    priors = [_json_dict(row.snapshot_json).get("prior_structure", {}) for row in rows]
    states = [p.get("current_pool", {}) for p in priors]
    membership = [{m["code"]: m for m in state.get("members", [])} for state in states]
    prior = priors[-1] if priors else {}
    current = states[-1] if states else {}
    reason = None
    if not latest:
        reason = "今日尚无扫描帧；不沿用昨日候选"
    elif latest.event_type != "universe_audit":
        reason = prior.get("reason") or "本帧覆盖不完整"
    elif current.get("read_model_version") != "c3_current_pool_v1":
        reason = "旧审计未保存当帧成员；不能从历史累计推断当前池"
    elif (now - as_of).total_seconds() > max_age:
        reason = "扫描帧已过期"
    confirm_end = _parse_hhmm(settings.PAPER_FIRST_BOARD_SHADOW_CONFIRM_END, time(14, 30))
    if not (
        time(9, 30) <= now.time() <= confirm_end
        and not time(11, 30) < now.time() < time(13, 0)
    ):
        reason = "非盘中确认时段"
    activation = _first_board_activation_date()
    if not settings.PAPER_FIRST_BOARD_SHADOW_ENABLED or activation is None or now.date() < activation:
        reason = "C3采证未激活"
    bad_times = sorted(t for t in invalid_quote_times if t.date() == now.date() and t <= now)
    if as_of and bad_times and bad_times[-1] >= as_of:
        reason = "最新行情轮次质量异常或未完成扫描"
    if not history_complete:
        reason = "历史观测读取已截断，无法证明持续确认的左边界"
    valid = reason is None
    members = []
    latest_members = current.get("members", []) if valid else []
    for leaf in latest_members:
        code = str(leaf["code"])
        samples = []
        source_samples = []
        newer_source = None
        # Counting stops at a gap; known failures must not disappear with it.
        # Inspect already-decoded bounded leaves independently of that walk.
        # Keep the actual failure clock, never move it to a delayed positive.
        boundaries = ([history_start_at] if history_start_at is not None else []) + bad_times
        recovery_at = history_start_at
        active_source = None
        bad_index = 0
        for row, state, by_code in zip(rows, states, membership):
            while bad_index < len(bad_times) and bad_times[bad_index] <= row.observed_at:
                recovery_at = max(recovery_at or bad_times[bad_index], bad_times[bad_index])
                active_source = None
                bad_index += 1
            match = by_code.get(code)
            source_at = local_clock(_parse_datetime(match.get("source_quote_at"))) if match else None
            if (row.event_type != "universe_audit"
                    or state.get("read_model_version") != "c3_current_pool_v1"
                    or not match or not match.get("confirmation_frame")
                    or source_at is None or source_at.date() != row.trade_date
                    or source_at > row.observed_at
                    or not _first_board_confirmation_time(source_at)
                    or (row.observed_at - source_at).total_seconds() > settings.ANOMALY_QUOTE_MAX_AGE_SEC):
                boundaries.append(row.observed_at)
                recovery_at = row.observed_at
                active_source = None
            elif active_source is not None and source_at < active_source:
                # A regression of an active segment is a real reset even if
                # a later reverse counting walk stops before reaching it.
                boundaries.append(row.observed_at)
                recovery_at = row.observed_at
                active_source = None
            elif recovery_at is None or source_at > recovery_at:
                # Equal sources do not advance the high-water mark. In an
                # empty segment old sources must not move its recovery edge.
                active_source = source_at
        known_reset_at = max(boundaries, default=None)
        reset_at = known_reset_at
        newer_at = as_of
        seen_rounds: set[str] = set()
        if leaf.get("confirmation_frame"):
            for row, state, by_code in reversed(list(zip(rows, states, membership))):
                if row.event_type != "universe_audit":
                    reset_at = row.observed_at
                    break
                if (newer_at - row.observed_at).total_seconds() > max_age:
                    break
                if any(row.observed_at <= t <= newer_at for t in bad_times):
                    reset_at = max(t for t in bad_times if row.observed_at <= t <= newer_at)
                    break
                round_ids = set(state.get("quote_round_ids", []))
                if seen_rounds & round_ids:
                    break  # Reprocessing one quote batch is not a new price frame.
                seen_rounds.update(round_ids)
                match = by_code.get(code)
                if state.get("read_model_version") != "c3_current_pool_v1" or not match or not match.get("confirmation_frame"):
                    reset_at = row.observed_at
                    break
                source_at = local_clock(_parse_datetime(match.get("source_quote_at")))
                if (source_at is None or source_at.date() != row.trade_date
                        or source_at > row.observed_at
                        or not _first_board_confirmation_time(source_at)
                        or (row.observed_at - source_at).total_seconds() > settings.ANOMALY_QUOTE_MAX_AGE_SEC):
                    reset_at = row.observed_at
                    break
                if newer_source is not None:
                    if source_at == newer_source:
                        continue  # A receipt refresh is not another source frame.
                    if source_at > newer_source:
                        # The chronological pass owns regression boundaries:
                        # an old-source inversion in an empty segment is not
                        # a new failure and must not push recovery forward.
                        break
                    if (newer_source - source_at).total_seconds() > max_age:
                        break
                samples.append(row.observed_at)
                source_samples.append(source_at)
                newer_source = source_at
                newer_at = row.observed_at
        reset_at = max((at for at in (reset_at, known_reset_at) if at is not None), default=None)
        if reset_at is not None:
            retained = [(at, source) for at, source in zip(samples, source_samples) if source > reset_at]
            samples = [at for at, _ in retained]
            source_samples = [source for _, source in retained]
        streak = _confirmation_streak_status(samples, as_of) if samples else None
        source_streak = (
            _confirmation_streak_status(source_samples, max(source_samples))
            if source_samples else None
        )
        confirmed = bool(streak and streak["ready"] and source_streak and source_streak["ready"])
        members.append({
            # Explicit leaves also strip oversized members from older audits.
            "code": code, "name": leaf.get("name"),
            "eligible": bool(leaf.get("eligible")), "currently_confirmed": confirmed,
            "reason": "逐帧连续确认有效（仅影子观察）" if confirmed else leaf.get("reason"),
        })
    member_codes = {m["code"] for m in members}
    previous_codes = set(cumulative)
    if len(rows) > 1:
        previous_codes.update(membership[-2])
    return {
        "read_model_version": "c3_current_pool_v1",
        "history_complete": history_complete,
        "history_start_at": history_start_at.isoformat() if history_start_at else None,
        "route_version": route_version_for(ROUTE_C3),
        "trade_date": now.date().isoformat(), "as_of": as_of.isoformat() if as_of else None,
        "checked_at": now.isoformat(), "valid": valid, "expired": bool(as_of and (now - as_of).total_seconds() > max_age),
        "expires_at": (as_of + timedelta(seconds=max_age)).isoformat() if as_of else None,
        "reason": reason or ("当前结构池为空" if not members else "全市场逐帧更新；非昨日榜单入口"),
        "coverage": {k: prior.get(k) for k in ("allowed_count", "valid_quote_count", "quote_coverage", "context_audit")},
        "quote_round_ids": current.get("quote_round_ids", []),
        "cumulative_confirmed_today": len(cumulative),
        "structural_count": len(members),
        "eligible_count": sum(bool(m.get("eligible")) for m in members),
        "confirmed_count": sum(m["currently_confirmed"] for m in members),
        "members": members,
        "invalidated": [{"code": code, "reason": reason or "本帧已离开结构池（板块/行情/近期涨停资格变化）"}
                        for code in sorted(previous_codes - member_codes)],
        "execution_enabled": False, "promotion_metric": False,
        "note": "当日累计确认是不可变首次证据；当前池独立逐帧验证，不覆盖历史确认或结算。",
    }


async def build_first_board_current_pool(
    db: AsyncSession, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Load bounded audit history and today's immutable count; never writes."""
    from app.models.stock import QuoteRound

    now = now or datetime.now()
    # Keep a hard JSON row budget, but never mistake a truncated suffix for
    # complete history. One extra row detects overflow and forces unknown.
    limit = max(
        int(settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC)
        + int(settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES) + 3, 10,
    )
    scope = (
        PaperShadowEvent.route_id == ROUTE_C3,
        PaperShadowEvent.route_version == route_version_for(ROUTE_C3),
        PaperShadowEvent.trade_date == now.date(),
        PaperShadowEvent.observed_at <= now,
    )
    frame_types = PaperShadowEvent.event_type.in_(("universe_audit", "coverage_blocked"))
    latest_at = await db.scalar(select(func.max(PaperShadowEvent.observed_at)).where(
        *scope, frame_types,
    ))
    history_seconds = _c3_confirmation_history_seconds()
    history_start = (latest_at or now) - timedelta(seconds=history_seconds)
    frames = list((await db.scalars(
        select(PaperShadowEvent).where(
            *scope, frame_types,
            PaperShadowEvent.observed_at >= history_start,
        ).order_by(PaperShadowEvent.observed_at.desc(), PaperShadowEvent.id.desc()).limit(limit + 1)
    )).all())
    history_complete = len(frames) <= limit
    frames = frames[:limit]
    codes = list((await db.scalars(
        select(PaperShadowEvent.code).where(*scope, PaperShadowEvent.event_type == "confirmed")
    )).all())
    earliest = history_start
    rounds = list((await db.scalars(
        select(QuoteRound).where(
            QuoteRound.trade_date == now.date(), QuoteRound.committed_at >= earliest,
            QuoteRound.committed_at <= now,
        ).order_by(QuoteRound.committed_at)
    )).all())
    audited_rounds = {
        round_id for frame in frames
        for round_id in _json_dict(frame.snapshot_json).get("prior_structure", {})
        .get("current_pool", {}).get("quote_round_ids", [])
    }
    invalid_times = [
        r.committed_at for r in rounds
        if r.quality_status != "ok" or r.round_id not in audited_rounds
    ]
    result = project_first_board_current_pool(
        frames, now=now, cumulative_confirmed_codes=codes, invalid_quote_times=invalid_times,
        history_start_at=history_start, history_complete=history_complete,
    )
    result["latest_quote_round"] = ({
        "round_id": rounds[-1].round_id, "as_of": rounds[-1].as_of_at.isoformat(),
        "quality_status": rounds[-1].quality_status, "reason": rounds[-1].quality_reason,
        "coverage": rounds[-1].coverage,
    } if rounds else None)
    return result


async def finalize_first_board_shadow_sessions(
    db: AsyncSession,
    as_of_date: date | None = None,
    limit_sessions: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append C3 controls and same-day outcomes after the route window closes.

    ``control`` means an eligible candidate that never completed this route's
    persistent confirmation.  It is not selected by whether the stock later hit
    limit-up.  ``session_outcome`` is a separate post-close label used only for
    funnel recall/precision diagnostics and never for automatic promotion.
    """

    if not settings.PAPER_FIRST_BOARD_SHADOW_ENABLED:
        return {"sessions": 0, "controls_added": 0, "outcomes_added": 0}
    clock = now or datetime.now()
    as_of_date = as_of_date or clock.date()
    activation_date = _first_board_activation_date()
    if activation_date is None:
        return {
            "sessions": 0,
            "controls_added": 0,
            "outcomes_added": 0,
            "status": "activation_config_invalid",
        }
    route_version = route_version_for(ROUTE_C3)
    structural_rows = list(
        (
            await db.scalars(
                select(PaperShadowEvent)
                .where(
                    PaperShadowEvent.route_id == ROUTE_C3,
                    PaperShadowEvent.route_version == route_version,
                    PaperShadowEvent.event_type == "structural_pool",
                    PaperShadowEvent.trade_date <= as_of_date,
                )
                .order_by(PaperShadowEvent.trade_date, PaperShadowEvent.observed_at)
            )
        ).all()
    )
    by_date: dict[date, list[PaperShadowEvent]] = defaultdict(list)
    for row in structural_rows:
        if row.trade_date >= activation_date:
            by_date[row.trade_date].append(row)
    # A same-day control requires the full confirmation window, while the
    # outcome label additionally requires a completed close.  Never let a
    # manual/intraday call turn still-observable names into controls.
    pending_dates = [
        trade_day
        for trade_day in sorted(by_date)
        if trade_day < clock.date()
        or (trade_day == clock.date() and clock.time() >= time(20, 30))
    ][: max(int(limit_sessions), 1)]
    if not pending_dates:
        return {
            "sessions": 0,
            "controls_added": 0,
            "outcomes_added": 0,
            "status": "before_formal_close",
        }

    stage_rows = list(
        (
            await db.scalars(
                select(PaperShadowEvent).where(
                    PaperShadowEvent.route_id == ROUTE_C3,
                    PaperShadowEvent.route_version == route_version,
                    PaperShadowEvent.trade_date.in_(pending_dates),
                    PaperShadowEvent.event_type.in_((
                        "eligible",
                        "confirmed",
                        "control",
                        "session_outcome",
                        "universe_audit",
                    )),
                )
            )
        ).all()
    )
    eligible_by_key: dict[tuple[date, str], PaperShadowEvent] = {}
    confirmed_keys: set[tuple[date, str]] = set()
    existing_controls: set[tuple[date, str]] = set()
    existing_outcomes: set[tuple[date, str]] = set()
    universe_frames_by_date: dict[date, list[datetime]] = defaultdict(list)
    for row in stage_rows:
        key = (row.trade_date, str(row.code))
        if row.event_type == "eligible":
            current = eligible_by_key.get(key)
            if current is None or row.observed_at < current.observed_at:
                eligible_by_key[key] = row
        elif row.event_type == "confirmed":
            confirmed_keys.add(key)
        elif row.event_type == "control":
            existing_controls.add(key)
        elif row.event_type == "session_outcome":
            existing_outcomes.add(key)
        elif row.event_type == "universe_audit":
            universe_frames_by_date[row.trade_date].append(row.observed_at)

    events: list[dict[str, Any]] = []
    for trade_day in pending_dates:
        rows = by_date[trade_day]
        structural_by_code = {str(row.code): row for row in rows}
        codes = set(structural_by_code)
        observation_health = _first_board_session_observation_health(
            universe_frames_by_date.get(trade_day, []),
            trade_day,
        )
        if not observation_health["complete"]:
            events.append(
                _event(
                    route_id=ROUTE_C3,
                    trade_date=trade_day,
                    observed_at=datetime.combine(trade_day, time(20, 34)),
                    code="MARKET",
                    name="首板路线全时段观测审计",
                    event_type="session_blocked",
                    status="incomplete_observation",
                    quote=None,
                    prior={
                        "structural_count": len(codes),
                        "session_observation_health": observation_health,
                        "reason": "盘中健康帧未覆盖完整路线时段，禁止把未确认误当作有效对照",
                    },
                )
            )
            continue
        events.append(
            _event(
                route_id=ROUTE_C3,
                trade_date=trade_day,
                observed_at=datetime.combine(trade_day, time(20, 34)),
                code="MARKET",
                name="首板路线全时段观测审计",
                event_type="session_ready",
                status="complete_observation",
                quote=None,
                prior={
                    "structural_count": len(codes),
                    "session_observation_health": observation_health,
                    "eligible_for_settlement": True,
                },
            )
        )

        # The control cohort is defined only by information available during the
        # route window plus the fact that its own confirmation never completed.
        for code in sorted(codes):
            key = (trade_day, code)
            eligible = eligible_by_key.get(key)
            if (
                eligible is None
                or key in confirmed_keys
                or key in existing_controls
            ):
                continue
            eligible_snapshot = _json_dict(eligible.snapshot_json)
            quote = (
                eligible_snapshot.get("quote")
                if isinstance(eligible_snapshot.get("quote"), dict)
                else {}
            )
            prior = (
                eligible_snapshot.get("prior_structure")
                if isinstance(eligible_snapshot.get("prior_structure"), dict)
                else {}
            )
            control = _event(
                route_id=ROUTE_C3,
                trade_date=trade_day,
                observed_at=eligible.observed_at,
                code=code,
                name=str(eligible.name or code),
                event_type="control",
                status="eligible_unconfirmed",
                quote=quote,
                prior={
                    **prior,
                    "control_cohort": "eligible_without_persistent_confirmation",
                    "eligible_anchor_event_key": eligible.event_key,
                    "classified_after_route_cutoff_at": clock.isoformat(),
                    "selected_without_market_outcome": True,
                },
            )
            control["assumed_fill_price"] = _assumed_fill_price(quote)
            if control["assumed_fill_price"] is not None:
                events.append(control)

        close_bar_candidates = list(
            (
                await db.scalars(
                    select(StockKline).where(
                        StockKline.trade_date == trade_day,
                        StockKline.code.in_(codes),
                        StockKline.source.in_(tuple(_FORMAL_CLOSE_SOURCES)),
                    )
                )
            ).all()
        )
        tencent_close_codes = {
            str(row.code)
            for row in close_bar_candidates
            if str(row.source or "") == "tencent_close"
        }
        fresh_tencent_close_codes: set[str] = set()
        if tencent_close_codes:
            close_cutoff = datetime.combine(trade_day, time(15, 0))
            next_day = datetime.combine(trade_day + timedelta(days=1), time.min)
            closing_spots = list(
                (
                    await db.scalars(
                        select(StockSpot).where(StockSpot.code.in_(tencent_close_codes))
                    )
                ).all()
            )
            fresh_tencent_close_codes = {
                str(row.code)
                for row in closing_spots
                if isinstance(row.updated_at, datetime)
                and close_cutoff <= row.updated_at < next_day
            }
        formal_bars = [
            row
            for row in close_bar_candidates
            if str(row.source or "") == "ths"
            or str(row.code) in fresh_tencent_close_codes
        ]
        bar_by_code = {str(row.code): row for row in formal_bars}
        kline_coverage = len(bar_by_code) / max(len(codes), 1)
        if kline_coverage < settings.PAPER_FIRST_BOARD_SHADOW_OUTCOME_MIN_KLINE_COVERAGE:
            events.append(
                _event(
                    route_id=ROUTE_C3,
                    trade_date=trade_day,
                    observed_at=datetime.combine(trade_day, time(20, 35)),
                    code="MARKET",
                    name="首板路线收盘结果审计",
                    event_type="outcome_blocked",
                    status="outcome_blocked",
                    quote=None,
                    prior={
                        "structural_count": len(codes),
                        "formal_kline_count": len(bar_by_code),
                        "formal_kline_coverage": round(kline_coverage, 6),
                        "accepted_formal_sources": sorted(_FORMAL_CLOSE_SOURCES),
                        "stored_tencent_close_count": len(tencent_close_codes),
                        "fresh_tencent_close_count": len(fresh_tencent_close_codes),
                        "minimum_kline_coverage": (
                            settings.PAPER_FIRST_BOARD_SHADOW_OUTCOME_MIN_KLINE_COVERAGE
                        ),
                        "reason": "正式收盘日K覆盖不足，禁止把缺失候选标成未涨停",
                    },
                )
            )
            continue

        broken_codes = set(
            (
                await db.scalars(
                    select(BrokenLimitPool.code).where(
                        BrokenLimitPool.trade_date == trade_day,
                        BrokenLimitPool.code.in_(codes),
                    )
                )
            ).all()
        )
        for code, structural in structural_by_code.items():
            key = (trade_day, code)
            if key in existing_outcomes:
                continue
            bar = bar_by_code.get(code)
            if bar is None:
                continue
            close_change = _safe_float(bar.change_pct)
            if close_change is None:
                close_change = _pct(bar.close, bar.prev_close)
            high_change = _pct(bar.high, bar.prev_close)
            adjusted_bar = str(bar.source or "") == "ths"
            closed_first_board = bool(
                close_change is not None
                and is_limit_up_change(
                    code,
                    close_change,
                    name=structural.name,
                    trade_date=trade_day,
                    adjusted_bar=adjusted_bar,
                )
            )
            touched_limit = bool(
                closed_first_board
                or code in broken_codes
                or (
                    high_change is not None
                    and is_limit_up_change(
                        code,
                        high_change,
                        name=structural.name,
                        trade_date=trade_day,
                        adjusted_bar=adjusted_bar,
                    )
                )
            )
            confirmed = key in confirmed_keys
            status = (
                "confirmed_first_board"
                if confirmed and closed_first_board
                else "confirmed_not_board"
                if confirmed
                else "unconfirmed_first_board"
                if closed_first_board
                else "unconfirmed_not_board"
            )
            outcome = _event(
                route_id=ROUTE_C3,
                trade_date=trade_day,
                observed_at=datetime.combine(trade_day, time(20, 35)),
                code=code,
                name=str(structural.name or code),
                event_type="session_outcome",
                status=status,
                quote=None,
                prior={
                    "structural_event_key": structural.event_key,
                    "eligible": key in eligible_by_key,
                    "confirmed": confirmed,
                    "closed_first_board": closed_first_board,
                    "touched_limit": touched_limit,
                    "formal_kline_source": str(bar.source or ""),
                    "close_change_pct": (
                        round(close_change, 4) if close_change is not None else None
                    ),
                    "high_change_pct": (
                        round(high_change, 4) if high_change is not None else None
                    ),
                    "post_close_label_only": True,
                    "eligible_for_automatic_promotion": False,
                },
            )
            outcome["price"] = _safe_float(bar.close)
            outcome["change_pct"] = close_change
            events.append(outcome)

    added = await _persist_events(db, events)
    return {
        "sessions": len(pending_dates),
        "controls_added": sum(row["event_type"] == "control" for row in added),
        "outcomes_added": sum(
            row["event_type"] == "session_outcome" for row in added
        ),
        "sessions_ready_added": sum(
            row["event_type"] == "session_ready" for row in added
        ),
        "quality_blocks_added": sum(
            row["event_type"] in {"outcome_blocked", "session_blocked"}
            for row in added
        ),
        "route_id": ROUTE_C3,
        "route_version": route_version,
    }


async def settle_strategy_iteration_shadow(
    db: AsyncSession,
    as_of_date: date | None = None,
    limit: int = 1000,
) -> dict[str, int]:
    """Append right-censored returns for confirmations and C3 controls."""

    as_of_date = as_of_date or date.today()
    # Old immutable C3 signals may finish their horizons, but readiness must
    # belong to that exact version/day, never borrowed from the new route.
    c3_ready = PaperShadowEvent.__table__.alias("c3_session_ready")
    c3_same_version_ready = select(c3_ready.c.id).where(
        c3_ready.c.route_id == ROUTE_C3,
        c3_ready.c.route_version == PaperShadowEvent.route_version,
        c3_ready.c.trade_date == PaperShadowEvent.trade_date,
        c3_ready.c.event_type == "session_ready",
    ).exists()
    signals = list(
        (
            await db.scalars(
                select(PaperShadowEvent)
                .where(
                    PaperShadowEvent.route_id.in_(ROUTE_IDS),
                    or_(
                        PaperShadowEvent.event_type == "confirmed",
                        (
                            (PaperShadowEvent.route_id == ROUTE_C3)
                            & (PaperShadowEvent.event_type == "control")
                        ),
                    ),
                    or_(
                        PaperShadowEvent.route_id != ROUTE_C3,
                        c3_same_version_ready,
                    ),
                    PaperShadowEvent.trade_date < as_of_date,
                )
                .order_by(PaperShadowEvent.trade_date, PaperShadowEvent.observed_at)
                .limit(max(int(limit), 1))
            )
        ).all()
    )
    if not signals:
        return {"signals": 0, "evaluations_added": 0}

    signal_keys = [item.event_key for item in signals]
    existing = set(
        (
            await db.execute(
                select(
                    PaperShadowEvaluation.signal_event_key,
                    PaperShadowEvaluation.horizon_days,
                ).where(PaperShadowEvaluation.signal_event_key.in_(signal_keys))
            )
        ).all()
    )
    benchmark_cache: dict[date, float] = {}

    async def market_return(trade_day: date) -> float:
        if trade_day not in benchmark_cache:
            value = await db.scalar(
                select(func.avg(StockKline.change_pct)).where(
                    StockKline.trade_date == trade_day,
                    StockKline.source.in_(tuple(_FORMAL_CLOSE_SOURCES)),
                    StockKline.change_pct.is_not(None),
                    StockKline.change_pct >= -31.0,
                    StockKline.change_pct <= 31.0,
                )
            )
            benchmark_cache[trade_day] = _safe_float(value, 0.0) or 0.0
        return benchmark_cache[trade_day]

    buy_commission = max(_safe_float(settings.PAPER_COMMISSION_RATE, 0.0) or 0.0, 0.0)
    sell_commission = buy_commission
    stamp_tax = max(_safe_float(settings.PAPER_STAMP_TAX_RATE, 0.0) or 0.0, 0.0)
    sell_slippage = (
        max(_safe_float(settings.PAPER_EXECUTION_SLIPPAGE_PCT, 0.0) or 0.0, 0.0)
        / 100.0
    )
    async def formal_bar_usable(bar: StockKline | None) -> bool:
        """Accept persisted formal close bars during later-horizon settlement.

        ``tencent_close`` rows are written only by the post-close finalizer.  The
        live ``stock_spot.updated_at`` clock belongs to *today* and must not be
        used to invalidate an older immutable close row after the next session
        refreshes the spot table.  Same-day C3 outcome finalization retains its
        stricter fresh-spot guard above.
        """

        return bool(bar is not None and str(bar.source or "") in _FORMAL_CLOSE_SOURCES)

    evaluations: list[dict[str, Any]] = []
    for signal in signals:
        signal_price = _safe_float(signal.assumed_fill_price)
        if signal_price is None or signal_price <= 0:
            continue
        signal_bar = await db.scalar(
            select(StockKline).where(
                StockKline.code == signal.code,
                StockKline.trade_date == signal.trade_date,
            )
        )
        signal_day_close = _safe_float(
            getattr(signal_bar, "close", None) if signal_bar is not None else None
        )
        if (
            signal_day_close is None
            or signal_day_close <= 0
            or not await formal_bar_usable(signal_bar)
        ):
            # 无信号日日K就无法识别下一交易日是否发生除权/价格断点。
            continue
        bars = list(
            (
                await db.scalars(
                    select(StockKline)
                    .where(
                        StockKline.code == signal.code,
                        StockKline.trade_date > signal.trade_date,
                        StockKline.trade_date <= as_of_date,
                    )
                    .order_by(StockKline.trade_date)
                    .limit(5)
                )
            ).all()
        )
        for horizon in (1, 3, 5):
            if len(bars) < horizon or (signal.event_key, horizon) in existing:
                continue
            observed = bars[:horizon]
            prior_close = signal_day_close
            price_chain_valid = True
            for bar in observed:
                if not await formal_bar_usable(bar):
                    price_chain_valid = False
                    break
                reference_close = _safe_float(bar.prev_close)
                current_close = _safe_float(bar.close)
                if (
                    reference_close is None
                    or reference_close <= 0
                    or current_close is None
                    or current_close <= 0
                    or abs(reference_close - prior_close)
                    > _PRICE_CHAIN_MAX_ABS_GAP
                ):
                    price_chain_valid = False
                    break
                prior_close = current_close
            if not price_chain_valid:
                # 除权、复权切换或断裂K线使绝对价格不可比；该 horizon 右删失。
                continue
            exit_close = _safe_float(observed[-1].close)
            if exit_close is None or exit_close <= 0:
                continue
            exit_fill = exit_close * (1.0 - sell_slippage)
            gross_return = (exit_close / signal_price - 1.0) * 100.0
            buy_cash = signal_price * (1.0 + buy_commission)
            sell_cash = exit_fill * (1.0 - sell_commission - stamp_tax)
            net_return = (sell_cash / buy_cash - 1.0) * 100.0
            benchmark_nav = 1.0
            for bar in observed:
                benchmark_nav *= 1.0 + await market_return(bar.trade_date) / 100.0
            benchmark_return = (benchmark_nav - 1.0) * 100.0
            highs = [
                value
                for bar in observed
                if (value := _safe_float(bar.high)) is not None and value > 0
            ]
            lows = [
                value
                for bar in observed
                if (value := _safe_float(bar.low)) is not None and value > 0
            ]
            evaluations.append(
                {
                    "signal_event_key": signal.event_key,
                    "route_id": signal.route_id,
                    "route_version": signal.route_version,
                    "code": signal.code,
                    "signal_trade_date": signal.trade_date,
                    "signal_time": signal.observed_at,
                    "horizon_days": horizon,
                    "exit_trade_date": observed[-1].trade_date,
                    "signal_price": signal_price,
                    "exit_price": round(exit_fill, 4),
                    "gross_return_pct": round(gross_return, 4),
                    "net_return_pct": round(net_return, 4),
                    "benchmark_return_pct": round(benchmark_return, 4),
                    "excess_return_pct": round(net_return - benchmark_return, 4),
                    "max_favorable_pct": (
                        round((max(highs) / signal_price - 1.0) * 100.0, 4)
                        if highs
                        else None
                    ),
                    "max_adverse_pct": (
                        round((min(lows) / signal_price - 1.0) * 100.0, 4)
                        if lows
                        else None
                    ),
                    "is_positive": bool(
                        net_return > 0 and net_return - benchmark_return > 0
                    ),
                    "details_json": json.dumps(
                        {
                            "as_of_date": as_of_date.isoformat(),
                            "cohort": (
                                "eligible_unconfirmed_control"
                                if signal.event_type == "control"
                                else "confirmed"
                            ),
                            "source_event_type": signal.event_type,
                            "entry_basis": (
                                "first_eligible_time_ask1_plus_buy_slippage"
                                if signal.event_type == "control"
                                else "confirmation_time_ask1_plus_buy_slippage"
                            ),
                            "exit_basis": "horizon_close_minus_sell_slippage",
                            "right_censored_if_horizon_unavailable": True,
                            "right_censored_on_price_chain_discontinuity": True,
                            "price_chain_max_abs_gap": _PRICE_CHAIN_MAX_ABS_GAP,
                            "signal_day_post_entry_path_available": False,
                            "execution_enabled": False,
                        },
                        ensure_ascii=False,
                    ),
                    "created_at": datetime.now(),
                }
            )

    for offset in range(0, len(evaluations), _EVENT_BATCH_SIZE):
        batch = evaluations[offset:offset + _EVENT_BATCH_SIZE]
        statement = sqlite_insert(PaperShadowEvaluation).values(batch)
        statement = statement.on_conflict_do_nothing(
            index_elements=["signal_event_key", "horizon_days"]
        )
        await db.execute(statement)
    if evaluations:
        await db.commit()
    return {"signals": len(signals), "evaluations_added": len(evaluations)}


def build_strategy_iteration_evidence_summary(
    evaluations: Iterable[PaperShadowEvaluation],
    *,
    route_id: str,
    route_version: str | None = None,
    horizon_days: int = 3,
) -> dict[str, Any]:
    """Evaluate one challenger route without ever enabling execution automatically."""

    all_rows = sorted(
        (
            item
            for item in evaluations
            if item.route_id == route_id
            and int(item.horizon_days) == horizon_days
            and (route_version is None or item.route_version == route_version)
        ),
        key=lambda item: (item.signal_trade_date, item.signal_time, item.signal_event_key),
    )

    def cohort(item: PaperShadowEvaluation) -> str:
        value = str(_json_dict(item.details_json).get("cohort") or "confirmed")
        return value

    rows = [item for item in all_rows if cohort(item) != "eligible_unconfirmed_control"]
    control_rows = [
        item for item in all_rows if cohort(item) == "eligible_unconfirmed_control"
    ]
    net_returns = [
        value
        for item in rows
        if (value := _safe_float(item.net_return_pct)) is not None
    ]
    excess_returns = [
        value
        for item in rows
        if (value := _safe_float(item.excess_return_pct)) is not None
    ]
    adverse_returns = [
        value
        for item in rows
        if (value := _safe_float(item.max_adverse_pct)) is not None
    ]
    control_net_returns = [
        value
        for item in control_rows
        if (value := _safe_float(item.net_return_pct)) is not None
    ]
    control_excess_returns = [
        value
        for item in control_rows
        if (value := _safe_float(item.excess_return_pct)) is not None
    ]
    control_session_counts = Counter(item.signal_trade_date for item in control_rows)
    session_counts = Counter(item.signal_trade_date for item in rows)
    code_counts = Counter(str(item.code) for item in rows)
    sample_count = len(rows)

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    split = sample_count // 2
    first_half = net_returns[:split]
    second_half = net_returns[split:]
    remove_count = min(5, max(len(net_returns) - 1, 0))
    without_top = (
        sorted(net_returns)[:-remove_count]
        if remove_count
        else list(net_returns)
    )
    avg_net = mean(net_returns)
    avg_excess = mean(excess_returns)
    control_avg_net = mean(control_net_returns)
    control_avg_excess = mean(control_excess_returns)
    confirmed_minus_control = (
        avg_net - control_avg_net
        if avg_net is not None and control_avg_net is not None
        else None
    )
    first_half_avg = mean(first_half)
    second_half_avg = mean(second_half)
    without_top_avg = mean(without_top)
    avg_mae = mean(adverse_returns)
    worst_mae = min(adverse_returns, default=None)
    max_session_share = (
        max(session_counts.values(), default=0) / sample_count
        if sample_count
        else None
    )
    max_code_share = (
        max(code_counts.values(), default=0) / sample_count
        if sample_count
        else None
    )
    min_sessions = max(int(settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_SESSIONS), 1)
    min_samples = max(int(settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_SAMPLES), 1)
    gates = {
        "enough_independent_sessions": len(session_counts) >= min_sessions,
        "enough_confirmed_samples": sample_count >= min_samples,
        "avg_net_return_positive": avg_net is not None and avg_net > 0,
        "avg_excess_return_positive": avg_excess is not None and avg_excess > 0,
        "first_half_positive": first_half_avg is not None and first_half_avg > 0,
        "second_half_positive": second_half_avg is not None and second_half_avg > 0,
        "top5_removed_still_positive": (
            without_top_avg is not None and without_top_avg > 0
        ),
        "avg_mae_acceptable": (
            avg_mae is not None
            and avg_mae >= settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_AVG_MAE_PCT
        ),
        "worst_mae_acceptable": (
            worst_mae is not None
            and worst_mae >= settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_WORST_MAE_PCT
        ),
        "session_concentration_acceptable": (
            max_session_share is not None
            and max_session_share
            <= settings.PAPER_STRATEGY_ITERATION_EVAL_MAX_SESSION_SHARE
        ),
        "code_concentration_acceptable": (
            max_code_share is not None
            and max_code_share <= settings.PAPER_STRATEGY_ITERATION_EVAL_MAX_CODE_SHARE
        ),
    }
    if route_id == ROUTE_C3:
        gates["beats_unconfirmed_control"] = bool(
            confirmed_minus_control is not None and confirmed_minus_control > 0
        )
    return {
        "route_id": route_id,
        "route_version": route_version,
        "horizon_days": horizon_days,
        "sample_count": sample_count,
        "independent_sessions": len(session_counts),
        "control_sample_count": len(control_rows),
        "control_independent_sessions": len(control_session_counts),
        "minimum_required_samples": min_samples,
        "minimum_required_sessions": min_sessions,
        "thresholds": {
            "minimum_avg_mae_pct": (
                settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_AVG_MAE_PCT
            ),
            "minimum_worst_mae_pct": (
                settings.PAPER_STRATEGY_ITERATION_EVAL_MIN_WORST_MAE_PCT
            ),
            "maximum_session_share": (
                settings.PAPER_STRATEGY_ITERATION_EVAL_MAX_SESSION_SHARE
            ),
            "maximum_code_share": (
                settings.PAPER_STRATEGY_ITERATION_EVAL_MAX_CODE_SHARE
            ),
        },
        "return_basis": "确认时卖一价加买入滑点，持有期收盘价扣卖出滑点、佣金与印花税",
        "benchmark_label": "同期全市场个股等权平均涨跌幅",
        "control_basis": (
            "同一结构与资格分母中，未完成持续确认的候选；按其首次资格时卖一价计入，"
            "不按是否最终涨停筛选"
            if route_id == ROUTE_C3
            else None
        ),
        "control_avg_net_return_pct": (
            round(control_avg_net, 4) if control_avg_net is not None else None
        ),
        "control_avg_excess_return_pct": (
            round(control_avg_excess, 4)
            if control_avg_excess is not None
            else None
        ),
        "confirmed_minus_control_pct": (
            round(confirmed_minus_control, 4)
            if confirmed_minus_control is not None
            else None
        ),
        "avg_net_return_pct": round(avg_net, 4) if avg_net is not None else None,
        "avg_excess_return_pct": (
            round(avg_excess, 4) if avg_excess is not None else None
        ),
        "first_half_avg_net_return_pct": (
            round(first_half_avg, 4) if first_half_avg is not None else None
        ),
        "second_half_avg_net_return_pct": (
            round(second_half_avg, 4) if second_half_avg is not None else None
        ),
        "top5_removed_avg_net_return_pct": (
            round(without_top_avg, 4) if without_top_avg is not None else None
        ),
        "avg_max_adverse_pct": round(avg_mae, 4) if avg_mae is not None else None,
        "worst_max_adverse_pct": (
            round(worst_mae, 4) if worst_mae is not None else None
        ),
        "max_session_share": (
            round(max_session_share, 4) if max_session_share is not None else None
        ),
        "max_code_share": (
            round(max_code_share, 4) if max_code_share is not None else None
        ),
        "gates": gates,
        "evidence_gate_passed": all(gates.values()),
        "execution_enabled": False,
        "promotion_requires_manual_review": True,
    }
