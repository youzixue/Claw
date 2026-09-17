"""改4：中线账户到期平仓不再无条件砍掉盈利仓（2026-09-17 复盘）。

缺陷：`_midline_sell_reason` 的 `hold_days >= max_hold_days` 无条件平仓，
会砍掉尚未走到止盈、但仍在水上的仓位。
实测个案：账户12 603980 持仓 5 个交易日到期时盈利 +1.80% 被平，
而 challenger_a 的目标止盈是 +8.0%。

修法：到期时仅在**未盈利**时平仓；盈利仓给出宽限天数（默认 5），
满宽限后仍强制平仓以避免无限持有。grace=0 恢复修复前行为。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.paper import _midline_sell_reason
from app.config.settings import settings


def _position():
    return SimpleNamespace(code="603980", name="吉华集团", buy_price=8.08,
                           stop_loss_price=7.68, buy_amount=600)


def _ctx(price=8.23):
    return {"price": price, "stop_loss_price": 7.68}


PARAMS = {"take_profit_pct": 8.0, "stop_loss_pct": 5.0, "max_hold_days": 5}


def test_profitable_position_is_not_expired_on_the_due_day():
    """到期日仍盈利 -> 不平仓（修复的核心行为改变）。"""
    assert _midline_sell_reason(_position(), _ctx(8.23), profit_pct=1.80,
                                hold_days=5, params=PARAMS) == ""


def test_profitable_position_is_force_closed_after_grace():
    """满宽限后仍强制平仓，避免无限持有。"""
    grace = settings.PAPER_MIDLINE_EXPIRY_GRACE_DAYS
    reason = _midline_sell_reason(_position(), _ctx(8.23), profit_pct=1.80,
                                  hold_days=5 + grace, params=PARAMS)
    assert "到期平仓" in reason
    assert "宽限" in reason


def test_losing_position_still_expires_on_the_due_day():
    """到期日未盈利 -> 照常平仓（不改变亏损单的处理）。"""
    assert "到期平仓" in _midline_sell_reason(_position(), _ctx(7.90), profit_pct=-2.0,
                                              hold_days=5, params=PARAMS)


def test_flat_position_expires():
    """盈亏为 0 视为未盈利 -> 平仓。"""
    assert "到期平仓" in _midline_sell_reason(_position(), _ctx(8.08), profit_pct=0.0,
                                              hold_days=5, params=PARAMS)


def test_before_due_day_nothing_happens():
    assert _midline_sell_reason(_position(), _ctx(8.23), profit_pct=1.80,
                                hold_days=4, params=PARAMS) == ""


def test_stop_loss_and_take_profit_unaffected():
    """止损/止盈不受宽限影响。"""
    # 价格在止损价之上，走硬止损分支（价格低于止损价时会先命中"触发持仓止损价"）
    assert _midline_sell_reason(_position(), _ctx(7.70), profit_pct=-6.0,
                                hold_days=6, params=PARAMS).startswith("触发硬止损")
    assert _midline_sell_reason(_position(), _ctx(8.80), profit_pct=9.0,
                                hold_days=6, params=PARAMS).startswith("触发短线止盈")


def test_grace_zero_restores_previous_behaviour():
    """grace=0 必须精确复现修复前行为：到期无条件平仓。"""
    params = dict(PARAMS, expiry_grace_days=0)
    reason = _midline_sell_reason(_position(), _ctx(8.23), profit_pct=1.80,
                                  hold_days=5, params=params)
    assert reason == "持仓5个交易日到期平仓"


def test_grace_is_configurable_per_call():
    params = dict(PARAMS, expiry_grace_days=2)
    assert _midline_sell_reason(_position(), _ctx(8.23), profit_pct=1.80,
                                hold_days=6, params=params) == ""
    assert "到期平仓" in _midline_sell_reason(_position(), _ctx(8.23), profit_pct=1.80,
                                              hold_days=7, params=params)


def test_default_grace_is_five_days():
    assert settings.PAPER_MIDLINE_EXPIRY_GRACE_DAYS == 5
