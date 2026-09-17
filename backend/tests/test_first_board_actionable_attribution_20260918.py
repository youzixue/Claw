"""首板路线 `actionable` 的归因：两级判定必须分开留证。

实测（2026-09-01 起，全库）：
    pre_board_probe_start   board=1  n=37789  actionable=0
    oversold_reversal_start board=1  n= 8364  actionable=0
    news_catalyst_start     board=1  n= 7485  actionable=0
    mainline_spread_start   board=1  n=  780  actionable=0
    auction_surge_start     board=1  n= 1975  actionable=4
    另 6 条首板路线          board=1  n=  190  actionable=0
    second_board_promotion  board=2  n= 3871  actionable=420
九条首板路线合计约 5.6 万条预测，actionable 总计 4 条。

根因链（promotion.py）：
    prediction_actionable = _is_actionable_first_board_prediction(item)
                            AND _first_board_rank_has_active_confirmation(item)
第二级只认「盘后硬新闻」或「已验证竞价证据」，而实测 109/109 条 mainline
记录的 `auction_evidence_status` 全是 `unknown`、新闻字段全不存在。

在此之前这两级混在一个 0 里，无法回答"到底是路线门槛挡的，还是确认证据缺失"。
本文件锁住归因出参，并锁住「打开归因不改变判定结果」。
"""
from __future__ import annotations

from app.api.v1 import promotion


def _item(**over):
    item = {
        "candidate_route": "mainline_spread_start",
        "probability": 0.5,
        "support_strength_score": 80.0,
        "main_net_inflow_pct": 5.0,
        "route_score": 80.0,
        "sector_strength_score": 80.0,
        "probability_factors": {
            "market_risk_level": "normal",
            "broad_rotation_member_setup": True,
            "sector_catalyst_spread": True,
            "strict_confirmation_count": 3,
        },
    }
    item.update(over)
    return item


def test_attribution_does_not_change_the_verdict():
    """核心保证：传 reject_reasons 只记账，返回的布尔必须逐位相同。"""
    samples = [
        _item(),
        _item(probability=0.0),
        _item(keep_in_diagnostics=True),
        _item(trade_ready=False),
        _item(time_horizon="weak_watch"),
        _item(candidate_route="news_catalyst_start"),
        _item(candidate_route="pre_board_probe_start"),
        _item(candidate_route="auction_surge_start"),
        _item(support_strength_score=None, main_net_inflow_pct=None),
        _item(route_score=0.0),
        _item(sector_strength_score=10.0),
    ]
    # 补一组 hostile（走路线分支）
    hostile = _item(probability_factors={
        "market_risk_level": "hostile",
        "broad_rotation_member_setup": True,
        "sector_catalyst_spread": True,
        "strict_confirmation_count": 3,
    })
    hostile_low = _item(
        route_score=1.0, sector_strength_score=1.0, support_strength_score=1.0,
        probability_factors={
            "market_risk_level": "hostile",
            "broad_rotation_member_setup": True,
            "sector_catalyst_spread": True,
            "strict_confirmation_count": 0,
        },
    )
    for item in [*samples, hostile, hostile_low]:
        plain = promotion._is_actionable_first_board_prediction(item)
        reasons: list[str] = []
        with_attr = promotion._is_actionable_first_board_prediction(
            item, reject_reasons=reasons)
        assert plain == with_attr, (item, plain, with_attr)
        if not plain:
            # 判负时必须能说出至少一条原因（除非被 route_gate 委派）
            assert reasons, item


def test_reasons_name_the_actual_blocker():
    reasons: list[str] = []
    assert promotion._is_actionable_first_board_prediction(
        _item(probability=0.0), reject_reasons=reasons) is False
    assert any(r.startswith("probability_below_min") for r in reasons), reasons

    reasons = []
    promotion._is_actionable_first_board_prediction(
        _item(time_horizon="weak_watch"), reject_reasons=reasons)
    assert "time_horizon_weak_watch" in reasons, reasons

    reasons = []
    promotion._is_actionable_first_board_prediction(
        _item(keep_in_diagnostics=True), reject_reasons=reasons)
    assert "keep_in_diagnostics_or_not_trade_ready" in reasons, reasons


def test_missing_inputs_are_recorded_but_do_not_flip_the_verdict():
    """缺输入只记账，本次**不改变**判定 —— 先取证再决定是否收紧。

    `support_strength_score` / `main_net_inflow_pct` 没有出现在
    `promotion_prediction_record.factors_json` 里，而
    `main_inflow_pct >= -6.5` 这类负阈值守护会被 `_safe_float(None)=0.0`
    静默满足。是否真实存在无法从落库反推，故先记录。
    """
    reasons: list[str] = []
    plain = promotion._is_actionable_first_board_prediction(
        _item(support_strength_score=None, main_net_inflow_pct=None))
    with_attr = promotion._is_actionable_first_board_prediction(
        _item(support_strength_score=None, main_net_inflow_pct=None),
        reject_reasons=reasons,
    )
    assert plain == with_attr
    assert any(
        r.startswith("input_missing:") and "ABSENT" in r for r in reasons
    ), reasons
    # 两个字段都存在时不得记这条
    reasons = []
    promotion._is_actionable_first_board_prediction(_item(), reject_reasons=reasons)
    assert not any(r.startswith("input_missing:") for r in reasons), reasons


def test_seal_gene_filter_being_skipped_is_recorded():
    """`memory_features` 缺失会让整段封板基因过滤被静默跳过，必须记账。"""
    reasons: list[str] = []
    promotion._is_actionable_first_board_prediction(
        _item(candidate_route="mainline_spread_start"), reject_reasons=reasons)
    assert "seal_gene_filter_skipped_no_memory_features" in reasons, reasons

    reasons = []
    promotion._is_actionable_first_board_prediction(
        _item(candidate_route="mainline_spread_start",
              memory_features={"memory_limit_up_hits_120d": 2}),
        reject_reasons=reasons)
    assert "seal_gene_filter_skipped_no_memory_features" not in reasons, reasons


def test_active_confirmation_false_is_recorded_separately():
    """没有硬新闻也没有已验证竞价证据时，必须单独标出这一级。"""
    item = _item(candidate_route="mainline_spread_start", probability_factors={
        "market_risk_level": "normal",
        "strict_confirmation_count": 3,
        "auction_cluster_confirmed": False,
        "auction_feed_complete": False,
        "auction_evidence_status": "unknown",
        "auction_evidence_contract": "",
    })
    meta = promotion._first_board_trade_actionability(item)
    assert meta["prediction_actionable"] is False
    assert meta["prediction_active_confirmation"] is False
    assert meta["prediction_route_level_actionable"] is True, (
        "本用例的路线门槛应当通过 —— 判负必须归因到 active_confirmation 这一级"
    )
    assert any(r.startswith("active_confirmation_false") for r in
               meta["prediction_not_actionable_reasons"]), meta
