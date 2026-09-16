import app.signal.b1_signal as b1_signal_module
from app.api.v1.tenbagger import _compare_aggregated_rows


def test_analyze_b1_signal_returns_empty_for_short_history():
    bars = [
        {
            "open": 10 + idx * 0.1,
            "high": 10.2 + idx * 0.1,
            "low": 9.8 + idx * 0.1,
            "close": 10.1 + idx * 0.1,
            "volume": 1_000_000 + idx * 1_000,
            "turnover": 3.5,
            "change_pct": 0.8,
        }
        for idx in range(30)
    ]

    result = b1_signal_module.analyze_b1_signal("000001", bars)

    assert result["signal_key"] is None
    assert result["signal_label"] == ""
    assert result["priority_bonus"] == 0
    assert result["success_samples"] == 0
    assert result["success_rate_5d"] is None
    assert result["is_break_trend"] is False


def test_history_stats_prefers_same_signal_when_samples_enough(monkeypatch):
    bars = [
        {
            "close": float(idx + 1),
            "open": float(idx + 1),
            "high": float(idx + 1.2),
            "low": float(idx + 0.8),
            "volume": 1_000_000,
            "turnover": 5.0,
            "change_pct": 1.0,
        }
        for idx in range(90)
    ]
    signal_days = {
        66: "volume_b1",
        70: "volume_b1",
        74: "volume_b1",
        78: "white_line_retest_b1",
    }

    def fake_evaluate(_code, subset):
        idx = len(subset) - 1
        signal_key = signal_days.get(idx)
        return {
            "signal_key": signal_key,
            "signal_label": b1_signal_module.SIGNAL_LABELS.get(signal_key, ""),
            "hold_score": 4,
            "j": 10,
            "rsi": 20,
            "short_score": 15,
            "long_score": 82,
            "kdj_signal": "",
            "close": float(idx + 1),
        }

    monkeypatch.setattr(b1_signal_module, "_evaluate_snapshot", fake_evaluate)

    result = b1_signal_module._history_stats("000001", bars, current_signal_key="volume_b1", future_days=3)

    assert result["success_scope"] == "same_signal"
    assert result["success_samples"] == 3
    assert result["success_rate"] == 1.0
    assert result["history_high_confidence"] is True


def test_analyze_b1_signal_exposes_3d_5d_and_break_trend(monkeypatch):
    bars = [
        {
            "close": float(idx + 1),
            "open": float(idx + 1),
            "high": float(idx + 1.2),
            "low": float(idx + 0.8),
            "volume": 1_000_000,
            "turnover": 5.0,
            "change_pct": 1.0,
        }
        for idx in range(90)
    ]

    monkeypatch.setattr(
        b1_signal_module,
        "_evaluate_snapshot",
        lambda _code, _bars: {
            "signal_key": "volume_b1",
            "signal_label": "缩量B1",
            "hold_score": 4,
            "j": 12,
            "rsi": 18,
            "short_score": 11,
            "long_score": 80,
            "kdj_signal": "golden_cross",
            "close": 12.8,
            "trend_white_price": 12.6,
            "is_break_trend": False,
        },
    )

    def fake_history(_code, _bars, _signal_key, future_days=3):
        if future_days == 3:
            return {
                "success_rate": 0.66,
                "success_samples": 6,
                "success_scope": "same_signal",
                "history_high_confidence": True,
            }
        return {
            "success_rate": 0.83,
            "success_samples": 6,
            "success_scope": "same_signal",
            "history_high_confidence": True,
        }

    monkeypatch.setattr(b1_signal_module, "_history_stats", fake_history)

    result = b1_signal_module.analyze_b1_signal("000001", bars)

    assert result["success_rate_3d"] == 0.66
    assert result["success_rate_5d"] == 0.83
    assert result["success_samples"] == 6
    assert result["success_samples_5d"] == 6
    assert result["trend_white_price"] == 12.6
    assert result["is_break_trend"] is False
    assert result["priority_bonus"] == 24


def test_priority_sort_prefers_b1_priority_score_bonus():
    stronger_b1_row = {
        "priority_score": 118.0,
        "b1_priority_bonus": 16,
        "display_score": 102.0,
        "setup_grade": "A2 盘口确认后执行",
        "latest_as_of": "2026-04-21T14:30:00",
    }
    higher_display_row = {
        "priority_score": 112.0,
        "b1_priority_bonus": 0,
        "display_score": 112.0,
        "setup_grade": "A1 可直接执行",
        "latest_as_of": "2026-04-21T14:31:00",
    }

    ordered = sorted(
        [higher_display_row, stronger_b1_row],
        key=lambda row: _compare_aggregated_rows(row, "priority"),
        reverse=True,
    )

    assert ordered[0] is stronger_b1_row
