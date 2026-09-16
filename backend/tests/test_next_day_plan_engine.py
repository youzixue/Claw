from app.signal.next_day_plan import next_day_plan_engine


def test_next_day_plan_blocks_high_position_as_direct_buy():
    result = next_day_plan_engine.generate(
        code="000001",
        name="高位强势股",
        bull_level="A",
        bull_score=88,
        price=12.0,
        change_pct=9.8,
        ma5=10.8,
        ma10=10.4,
        ma20=9.8,
        high_20d=12.0,
        high_60d=13.2,
        boll_upper=12.0,
        rsi14=78,
        volume_ratio=1.8,
        fund_5d_billion=2.5,
        is_limit_up=True,
    )

    assert result.strategies[0].strategy_type == "avoid"
    assert any("明日不追高" in reason for reason in result.avoid_reasons)
    assert any("直接买点过高" in reason for reason in result.avoid_reasons)
    assert not any(s.strategy_type in ("strong_get_stronger", "trend", "aggressive") for s in result.strategies)


def test_next_day_plan_allows_near_support_actionable_buy():
    result = next_day_plan_engine.generate(
        code="000002",
        name="贴线低吸股",
        bull_level="A",
        bull_score=86,
        price=10.2,
        change_pct=1.8,
        ma5=10.0,
        ma10=9.8,
        ma20=9.5,
        high_20d=12.0,
        high_60d=13.0,
        boll_upper=12.8,
        rsi14=62,
        volume_ratio=1.2,
        fund_5d_billion=1.8,
    )

    assert result.avoid_reasons == []
    assert result.strategies[0].strategy_type == "main_wave_confirm"
    assert result.strategies[0].entry_price_hint == "≈10.00"


def test_next_day_plan_adds_main_wave_confirm_for_high_confidence_near_ma5():
    result = next_day_plan_engine.generate(
        code="000003",
        name="主升贴线股",
        bull_level="A",
        bull_score=91,
        price=10.2,
        change_pct=2.0,
        ma5=10.0,
        ma10=9.7,
        ma20=9.3,
        high_20d=12.0,
        high_60d=13.0,
        boll_upper=12.8,
        rsi14=66,
        volume_ratio=1.2,
        turnover=3.5,
        fund_5d_billion=2.2,
        macd_signal="golden_cross",
    )

    assert result.avoid_reasons == []
    assert result.strategies[0].strategy_type == "main_wave_confirm"
    assert result.strategies[0].confidence == "高"
    assert result.strategies[0].position_ratio == "1/2仓"
    assert "主升确认" in result.strategies[0].strategy_label
    assert not any(s.strategy_type == "trend" for s in result.strategies)


def test_next_day_plan_main_wave_confirm_requires_momentum_confirmation():
    result = next_day_plan_engine.generate(
        code="000004",
        name="弱动能贴线股",
        bull_level="A",
        bull_score=84,
        price=10.2,
        change_pct=1.0,
        ma5=10.0,
        ma10=9.7,
        ma20=9.3,
        high_20d=12.0,
        high_60d=13.0,
        boll_upper=12.8,
        rsi14=62,
        volume_ratio=0.9,
        turnover=1.2,
        fund_5d_billion=0.0,
    )

    assert result.avoid_reasons == []
    assert not any(s.strategy_type == "main_wave_confirm" for s in result.strategies)
    assert result.strategies[0].strategy_type == "trend"


def test_next_day_plan_adds_strong_get_stronger_for_leader_momentum():
    result = next_day_plan_engine.generate(
        code="000005",
        name="恒强承接股",
        bull_level="A",
        bull_score=95,
        price=12.0,
        change_pct=3.2,
        ma5=11.5,
        ma10=11.0,
        ma20=10.4,
        high_20d=12.0,
        high_60d=13.5,
        boll_upper=13.2,
        rsi14=68,
        volume_ratio=1.8,
        turnover=6.0,
        fund_5d_billion=2.8,
        price_volume_relation="放量上涨",
        sector_resonance="强共振",
    )

    assert result.avoid_reasons == []
    assert result.strategies[0].strategy_type == "strong_get_stronger"
    assert "强势回踩承接" in result.strategies[0].strategy_label
    assert "首次缩量回踩" in result.strategies[0].entry_condition
    assert "竞价确认" not in result.strategies[0].entry_price_hint
    assert result.strategies[0].position_ratio == "1/4仓"


def test_next_day_plan_strong_get_stronger_blocks_direct_buy_overextension():
    result = next_day_plan_engine.generate(
        code="000007",
        name="追高风险股",
        bull_level="A",
        bull_score=95,
        price=12.0,
        change_pct=7.2,
        ma5=11.1,
        ma10=10.6,
        ma20=10.0,
        high_20d=12.1,
        high_60d=13.5,
        boll_upper=13.2,
        rsi14=76,
        volume_ratio=1.8,
        turnover=6.0,
        fund_5d_billion=2.8,
        price_volume_relation="放量上涨",
        sector_resonance="强共振",
    )

    assert any("明日不追高" in reason for reason in result.avoid_reasons)
    assert not any(s.strategy_type == "strong_get_stronger" for s in result.strategies)


def test_next_day_plan_strong_get_stronger_blocks_extreme_overheat():
    result = next_day_plan_engine.generate(
        code="000006",
        name="极热强势股",
        bull_level="A",
        bull_score=96,
        price=12.0,
        change_pct=9.8,
        ma5=10.9,
        ma10=10.4,
        ma20=9.8,
        high_20d=12.1,
        high_60d=13.5,
        boll_upper=12.2,
        rsi14=88,
        volume_ratio=1.9,
        turnover=7.0,
        fund_5d_billion=3.0,
        price_volume_relation="放量上涨",
    )

    assert any("极度超买" in reason for reason in result.avoid_reasons)
    assert not any(s.strategy_type == "strong_get_stronger" for s in result.strategies)
