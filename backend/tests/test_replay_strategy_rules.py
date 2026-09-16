from datetime import date

import pandas as pd

from app.config.settings import settings
from app.models.stock import StockKline
from scripts.replay_break_reversal import SignalParams, _detect_signals
from scripts.replay_five_strategies import (
    ReplayConfig,
    ReplayTrade,
    _apply_portfolio_constraints,
    _simulate_hold,
    _strategy_configs,
)


def _bar(
    trade_day: date,
    *,
    open_price: float,
    close: float,
    prev_close: float,
    change_pct: float,
    high: float | None = None,
    low: float | None = None,
) -> StockKline:
    return StockKline(
        code="600001",
        trade_date=trade_day,
        open=open_price,
        close=close,
        high=high if high is not None else max(open_price, close),
        low=low if low is not None else min(open_price, close),
        prev_close=prev_close,
        change_pct=change_pct,
        volume=100_000,
    )


def test_replay_default_configs_match_live_strategy_settings():
    configs = _strategy_configs()

    assert configs["B"].take_profit_pct == settings.PAPER_PROMOTION_TAKE_PROFIT_PCT
    assert configs["B"].stop_loss_pct == settings.PAPER_PROMOTION_STOP_LOSS_PCT
    assert configs["B"].position_pct == settings.PAPER_PROMOTION_POSITION_PCT
    assert configs["B"].max_positions == settings.PAPER_PROMOTION_MAX_POSITIONS
    assert configs["C"].take_profit_pct == settings.PAPER_MAINLINE_TAKE_PROFIT_PCT
    assert configs["C"].stop_loss_pct == settings.PAPER_MAINLINE_STOP_LOSS_PCT
    assert configs["D"].max_hold_days == settings.PAPER_AUCTION_MAX_HOLD_DAYS
    assert configs["E"].stop_loss_pct == settings.PAPER_HIGHBOARD_STOP_LOSS_PCT
    assert configs["E"].max_daily_buys == settings.PAPER_TENBAGGER_MAX_DAILY_BUYS


def test_five_strategy_replay_defers_buy_day_stop_to_t1_open():
    cfg = ReplayConfig(
        strategy="E",
        account_name="tenbagger",
        take_profit_pct=18.0,
        stop_loss_pct=6.0,
        max_hold_days=3,
        min_probability=0.0,
    )
    signal_date = date(2026, 8, 28)
    bars = [
        _bar(
            signal_date,
            open_price=10.0,
            close=10.0,
            prev_close=9.1,
            change_pct=9.89,
        ),
        _bar(
            date(2026, 8, 31),
            open_price=10.0,
            close=9.2,
            prev_close=10.0,
            change_pct=-8.0,
        ),
        _bar(
            date(2026, 9, 1),
            open_price=8.8,
            close=9.0,
            prev_close=9.2,
            change_pct=-2.17,
        ),
    ]

    trade = _simulate_hold(
        cfg,
        "600001",
        "主板测试",
        signal_date,
        bars,
        {},
        {"consecutive_days": 4},
    )

    assert trade is not None
    assert trade.buy_date == date(2026, 8, 31)
    assert trade.sell_date == date(2026, 9, 1)
    assert trade.sell_price == 8.8
    assert trade.shares >= 100
    assert "T+1" in trade.sell_reason


def test_five_strategy_replay_right_censors_incomplete_holding_window():
    cfg = ReplayConfig(
        strategy="E",
        account_name="tenbagger",
        take_profit_pct=18.0,
        stop_loss_pct=6.0,
        max_hold_days=3,
        min_probability=0.0,
    )
    signal_date = date(2026, 8, 28)
    bars = [
        _bar(
            signal_date,
            open_price=10.0,
            close=10.0,
            prev_close=9.1,
            change_pct=9.89,
        ),
        _bar(
            date(2026, 8, 31),
            open_price=10.0,
            close=10.1,
            prev_close=10.0,
            change_pct=1.0,
        ),
        _bar(
            date(2026, 9, 1),
            open_price=10.1,
            close=10.2,
            prev_close=10.1,
            change_pct=0.99,
        ),
    ]

    assert (
        _simulate_hold(
            cfg,
            "600001",
            "主板测试",
            signal_date,
            bars,
            {},
            {"consecutive_days": 4},
        )
        is None
    )


def test_all_strategy_replays_reject_limit_up_open_as_unproven_fill():
    cfg = ReplayConfig(
        strategy="B",
        account_name="promotion",
        take_profit_pct=8.0,
        stop_loss_pct=6.0,
        max_hold_days=1,
        min_probability=0.25,
    )
    signal_date = date(2026, 8, 28)
    bars = [
        _bar(
            signal_date,
            open_price=10.0,
            close=10.0,
            prev_close=9.1,
            change_pct=9.89,
        ),
        _bar(
            date(2026, 8, 31),
            open_price=11.0,
            close=10.8,
            prev_close=10.0,
            change_pct=8.0,
            high=11.0,
            low=10.7,
        ),
        _bar(
            date(2026, 9, 1),
            open_price=10.9,
            close=11.2,
            prev_close=10.8,
            change_pct=3.7,
        ),
    ]

    assert (
        _simulate_hold(
            cfg,
            "600001",
            "主板测试",
            signal_date,
            bars,
            {},
            {"probability": 0.8},
        )
        is None
    )


def test_portfolio_constraints_limit_daily_buys_and_concurrent_positions():
    cfg = ReplayConfig(
        strategy="E",
        account_name="tenbagger",
        take_profit_pct=18.0,
        stop_loss_pct=6.0,
        max_hold_days=3,
        min_probability=0.0,
        max_daily_buys=2,
        max_positions=2,
    )
    trades = [
        ReplayTrade(
            code=f"60000{index}",
            name=str(index),
            signal_date=date(2026, 8, 28),
            buy_date=date(2026, 8, 31),
            buy_price=10,
            sell_date=date(2026, 9, 2),
            sell_price=11,
            shares=100,
            profit_pct=10,
            pnl=100,
            hold_days=2,
            sell_reason="test",
            signal_meta={"probability": probability},
        )
        for index, probability in enumerate((0.9, 0.8, 0.7), start=1)
    ]

    selected, blocked = _apply_portfolio_constraints(trades, cfg)

    assert [item.code for item in selected] == ["600001", "600002"]
    assert blocked == 1


def test_break_reversal_uses_window_low_not_previous_close_change():
    rows = [
        ("2026-08-10", 8.0, 8.0, 8.0, 8.0, 8.0, 0.0, 100_000),
        ("2026-08-11", 8.0, 8.8, 8.8, 8.0, 8.0, 10.0, 100_000),
        ("2026-08-12", 8.8, 9.68, 9.68, 8.8, 8.8, 10.0, 100_000),
        ("2026-08-13", 9.68, 10.65, 10.65, 9.68, 9.68, 10.0, 100_000),
        ("2026-08-14", 10.4, 10.2, 10.5, 9.7, 10.65, -4.23, 100_000),
        ("2026-08-17", 10.4, 11.22, 11.22, 10.3, 10.2, 10.0, 200_000),
    ]
    frame = pd.DataFrame(
        [
            {
                "code": "600001",
                "trade_date": trade_day,
                "open": open_price,
                "close": close,
                "high": high,
                "low": low,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "volume": volume,
            }
            for (
                trade_day,
                open_price,
                close,
                high,
                low,
                prev_close,
                change_pct,
                volume,
            ) in rows
        ]
    )
    params = SignalParams(
        min_consecutive=3,
        max_gap_days=1,
        min_vol_ratio=1.5,
        take_profit_pct=15.0,
        stop_loss_pct=8.0,
        max_hold_days=5,
    )

    signals = _detect_signals(frame, params)

    assert len(signals) == 1
    assert signals[0]["gap_days"] == 1
    assert signals[0]["prev_day_chg"] > -5.0
    assert signals[0]["window_dip_pct"] < -5.0
