"""Caller-supplied per-stock research context is not market/PIT evidence."""
import ast
import asyncio
from dataclasses import asdict
from datetime import date, datetime
import inspect
import json
import textwrap

import numpy as np
import pandas as pd
import pytest

from app.factors.base import FactorEngine, FactorRegistry
from app.factors.computation_evidence import engine_descriptor
from test_factor_market_inputs_20260914 import db, seed

DAY = date(2026, 9, 14)
A, B, C = "600001", "000002", "600003"


def frames(*codes):
    return {code: pd.DataFrame() for code in codes}


@pytest.mark.asyncio
async def test_cross_section_does_not_broadcast_legacy_kwargs():
    with pytest.raises(ValueError, match="broadcast"):
        await FactorEngine().compute_cross_section(DAY, frames(A, B), roe=15)


@pytest.mark.asyncio
async def test_per_stock_context_and_reverse_rank_keep_zero_and_missing():
    results = await FactorEngine().compute_cross_section(
        DAY, frames(A, B, C), contexts_by_code={
            A: {"roe": 15, "pe_percentile": 80},
            B: {"roe": 0, "pe_percentile": 0},
        })
    assert results[A]["roe"].value == 15
    assert results[B]["roe"].value == 0
    assert results[C]["roe"].value is None
    assert results[A]["roe"].rank == 1 and results[B]["roe"].rank == 2
    assert results[B]["pe_pct"].rank == 1 and results[A]["pe_pct"].rank == 2
    assert results[C]["pe_pct"].rank is None
    scope = results[B]["roe"].meta["cross_section_context"]
    assert scope["code"] == B and scope["trade_date"] == DAY.isoformat()
    assert scope["values"] == {"roe": 0}
    assert scope["point_in_time_verified"] is scope["trading_authority"] is False
    assert scope["promotion_eligible"] is scope["automatic_weight_update"] is False
    assert scope["source_basis"] == "caller_supplied_unverified"
    assert results[C]["roe"].meta["cross_section_context"]["missing_required_fields"] == ["roe"]
    json.dumps({c: {n: asdict(r) for n, r in rows.items()} for c, rows in results.items()}, allow_nan=False)


@pytest.mark.asyncio
async def test_sector_and_news_context_cannot_leak_between_stocks():
    result = await FactorEngine().compute_cross_section(
        DAY, frames(A, B), contexts_by_code={
            A: {"stock_main_net_inflow": 1, "sector_main_net_inflow": 1,
                "stock_bull_ratio": 1, "sector_bull_ratio": .5},
            B: {"stock_main_net_inflow": -1, "sector_main_net_inflow": -1,
                "stock_bull_ratio": 0},  # No borrowing A's sector ratio.
        })
    assert result[A]["sector_resonance"].value == 1
    assert result[B]["sector_resonance"].value == -1
    assert result[A]["bull_resonance"].value == 50
    assert result[B]["bull_resonance"].value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("contexts", [None, {}])
async def test_no_context_remains_unknown_not_recovery_or_zero(contexts):
    result = await FactorEngine().compute_cross_section(DAY, frames(A), contexts_by_code=contexts)
    for name in ("roe", "news_heat", "sentiment_cycle", "lifecycle_stage"):
        assert result[A][name].value is None
        assert result[A][name].rank is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf, True, np.bool_(False), "15", None, 10 ** 400])
async def test_bad_scalar_only_invalidates_its_factor_not_other_stock(bad):
    result = await FactorEngine().compute_cross_section(
        DAY, frames(A, B), contexts_by_code={A: {"roe": bad}, B: {"roe": 15}})
    assert result[A]["roe"].value is None and result[A]["roe"].rank is None
    assert result[B]["roe"].value == 15 and result[B]["roe"].rank == 1
    json.dumps(asdict(result[A]["roe"]), allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -2, np.int64(0), np.float64(0.5)])
async def test_finite_scalars_are_owned_and_keep_real_zero(value):
    result = await FactorEngine().compute_cross_section(
        DAY, frames(A), contexts_by_code={A: {"roe": value}})
    row = result[A]["roe"]
    assert row.value == float(value)
    assert row.meta["cross_section_context"]["values"] == {"roe": float(value)}
    json.dumps(asdict(row), allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("contexts", [
    [], "bad", {A: None}, {A: []}, {B: {"roe": 1}},
    {"６００００１": {"roe": 1}}, {A: {"roee": 1}}, {A: {1: 1}},
    {A: {"roe": [1]}}, {A: {"roe": {"value": 1}}},
    {A: {"roe": datetime(2026, 9, 14)}}, {A: {"roe": object()}},
])
async def test_ambiguous_context_rejected_before_any_calculator(monkeypatch, contexts):
    engine = FactorEngine()
    async def forbidden(*args, **kwargs):
        pytest.fail("bad context must fail before computation")
    monkeypatch.setattr(engine, "compute_single", forbidden)
    with pytest.raises(ValueError):
        await engine.compute_cross_section(DAY, frames(A), contexts_by_code=contexts)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["", "zero", "60001", "600001 ", "６００００１", 600001, None])
async def test_security_identity_is_ascii_six_digit(code):
    with pytest.raises(ValueError, match="code"):
        await FactorEngine().compute_cross_section(DAY, frames(code))


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [None, "2026-09-14", datetime(2026, 9, 14)])
async def test_trade_date_must_be_explicit_date(day):
    with pytest.raises(ValueError, match="date"):
        await FactorEngine().compute_cross_section(day, frames(A))


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], None, {A: None}, {A: []}])
async def test_stock_frame_mapping_is_explicit(data):
    with pytest.raises(ValueError):
        await FactorEngine().compute_cross_section(DAY, data)


@pytest.mark.asyncio
async def test_empty_cross_section_not_implicit_universe():
    assert await FactorEngine().compute_cross_section(DAY, {}) == {}
    with pytest.raises(ValueError):
        await FactorEngine().compute_cross_section(DAY, {}, contexts_by_code={A: {"roe": 1}})


@pytest.mark.asyncio
async def test_snapshot_all_stock_contexts_before_first_await(monkeypatch):
    engine = FactorEngine()
    contexts = {A: {"roe": 1}, B: {"roe": 2}}
    data = frames(A, B)
    original = engine.compute_single
    async def mutate(code, day, df, **kwargs):
        if code == A:
            await asyncio.sleep(0)
            contexts[B]["roe"] = 99
            contexts[A]["roe"] = 88
            contexts[C] = {"roe": 77}
            data[C] = pd.DataFrame()
        return await original(code, day, df, **kwargs)
    monkeypatch.setattr(engine, "compute_single", mutate)
    result = await engine.compute_cross_section(DAY, data, contexts_by_code=contexts)
    assert set(result) == {A, B}
    assert result[A]["roe"].value == 1 and result[B]["roe"].value == 2
    assert result[B]["roe"].meta["cross_section_context"]["values"] == {"roe": 2}


@pytest.mark.asyncio
async def test_equal_ranks_keep_original_stable_input_order():
    result = await FactorEngine().compute_cross_section(
        DAY, frames(B, A), contexts_by_code={A: {"roe": 0}, B: {"roe": 0}})
    assert result[B]["roe"].rank == 1 and result[A]["roe"].rank == 2


@pytest.mark.asyncio
async def test_explicit_market_context_is_not_implicitly_broadcast():
    result = await FactorEngine().compute_cross_section(
        DAY, frames(A, B, C), contexts_by_code={
            A: {"sentiment_cycle": "recovery"}, B: {"sentiment_cycle": "recovery"}})
    assert result[A]["sentiment_cycle"].value == result[B]["sentiment_cycle"].value
    assert result[C]["sentiment_cycle"].value is None


@pytest.mark.asyncio
async def test_conditional_inputs_are_declared_and_consumed_without_formula_change():
    kwargs = {"news_count_1h": 1, "news_count_24h": 2, "news_avg_importance": 5,
              "limit_up_time": "10:00:00", "seal_amount": 100_000_000, "break_count": 0,
              "margin_balance_change_avg5": 0}
    engine = FactorEngine()
    result = (await engine.compute_cross_section(DAY, frames(A), contexts_by_code={A: kwargs}))[A]
    for name in ("news_heat", "first_board_quality", "margin_balance_trend"):
        expected = FactorRegistry.get(name).calculate(pd.DataFrame(), **kwargs)
        assert result[name].value == expected.value and result[name].value is not None
    assert result["news_heat"].meta["cross_section_context"]["values"] == {
        "news_count_1h": 1, "news_count_24h": 2, "news_avg_importance": 5}
    # Unrelated context is NOT claimed as this factor's consumed inputs.
    assert "limit_up_time" not in result["news_heat"].meta["cross_section_context"]["values"]


@pytest.mark.parametrize("factor", list(FactorRegistry.all_factors().values()), ids=lambda f: f.factor_name)
def test_every_calculator_kwarg_is_declared_required_or_conditional(factor):
    tree = ast.parse(textwrap.dedent(inspect.getsource(factor.calculate)))
    consumed = {node.args[0].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "kwargs"
                and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant)}
    declared = set(factor.required_context) | set(factor.conditional_context)
    assert consumed == declared
    summary = FactorEngine().get_factor_summary()["factors"][factor.factor_name]["input_contract"]
    assert summary["conditional_context"] == list(factor.conditional_context)


def test_descriptor_owns_conditional_contract_and_detects_change(monkeypatch):
    engine = FactorEngine()
    before = engine_descriptor(engine)
    assert before["factors"]["news_heat"]["conditional_context"] == ["news_avg_importance"]
    factor = FactorRegistry.get("news_heat")
    monkeypatch.setattr(factor, "conditional_context", ["changed"])
    after = engine_descriptor(engine)
    assert before != after and before["factors"]["news_heat"]["conditional_context"] == ["news_avg_importance"]


@pytest.mark.asyncio
async def test_old_descriptor_format_remains_readable_without_relabelling(db):
    from app.factors.computation_evidence import (
        append_computation, read_computation, replay_computation, digest,
    )
    from test_factor_computation_evidence_20260914 import capture, arguments
    await seed(db)
    _, current = await capture(db)
    old = arguments(current)
    old["capture_id"] = "isolated-old-descriptor-format"
    for item in old["descriptor"]["factors"].values():
        item.pop("conditional_context")
    # Synthetic compatibility fixture, NOT reconstruction of historical production.
    await append_computation(db, **old)
    await db.commit()
    loaded = await read_computation(db, capture_id=old["capture_id"])
    before = json.dumps(loaded, sort_keys=True, allow_nan=False)
    assert loaded["descriptor"] == old["descriptor"]
    assert loaded["implementation_hash"] == digest(old["descriptor"])
    with pytest.raises(ValueError, match="implementation"):
        await replay_computation(loaded, FactorEngine())
    assert json.dumps(await read_computation(db, capture_id=old["capture_id"]),
                      sort_keys=True, allow_nan=False) == before


@pytest.mark.asyncio
async def test_stored_single_stock_path_does_not_import_unverified_cross_section_context(db):
    from app.factors.computation_evidence import read_computation, replay_computation
    from app.risk.factor_scheduler import FactorEvaluationScheduler
    await seed(db)
    # Explicit isolated calculation does not become a cache for another entry point.
    await FactorEngine().compute_cross_section(DAY, frames(A), contexts_by_code={A: {"roe": 99}})
    report = await FactorEvaluationScheduler().compute_and_store_factors(db, DAY, [A])
    capture = await read_computation(db, capture_id=report["computation_capture"]["capture_id"])
    assert capture["stocks"][A]["context"] == {}
    result = capture["stocks"][A]["results"]["roe"]
    assert result["value"] is None and result["rank"] is None
    assert "cross_section_context" not in result["meta"]
    assert (await replay_computation(capture, FactorEngine()))["matched"] is True

