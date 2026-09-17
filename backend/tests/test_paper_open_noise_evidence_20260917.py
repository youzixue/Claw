"""开盘噪声窗"独立走弱证据"修复的回归测试（2026-09-17 复盘 T1-1 / T1-2）。

缺陷背景
--------
开盘噪声窗（09:30 - PAPER_AUTO_OPEN_NOISE_END，默认 09:45）内，止损豁免原写作::

    if open_noise and profit_pct > -open_severe_stop_loss_pct and weak_confirmations < 2:
        return ""

其中 ``weak_confirmations`` 来自 ``_short_weak_confirmation_count``，5 项里
「现价<开盘」「现价<均价」在止损价被击穿时**必然成立**（止损价低于成本，
价格跌到止损位必然同时低于当日开盘与分时均价），对"噪声回踩 vs 真实走弱"
没有区分度。

生产证据：历史 61 次窗内止损触发中，该计数 <2 的为 **0 次**
（分布 2:5 / 3:17 / 4:34 / 5:5，下界恒为 2），
即豁免分支从未生效，``PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT`` 形同虚设。
最典型个案：605580 恒盛能源 09:31:04 被 2.5% 紧止损砍在 22.65，
当日收 23.39、最高 23.69，错失 +2.59%。

修复
----
新增 ``_short_independent_weak_evidence_count``，剔除**确证同义反复**的两项
（现价<开盘 / 现价<均价），保留有区分度的信号（现价<MA5 / 五档卖压 /
放量下跌 / 5分钟急跌）。实测各命中率：开盘 62%、均价 62%、MA5 35%、
五档 18%、量比 67% —— 只有前两项在止损点上必然成立。

窗内止损要求 ``>= PAPER_AUTO_OPEN_NOISE_STOP_MIN_EVIDENCE``（默认 2；
剔除两项后的分布为 {1:9, 2:33, 3:17, 4:2}，取 1 则豁免仍不生效，
取 2 豁免 15%，保守可解释）；
窗内弱触发要求 ``>= PAPER_AUTO_OPEN_NOISE_WEAK_MIN_EVIDENCE``（默认 1），
取代原先的一律硬禁止，以减轻 09:45 整点集中释放。

**边界说明**：605580 恒盛能源在新规则下**仍然会止损** —— 它命中
「跌破MA5」与「放量下跌」两项，达到门槛。要豁免它需要把"五档买盘占优"
视为对豁免的否决票（该股当时五档 +0.67），属于另一条独立假设，未随本次修复启用。
"""

from __future__ import annotations

from datetime import datetime, time
from types import SimpleNamespace

import pytest

from app.api.v1 import paper as paper_api
from app.api.v1.paper import (
    _is_open_noise_window,
    _short_independent_weak_evidence_count,
    _short_sell_reason,
    _short_weak_confirmation_count,
)
from app.config.settings import settings


# --------------------------------------------------------------------------
# 1. 独立证据计数本身
# --------------------------------------------------------------------------

def test_independent_evidence_excludes_tautological_items():
    """确证同义反复的两项（现价<开盘 / 现价<均价）不得计入独立证据。

    构造：价格低于开盘与均价（止损点上的必然情形），但未跌破 MA5、
    五档无卖压、量比不足 —— 独立证据必须为 0，否则豁免永远不可能生效。
    """
    count = _short_independent_weak_evidence_count(
        price=10.00, open_price=10.50, avg_price=10.30, ma5=9.80,
        min5_change=None, orderbook_imbalance=0.40, volume_ratio=0.9,
        change_pct=-0.30,
    )
    assert count == 0


def test_ma5_break_counts_as_evidence():
    """现价<MA5 实测命中率仅 35%，不是同义反复，必须计入。"""
    assert _short_independent_weak_evidence_count(
        price=10.00, open_price=9.90, avg_price=9.90, ma5=10.05,
        min5_change=None, orderbook_imbalance=0.10, volume_ratio=0.9,
        change_pct=0.20,
    ) == 1


def test_real_case_605580_evidence_count():
    """605580 恒盛能源 2026-09-17 09:31 实测：MA5 破位 + 放量下跌 = 2 项。"""
    assert _short_independent_weak_evidence_count(
        price=22.65, open_price=23.14, avg_price=22.91, ma5=22.69,
        min5_change=None, orderbook_imbalance=0.6739, volume_ratio=9.79,
        change_pct=-1.61,
    ) == 2


def test_independent_evidence_counts_orderbook_and_min5():
    assert _short_independent_weak_evidence_count(
        price=10.0, open_price=10.2, avg_price=10.1, ma5=9.7,
        min5_change=None, orderbook_imbalance=-0.49, volume_ratio=0.5,
        change_pct=-1.0,
    ) == 1
    assert _short_independent_weak_evidence_count(
        price=10.0, open_price=9.8, avg_price=9.9, ma5=9.7,
        min5_change=-1.4, orderbook_imbalance=0.4, volume_ratio=0.5,
        change_pct=1.0,
    ) == 1
    assert _short_independent_weak_evidence_count(
        price=10.0, open_price=10.2, avg_price=10.1, ma5=10.15,
        min5_change=-1.4, orderbook_imbalance=-0.49, volume_ratio=2.0,
        change_pct=-2.0,
    ) == 4


def test_weak_confirmation_count_semantics_unchanged():
    """原 5 项计数语义保持不变（仍被板块退潮佐证使用）。"""
    assert _short_weak_confirmation_count(
        price=22.65, open_price=23.14, avg_price=22.91, ma5=22.69,
        orderbook_imbalance=0.6739, volume_ratio=9.79, change_pct=-1.61,
    ) == 4


# --------------------------------------------------------------------------
# 2. 生产个案回归：605580 恒盛能源 2026-09-17 09:31:04
# --------------------------------------------------------------------------

REAL_CASE = dict(
    code="605580",
    name="恒盛能源",
    # 成本 23.375，止损价 22.79（-2.5%）
    buy_price=23.375,
    stop_loss_price=22.79,
)


def _position():
    return SimpleNamespace(
        code=REAL_CASE["code"],
        name=REAL_CASE["name"],
        buy_price=REAL_CASE["buy_price"],
        stop_loss_price=REAL_CASE["stop_loss_price"],
    )


def _ctx_605580():
    return {
        "price": 22.65,
        "open": 23.14,
        "high": 23.14,
        "low": 22.64,
        "change_pct": -1.61,
        "volume_ratio": 9.79,
        "avg_price": 22.91,
        "ma5": 22.69,
        "orderbook_imbalance": 0.6739,
        "min5_change": None,
        "stop_loss_price": 22.79,
        "prev_was_limit_up": False,
        "open_gap_from_prev_close_pct": 0.52,
        "limit_down": 20.72,
    }


AT_0931 = datetime(2026, 9, 17, 9, 31, 4)


def test_real_case_605580_still_stops_under_corrected_rule():
    """605580 在新规则下仍然止损（命中 MA5 破位 + 放量下跌 = 2 项，达到门槛）。

    这是诚实边界：本次修复让豁免分支**可以**生效，但不豁免这一笔。
    要豁免它需要把"五档买盘占优"(+0.67) 视为否决票，属另一条独立假设，未启用。
    """
    reason = _short_sell_reason(
        _position(), _ctx_605580(), profit_pct=-3.10, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931,
        params={
            "stop_loss_pct": 2.5,
            "small_stop_loss_pct": 2.5,
            "open_severe_stop_loss_pct": 4.5,
            "open_noise_end": "09:45",
            "take_profit_pct": 8.0,
            "max_hold_days": 3,
        },
    )
    assert reason.startswith("触发持仓止损价"), reason


def test_real_case_suppressed_when_only_one_evidence():
    """同一止损价，但只有 1 项独立证据时，开盘噪声窗必须豁免。

    构造：MA5 在价格下方（无 MA5 破位），仅"放量下跌"成立。
    这正是修复前不可能出现的分支 —— 旧口径下 weak_confirmations 恒 >= 2。
    """
    ctx = _ctx_605580()
    ctx["ma5"] = 22.30          # 价格 22.65 在 MA5 上方，无 MA5 破位
    reason = _short_sell_reason(
        _position(), ctx, profit_pct=-3.10, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931,
        params={
            "stop_loss_pct": 2.5,
            "small_stop_loss_pct": 2.5,
            "open_severe_stop_loss_pct": 4.5,
            "open_noise_end": "09:45",
            "take_profit_pct": 8.0,
            "max_hold_days": 3,
        },
    )
    assert reason == "", f"仅 1 项独立证据时应豁免，实际 {reason!r}"


def test_real_case_old_behaviour_reproducible_with_zero_threshold():
    """把门槛置 0 必须精确复现修复前行为（窗内止损一律放行）。"""
    reason = _short_sell_reason(
        _position(), _ctx_605580(), profit_pct=-3.10, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931,
        params={
            "stop_loss_pct": 2.5,
            "small_stop_loss_pct": 2.5,
            "open_severe_stop_loss_pct": 4.5,
            "open_noise_end": "09:45",
            "take_profit_pct": 8.0,
            "max_hold_days": 3,
            "open_noise_stop_min_evidence": 0,
        },
    )
    assert reason.startswith("触发持仓止损价")


def test_real_case_still_stops_after_open_noise_window():
    """09:45 之后窗口失效，同一行情必须照常止损。"""
    reason = _short_sell_reason(
        _position(), _ctx_605580(), profit_pct=-3.10, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(),
        now=datetime(2026, 9, 17, 9, 45, 30),
        params={
            "stop_loss_pct": 2.5,
            "small_stop_loss_pct": 2.5,
            "open_severe_stop_loss_pct": 4.5,
            "open_noise_end": "09:45",
            "take_profit_pct": 8.0,
            "max_hold_days": 3,
        },
    )
    assert reason.startswith("触发持仓止损价")


def test_real_case_still_stops_when_severe_loss():
    """亏损超过 open_severe_stop_loss_pct 时，窗内豁免必须让路。"""
    ctx = _ctx_605580()
    ctx["ma5"] = 22.30
    reason = _short_sell_reason(
        _position(), ctx, profit_pct=-5.0, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931,
        params={
            "stop_loss_pct": 2.5,
            "small_stop_loss_pct": 2.5,
            "open_severe_stop_loss_pct": 4.5,
            "open_noise_end": "09:45",
            "take_profit_pct": 8.0,
            "max_hold_days": 3,
        },
    )
    assert reason.startswith("触发持仓止损价")


def test_real_case_still_stops_with_three_independent_evidence():
    """具备 3 项独立证据时窗内照常止损。"""
    ctx = _ctx_605580()
    ctx["orderbook_imbalance"] = -0.62
    reason = _short_sell_reason(
        _position(), ctx, profit_pct=-3.10, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931,
        params={
            "stop_loss_pct": 2.5,
            "small_stop_loss_pct": 2.5,
            "open_severe_stop_loss_pct": 4.5,
            "open_noise_end": "09:45",
            "take_profit_pct": 8.0,
            "max_hold_days": 3,
        },
    )
    assert reason.startswith("触发持仓止损价")


# --------------------------------------------------------------------------
# 3. 窗内弱触发：由"一律硬禁止"改为"独立证据门槛"
# --------------------------------------------------------------------------

def _weak_ctx(**over):
    """构造"跌破分时均价"这一档的弱行情，且**不携带**独立证据。

    price 低于 avg（触发跌破分时均价），但高于 ma5 与 open（不产生 MA5 证据），
    五档为正、量比不足、无 5 分钟动能 —— 独立证据为 0，用于验证窗内屏蔽。
    """
    ctx = {
        "price": 9.96,
        "open": 9.95,
        "high": 10.08,
        "low": 9.95,
        "change_pct": -0.40,
        "volume_ratio": 0.9,
        "avg_price": 10.00,
        "ma5": 9.80,
        "orderbook_imbalance": 0.10,
        "min5_change": None,
        "stop_loss_price": 9.00,   # 未触及止损
        "prev_was_limit_up": False,
        "open_gap_from_prev_close_pct": 0.0,
        "limit_down": 9.00,
    }
    ctx.update(over)
    return ctx


def _weak_position():
    return SimpleNamespace(code="000001", name="测试", buy_price=10.0, stop_loss_price=9.0)


WEAK_PARAMS = {
    "stop_loss_pct": 5.0,
    "small_stop_loss_pct": 5.0,
    "open_severe_stop_loss_pct": 5.0,
    "open_noise_end": "09:45",
    "take_profit_pct": 8.0,
    "max_hold_days": 5,
    "next_day_min_profit_pct": -99.0,
    "trade_t_enabled": False,
}


def test_weak_trigger_blocked_in_window_without_independent_evidence():
    """窗前、零独立证据的"跌破分时均价"仍须屏蔽（保持开盘噪声保护）。"""
    reason = _short_sell_reason(
        _weak_position(), _weak_ctx(), profit_pct=-0.4, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931, params=WEAK_PARAMS,
    )
    assert reason == "", reason


def test_weak_trigger_allowed_in_window_with_independent_evidence():
    """窗前、带 1 项独立证据（放量下跌）的弱触发可提前生效，减轻 09:45 集中释放。"""
    reason = _short_sell_reason(
        _weak_position(),
        _weak_ctx(volume_ratio=2.5, change_pct=-1.2), profit_pct=-0.4, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931, params=WEAK_PARAMS,
    )
    assert reason.startswith("跌破分时均价"), reason


def test_weak_trigger_orderbook_evidence_allowed_in_window():
    reason = _short_sell_reason(
        _weak_position(), _weak_ctx(orderbook_imbalance=-0.55), profit_pct=-0.4, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931, params=WEAK_PARAMS,
    )
    assert reason.startswith("跌破分时均价"), reason


def test_weak_trigger_min5_evidence_allowed_in_window():
    reason = _short_sell_reason(
        _weak_position(), _weak_ctx(min5_change=-1.6), profit_pct=-0.4, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931, params=WEAK_PARAMS,
    )
    assert reason.startswith("跌破分时均价"), reason


def test_weak_hard_block_restorable_with_99():
    """open_noise_weak_min_evidence=99 必须恢复修复前的一律硬禁止。"""
    params = dict(WEAK_PARAMS)
    params["open_noise_weak_min_evidence"] = 99
    reason = _short_sell_reason(
        _weak_position(), _weak_ctx(orderbook_imbalance=-0.55), profit_pct=-0.4, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931, params=params,
    )
    assert reason == ""


def test_weak_trigger_unaffected_after_window():
    """窗外不受门槛约束，零独立证据也照常触发。"""
    reason = _short_sell_reason(
        _weak_position(), _weak_ctx(), profit_pct=-0.4, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(),
        now=datetime(2026, 9, 17, 10, 30, 0), params=WEAK_PARAMS,
    )
    assert reason.startswith("跌破分时均价"), reason


# --------------------------------------------------------------------------
# 4. 窗口边界与配置默认值
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "moment,expected",
    [
        (time(9, 29, 59), False),
        (time(9, 30, 0), True),
        (time(9, 44, 59), True),
        (time(9, 45, 0), False),
    ],
)
def test_open_noise_window_boundaries(moment, expected):
    now = datetime(2026, 9, 17, moment.hour, moment.minute, moment.second)
    assert _is_open_noise_window(now, end_value="09:45") is expected


def test_settings_defaults():
    assert settings.PAPER_AUTO_OPEN_NOISE_STOP_MIN_EVIDENCE == 2
    assert settings.PAPER_AUTO_OPEN_NOISE_WEAK_MIN_EVIDENCE == 1
    assert settings.PAPER_AUTO_OPEN_NOISE_END == "09:45"


def test_account_params_expose_new_thresholds():
    from app.paper.account_policy import account_sell_params

    for account in (
        "default", "promotion", "mainline", "auction", "tenbagger", "reversal",
        "challenger_a", "challenger_b", "challenger_c", "challenger_d",
        "challenger_e", "challenger_f2",
    ):
        params = account_sell_params(account)
        assert "open_noise_stop_min_evidence" in params, account
        assert "open_noise_weak_min_evidence" in params, account
        assert params["open_noise_stop_min_evidence"] == 2
        assert params["open_noise_weak_min_evidence"] == 1


def test_missing_market_data_is_safe():
    """空数据（缺五档/量比/5分钟）不得抛异常，且不得凭空产生独立证据。"""
    assert _short_independent_weak_evidence_count(
        price=None, open_price=None, avg_price=None, ma5=None,
        min5_change=None, orderbook_imbalance=None, volume_ratio=None,
        change_pct=None,
    ) == 0
    reason = _short_sell_reason(
        _weak_position(),
        {"price": None, "open": None, "high": None, "change_pct": None,
         "volume_ratio": None, "avg_price": None, "ma5": None,
         "orderbook_imbalance": None, "min5_change": None,
         "stop_loss_price": None, "prev_was_limit_up": False, "limit_down": None},
        profit_pct=-1.0, hold_days=1,
        trade_date=datetime(2026, 9, 17).date(), now=AT_0931, params=WEAK_PARAMS,
    )
    assert reason == ""
