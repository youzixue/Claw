"""Effective per-account paper policy and version snapshots.

The twelve paper accounts share market data and conservative execution constraints,
but never inherit another account's capital, position or strategy thresholds.
"""

from __future__ import annotations

from typing import Any

from app.config.settings import settings, PaperConfirmationPolicy, PaperRouteSignalPolicy, PaperChallengerExecutionPolicy

# Immutable default mechanics currently shared by short-account exits.  Keeping
# them in the effective account snapshot prevents later PAPER_AUTO_* changes
# from silently altering another account.  A future intentional change should
# update this profile (and therefore rotate affected account versions).
_DEFAULT_SHORT_SELL_PROFILE = {
    "pullback_from_high_pct": 2.5,
    "breakeven_protect_high_profit_pct": 3.0,
    "breakeven_protect_low_pct": -0.2,
    "breakeven_protect_high_pct": 0.6,
    "volume_negative_ratio": 1.2,
    "open_noise_end": "09:45",
    "trade_t_enabled": True,
    "t_sell_pct": 0.50,
    "t_weak_sell_pct": 0.33,
    "short_full_exit_profit_max_amount": 300,
}

ACCOUNT_NAMES = (
    "default", "promotion", "mainline", "auction", "tenbagger", "reversal",
    "challenger_a", "challenger_b", "challenger_c", "challenger_d",
    "challenger_e", "challenger_f2",
)

# Only constraints genuinely shared by every paper execution path.  Signal,
# ranking and account-specific entry values intentionally stay out of this set.
_COMMON_EXECUTION_KEYS = (
    "PAPER_COMMISSION_RATE",
    "PAPER_MIN_COMMISSION",
    "PAPER_STAMP_TAX_RATE",
    "PAPER_EXECUTION_QUOTE_MAX_AGE_SEC",
    "PAPER_EXECUTION_SLIPPAGE_PCT",
    "PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND",
    "PAPER_DEPTH_MAX_PARTICIPATION_RATIO",
    "PAPER_AUTO_HARD_STOP_MAX_LOSS_PCT",
    "QUOTE_ROUND_MIN_COVERAGE",
    "QUOTE_ROUND_MIN_SOURCE_TIME_COVERAGE",
    "QUOTE_ROUND_MAX_SOURCE_SKEW_SEC",
)

# Accounts with the champion-style confirmation algorithm, but independent parameters.
_CONFIRMATION_ACCOUNTS = frozenset({
    "default", "promotion", "mainline", "auction", "tenbagger", "reversal",
    "challenger_e",
})

_ACCOUNT_PREFIXES = {
    "default": ("PAPER_AUTO_", "PAPER_MARKET_"),
    "promotion": ("PAPER_PROMOTION_",),
    "mainline": ("PAPER_MAINLINE_",),
    "auction": ("PAPER_AUCTION_",),
    "tenbagger": ("PAPER_TENBAGGER_", "PAPER_HIGHBOARD_"),
    "reversal": ("PAPER_REVERSAL_",),
    "challenger_a": ("PAPER_CHALLENGER_A_", "PAPER_MOMENTUM_RETEST_"),
    "challenger_b": ("PAPER_CHALLENGER_B_", "PAPER_STRATEGY_B_"),
    "challenger_c": ("PAPER_CHALLENGER_C_", "PAPER_STRATEGY_C_"),
    "challenger_d": ("PAPER_CHALLENGER_D_", "PAPER_STRATEGY_D_"),
    "challenger_e": ("PAPER_CHALLENGER_E_",),
    "challenger_f2": ("PAPER_CHALLENGER_F2_", "PAPER_STRATEGY_F2_"),
}

# These explicit keys document the normalized sizing/exit contract. Prefix
# capture below additionally includes every entry dependency in that namespace.
_ACCOUNT_KEYS = {
    "default": (
        "PAPER_AUTO_MAX_DAILY_NEW_BUYS", "PAPER_AUTO_MAX_POSITIONS",
        "PAPER_AUTO_POSITION_PCT", "PAPER_AUTO_TAKE_PROFIT_PCT",
        "PAPER_AUTO_STOP_LOSS_PCT", "PAPER_AUTO_SMALL_STOP_LOSS_PCT",
        "PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT", "PAPER_AUTO_MAX_HOLD_DAYS",
    ),
    "promotion": (
        "PAPER_PROMOTION_MAX_DAILY_BUYS", "PAPER_PROMOTION_MAX_POSITIONS",
        "PAPER_PROMOTION_POSITION_PCT", "PAPER_PROMOTION_TAKE_PROFIT_PCT",
        "PAPER_PROMOTION_STOP_LOSS_PCT", "PAPER_PROMOTION_MAX_HOLD_DAYS",
    ),
    "mainline": (
        "PAPER_MAINLINE_MAX_DAILY_BUYS", "PAPER_MAINLINE_MAX_POSITIONS",
        "PAPER_MAINLINE_POSITION_PCT", "PAPER_MAINLINE_TAKE_PROFIT_PCT",
        "PAPER_MAINLINE_STOP_LOSS_PCT", "PAPER_MAINLINE_MAX_HOLD_DAYS",
    ),
    "auction": (
        "PAPER_AUCTION_MAX_DAILY_BUYS", "PAPER_AUCTION_MAX_POSITIONS",
        "PAPER_AUCTION_POSITION_PCT", "PAPER_AUCTION_TAKE_PROFIT_PCT",
        "PAPER_AUCTION_STOP_LOSS_PCT", "PAPER_AUCTION_MAX_HOLD_DAYS",
    ),
    "tenbagger": (
        "PAPER_TENBAGGER_MAX_DAILY_BUYS", "PAPER_TENBAGGER_MAX_POSITIONS",
        "PAPER_TENBAGGER_POSITION_PCT", "PAPER_HIGHBOARD_TAKE_PROFIT_PCT",
        "PAPER_HIGHBOARD_STOP_LOSS_PCT", "PAPER_HIGHBOARD_MAX_HOLD_DAYS",
        "PAPER_HIGHBOARD_MIN_CONSECUTIVE", "PAPER_HIGHBOARD_MAX_CONSECUTIVE",
        "PAPER_HIGHBOARD_MIN_SEAL_AMOUNT", "PAPER_HIGHBOARD_MAX_BREAK_COUNT",
    ),
    "reversal": (
        "PAPER_REVERSAL_MAX_DAILY_BUYS", "PAPER_REVERSAL_MAX_POSITIONS",
        "PAPER_REVERSAL_POSITION_PCT", "PAPER_REVERSAL_TAKE_PROFIT_PCT",
        "PAPER_REVERSAL_STOP_LOSS_PCT", "PAPER_REVERSAL_MAX_HOLD_DAYS",
    ),
    "challenger_a": (
        "PAPER_CHALLENGER_A_MAX_DAILY_BUYS", "PAPER_CHALLENGER_A_MAX_POSITIONS",
        "PAPER_CHALLENGER_A_POSITION_PCT", "PAPER_CHALLENGER_A_TAKE_PROFIT_PCT",
        "PAPER_CHALLENGER_A_STOP_LOSS_PCT", "PAPER_CHALLENGER_A_MAX_HOLD_DAYS",
        "PAPER_CHALLENGER_A_ENTRY_NOT_BEFORE",
    ),
    "challenger_b": (
        "PAPER_CHALLENGER_B_MAX_DAILY_BUYS", "PAPER_CHALLENGER_B_MAX_POSITIONS",
        "PAPER_CHALLENGER_B_POSITION_PCT", "PAPER_CHALLENGER_B_TAKE_PROFIT_PCT",
        "PAPER_CHALLENGER_B_STOP_LOSS_PCT", "PAPER_CHALLENGER_B_MAX_HOLD_DAYS",
        "PAPER_CHALLENGER_B_ENTRY_NOT_BEFORE",
    ),
    "challenger_c": (
        "PAPER_CHALLENGER_C_MAX_DAILY_BUYS", "PAPER_CHALLENGER_C_MAX_POSITIONS",
        "PAPER_CHALLENGER_C_POSITION_PCT", "PAPER_CHALLENGER_C_TAKE_PROFIT_PCT",
        "PAPER_CHALLENGER_C_STOP_LOSS_PCT", "PAPER_CHALLENGER_C_MAX_HOLD_DAYS",
        "PAPER_CHALLENGER_C_ENTRY_NOT_BEFORE",
    ),
    "challenger_d": (
        "PAPER_CHALLENGER_D_MAX_DAILY_BUYS", "PAPER_CHALLENGER_D_MAX_POSITIONS",
        "PAPER_CHALLENGER_D_POSITION_PCT", "PAPER_CHALLENGER_D_TAKE_PROFIT_PCT",
        "PAPER_CHALLENGER_D_STOP_LOSS_PCT", "PAPER_CHALLENGER_D_MAX_HOLD_DAYS",
        "PAPER_CHALLENGER_D_ENTRY_NOT_BEFORE",
    ),
    "challenger_e": (
        "PAPER_CHALLENGER_E_MAX_DAILY_BUYS", "PAPER_CHALLENGER_E_MAX_POSITIONS",
        "PAPER_CHALLENGER_E_POSITION_PCT", "PAPER_CHALLENGER_E_TAKE_PROFIT_PCT",
        "PAPER_CHALLENGER_E_STOP_LOSS_PCT", "PAPER_CHALLENGER_E_MAX_HOLD_DAYS",
        "PAPER_CHALLENGER_E_MIN_CONSECUTIVE", "PAPER_CHALLENGER_E_MAX_CONSECUTIVE",
        "PAPER_CHALLENGER_E_MIN_SEAL_AMOUNT", "PAPER_CHALLENGER_E_MAX_BREAK_COUNT",
        "PAPER_CHALLENGER_E_MAX_ENTRY_CHANGE_PCT",
    ),
    "challenger_f2": (
        "PAPER_CHALLENGER_F2_MAX_DAILY_BUYS", "PAPER_CHALLENGER_F2_MAX_POSITIONS",
        "PAPER_CHALLENGER_F2_POSITION_PCT", "PAPER_CHALLENGER_F2_TAKE_PROFIT_PCT",
        "PAPER_CHALLENGER_F2_STOP_LOSS_PCT", "PAPER_CHALLENGER_F2_MAX_HOLD_DAYS",
        "PAPER_CHALLENGER_F2_ENTRY_NOT_BEFORE",
    ),
}


def _values(keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: getattr(settings, key) for key in keys}


def account_sell_params(account_name: str) -> dict[str, Any]:
    """Return the complete lowercase exit profile consumed by paper.py."""
    if account_name not in _ACCOUNT_KEYS:
        raise KeyError(f"unknown paper account: {account_name}")
    challenger_prefix = {
        "challenger_a": "PAPER_CHALLENGER_A",
        "challenger_b": "PAPER_CHALLENGER_B",
        "challenger_c": "PAPER_CHALLENGER_C",
        "challenger_d": "PAPER_CHALLENGER_D",
        "challenger_e": "PAPER_CHALLENGER_E",
        "challenger_f2": "PAPER_CHALLENGER_F2",
    }.get(account_name)
    if challenger_prefix:
        take_profit = getattr(settings, f"{challenger_prefix}_TAKE_PROFIT_PCT")
        stop_loss = getattr(settings, f"{challenger_prefix}_STOP_LOSS_PCT")
        max_hold = getattr(settings, f"{challenger_prefix}_MAX_HOLD_DAYS")
    else:
        take_profit, stop_loss, max_hold = {
            "default": (
                settings.PAPER_AUTO_TAKE_PROFIT_PCT,
                settings.PAPER_AUTO_STOP_LOSS_PCT,
                settings.PAPER_AUTO_MAX_HOLD_DAYS,
            ),
            "promotion": (
                settings.PAPER_PROMOTION_TAKE_PROFIT_PCT,
                settings.PAPER_PROMOTION_STOP_LOSS_PCT,
                settings.PAPER_PROMOTION_MAX_HOLD_DAYS,
            ),
            "mainline": (
                settings.PAPER_MAINLINE_TAKE_PROFIT_PCT,
                settings.PAPER_MAINLINE_STOP_LOSS_PCT,
                settings.PAPER_MAINLINE_MAX_HOLD_DAYS,
            ),
            "auction": (
                settings.PAPER_AUCTION_TAKE_PROFIT_PCT,
                settings.PAPER_AUCTION_STOP_LOSS_PCT,
                settings.PAPER_AUCTION_MAX_HOLD_DAYS,
            ),
            "tenbagger": (
                settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT,
                settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
                settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS,
            ),
            "reversal": (
                settings.PAPER_REVERSAL_TAKE_PROFIT_PCT,
                settings.PAPER_REVERSAL_STOP_LOSS_PCT,
                settings.PAPER_REVERSAL_MAX_HOLD_DAYS,
            ),
        }[account_name]
    mechanics = dict(_DEFAULT_SHORT_SELL_PROFILE)
    if account_name == "default":
        # A remains the owner of PAPER_AUTO_*; do not turn its configurable
        # production policy into module constants while isolating peer accounts.
        mechanics.update({
            "pullback_from_high_pct": settings.PAPER_AUTO_PULLBACK_FROM_HIGH_PCT,
            "breakeven_protect_high_profit_pct": settings.PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PROFIT_PCT,
            "breakeven_protect_low_pct": settings.PAPER_AUTO_BREAKEVEN_PROTECT_LOW_PCT,
            "breakeven_protect_high_pct": settings.PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PCT,
            "volume_negative_ratio": settings.PAPER_AUTO_VOLUME_NEGATIVE_RATIO,
            "open_noise_end": settings.PAPER_AUTO_OPEN_NOISE_END,
            "trade_t_enabled": settings.PAPER_AUTO_TRADE_T_ENABLED,
            "t_sell_pct": settings.PAPER_AUTO_T_SELL_PCT,
            "t_weak_sell_pct": settings.PAPER_AUTO_T_WEAK_SELL_PCT,
            "short_full_exit_profit_max_amount": settings.PAPER_AUTO_SHORT_FULL_EXIT_PROFIT_MAX_AMOUNT,
        })
    severe_add = 0.0 if account_name in {
        "challenger_a", "challenger_e", "challenger_f2", "tenbagger", "reversal",
    } else 2.0
    return {
        **mechanics,
        "take_profit_pct": take_profit,
        "stop_loss_pct": stop_loss,
        "small_stop_loss_pct": (
            settings.PAPER_AUTO_SMALL_STOP_LOSS_PCT
            if account_name == "default" else stop_loss
        ),
        "open_severe_stop_loss_pct": (
            settings.PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT
            if account_name == "default" else stop_loss + severe_add
        ),
        "next_day_min_profit_pct": (
            settings.PAPER_AUTO_NEXT_DAY_MIN_PROFIT_PCT
            if account_name == "default" else -99.0
        ),
        "max_hold_days": max_hold,
        # 2026-09-17 复盘修复：开盘噪声窗独立证据门槛随账户参数冻结并进入 exit_parameters 审计。
        "open_noise_stop_min_evidence": settings.PAPER_AUTO_OPEN_NOISE_STOP_MIN_EVIDENCE,
        "open_noise_weak_min_evidence": settings.PAPER_AUTO_OPEN_NOISE_WEAK_MIN_EVIDENCE,
    }


ROUTE_ACCOUNT_NAMES = {
    "momentum_first_retest": "challenger_a",
    "b_weak_open_second_board": "challenger_b",
    "c_recent_limit_relaunch": "challenger_c",
    "d_auction_recovery": "challenger_d",
    "f2_highboard_break_reclaim": "challenger_f2",
}


def account_confirmation_policy(account_name: str = "default") -> dict[str, Any]:
    if account_name not in _CONFIRMATION_ACCOUNTS:
        raise KeyError(f"account has no champion confirmation path: {account_name}")
    explicit = settings.PAPER_ACCOUNT_CONFIRMATION_POLICIES.get(account_name)
    if explicit is not None:
        return explicit.model_dump()
    if account_name == "default":
        return {
            "min_samples": settings.PAPER_INTRADAY_CONFIRM_MIN_SAMPLES,
            "min_persistence_sec": settings.PAPER_INTRADAY_CONFIRM_MIN_PERSISTENCE_SEC,
            "max_sample_gap_sec": settings.PAPER_INTRADAY_CONFIRM_MAX_SAMPLE_GAP_SEC,
            "clock_jitter_sec": settings.PAPER_CONFIRMATION_CLOCK_JITTER_SEC,
            "max_pullback_from_high_pct": settings.PAPER_INTRADAY_CONFIRM_MAX_PULLBACK_FROM_HIGH_PCT,
        }
    return PaperConfirmationPolicy().model_dump()


def route_signal_policy(route_id: str) -> dict[str, Any]:
    name = ROUTE_ACCOUNT_NAMES[route_id]
    if name == "challenger_a":
        raise KeyError("A2 owns PAPER_MOMENTUM_RETEST_* rather than reclaim rules")
    profile = settings.PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES.get(name)
    return (profile if profile is not None else PaperRouteSignalPolicy()).model_dump()


def challenger_execution_policy(route_id: str) -> dict[str, Any]:
    name = ROUTE_ACCOUNT_NAMES[route_id]
    profile = settings.PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES.get(name)
    return (profile if profile is not None else PaperChallengerExecutionPolicy()).model_dump()


def account_parameter_snapshot(account_name: str) -> dict[str, Any]:
    """Return only this account's effective strategy and shared hard constraints."""
    if account_name not in _ACCOUNT_KEYS:
        raise KeyError(f"unknown paper account: {account_name}")
    prefixes = _ACCOUNT_PREFIXES[account_name]
    namespaced = tuple(
        key for key in type(settings).model_fields
        if any(key.startswith(prefix) for prefix in prefixes)
        and key not in {"PAPER_STRATEGY_B_VERSION", "PAPER_STRATEGY_C_VERSION", "PAPER_STRATEGY_D_VERSION"}
    )
    account_keys = tuple(dict.fromkeys((*_ACCOUNT_KEYS[account_name], *namespaced)))
    route_id = next((route for route, name in ROUTE_ACCOUNT_NAMES.items() if name == account_name), None)
    route_signal = route_signal_policy(route_id) if route_id and account_name != "challenger_a" else {}
    return {
        "account_name": account_name,
        "account": _values(account_keys),
        "sell": account_sell_params(account_name),
        "confirmation": account_confirmation_policy(account_name) if account_name in _CONFIRMATION_ACCOUNTS else {},
        "route_signal": route_signal,
        "route_execution": challenger_execution_policy(route_id) if route_id else {},
        "shared_execution": {
            **_values(_COMMON_EXECUTION_KEYS),
            **({"PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE": settings.PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE}
               if route_signal else {}),
        },
        "shared_strategy_constraints": {},
    }


def account_entry_exit_policy(account_name: str) -> dict[str, Any]:
    """Normalized values consumed by entry sizing and exit integration."""
    snapshot = account_parameter_snapshot(account_name)["account"]
    prefix = {
        "challenger_a": "PAPER_CHALLENGER_A",
        "challenger_b": "PAPER_CHALLENGER_B",
        "challenger_c": "PAPER_CHALLENGER_C",
        "challenger_d": "PAPER_CHALLENGER_D",
        "challenger_e": "PAPER_CHALLENGER_E",
        "challenger_f2": "PAPER_CHALLENGER_F2",
    }.get(account_name)
    if prefix is None:
        raise KeyError(f"normalized policy is reserved for challenger accounts: {account_name}")
    return {
        "position_pct": snapshot[f"{prefix}_POSITION_PCT"],
        "max_daily_buys": snapshot[f"{prefix}_MAX_DAILY_BUYS"],
        "max_positions": snapshot[f"{prefix}_MAX_POSITIONS"],
        "take_profit_pct": snapshot[f"{prefix}_TAKE_PROFIT_PCT"],
        "stop_loss_pct": snapshot[f"{prefix}_STOP_LOSS_PCT"],
        "max_hold_days": snapshot[f"{prefix}_MAX_HOLD_DAYS"],
    }
