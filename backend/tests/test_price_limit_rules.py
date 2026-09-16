from datetime import date

import pytest

from app.core.price_limit_rules import (
    GEM_LIMIT_REFORM_DATE,
    ST_MAINBOARD_LIMIT_REFORM_DATE,
    board_for_code,
    is_limit_up_change,
    limit_up_change_threshold,
    price_limit_rule,
)


@pytest.mark.parametrize(
    ("code", "board", "nominal", "threshold"),
    [
        ("600000", "main", 10.0, 9.5),
        ("000001", "main", 10.0, 9.5),
        ("300001", "gem", 20.0, 19.0),
        ("688001", "star", 20.0, 19.0),
        ("920001", "bse", 30.0, 29.0),
    ],
)
def test_board_price_limit_rules(code, board, nominal, threshold):
    rule = price_limit_rule(code, trade_date=date(2026, 8, 28))
    assert board_for_code(code) == board
    assert rule.board == board
    assert rule.nominal_limit_pct == nominal
    assert rule.detection_threshold_pct == threshold


def test_st_mainboard_rule_is_date_effective():
    before = price_limit_rule(
        "600001", name="*ST测试", trade_date=ST_MAINBOARD_LIMIT_REFORM_DATE.replace(day=3)
    )
    after = price_limit_rule(
        "600001", name="*ST测试", trade_date=ST_MAINBOARD_LIMIT_REFORM_DATE
    )

    assert before.nominal_limit_pct == 5.0
    assert before.detection_threshold_pct == 4.7
    assert after.nominal_limit_pct == 10.0
    assert after.detection_threshold_pct == 9.5
    assert after.effective_date == ST_MAINBOARD_LIMIT_REFORM_DATE


def test_adjusted_bar_uses_vendor_tolerant_detection_without_changing_nominal_rule():
    rule = price_limit_rule(
        "300001", trade_date="2026-08-28", adjusted_bar=True
    )
    assert rule.nominal_limit_pct == 20.0
    assert rule.detection_threshold_pct == 18.8
    assert limit_up_change_threshold(
        "300001", trade_date="2026-08-28", adjusted_bar=True
    ) == 18.8
    assert is_limit_up_change(
        "300001", 18.9, trade_date="2026-08-28", adjusted_bar=True
    )
    assert not is_limit_up_change(
        "300001", 18.7, trade_date="2026-08-28", adjusted_bar=True
    )


def test_gem_limit_rule_switches_on_2020_reform_date():
    before = price_limit_rule(
        "300001", trade_date=GEM_LIMIT_REFORM_DATE - date.resolution
    )
    after = price_limit_rule("300001", trade_date=GEM_LIMIT_REFORM_DATE)

    assert before.nominal_limit_pct == 10.0
    assert before.detection_threshold_pct == 9.5
    assert after.nominal_limit_pct == 20.0
    assert after.detection_threshold_pct == 19.0
    assert after.effective_date == GEM_LIMIT_REFORM_DATE


def test_invalid_change_is_not_limit_up():
    assert not is_limit_up_change("600000", None, trade_date="2026-08-28")
