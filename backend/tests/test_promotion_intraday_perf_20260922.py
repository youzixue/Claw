"""Promotion overlapping-window reuse is numerical equivalence, not rule tuning."""
import asyncio
import random
from datetime import date, timedelta

import pytest
from app.api.v1 import promotion as p


class Rows:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def partitions(self, size):
        assert size == 2048
        async def batches():
            for i in range(0, len(self.data), size):
                yield self.data[i:i + size]
        return batches()

    async def close(self):
        self.closed = True


def bars(seed, length):
    rng = random.Random(seed)
    return [
        dict(trade_date=date(2025, 1, 1) + timedelta(days=i),
             open=10 + rng.random(), close=10 + rng.random(),
             high=10 + rng.random() * 5, low=6 + rng.random() * 4,
             volume=rng.random() * 1e8, turnover=rng.random()*10, change_pct=0)
        for i in range(length)
    ]


def scalar_resolve(rows, latest):
    best = None
    for config in p.PLATFORM_CYCLE_CONFIGS:
        minimum = p._safe_int(config.get("min_days"))
        maximum = min(p._safe_int(config.get("max_days")), len(rows))
        for days in range(maximum, minimum - 1, -1):
            metrics = p._evaluate_platform_cycle_window(rows[-days:], latest, config)
            if not metrics:
                continue
            if metrics["qualifies"]:
                return metrics
            if best is None or p._safe_float(metrics["fit_score"]) > p._safe_float(best["fit_score"]):
                best = metrics
    if best is not None:
        return best
    config = p.PLATFORM_CYCLE_CONFIGS[-1]
    count = min(len(rows), p._safe_int(config.get("min_days"), 8))
    return p._evaluate_platform_cycle_window(rows[-count:], latest, config)


@pytest.mark.parametrize("length", [0, 1, 7, 8, 15, 16, 30, 31, 60, 61, 260])
@pytest.mark.parametrize("seed", range(5))
def test_resolver_exact_scalar_parity(length, seed):
    rows = bars(seed, length)
    expected = scalar_resolve(rows, 10)
    actual = p._resolve_platform_cycle_context(rows, 10)
    if expected is not None:
        assert actual == expected
    else:
        assert actual["qualifies"] is False
        assert actual["platform_cycle_days"] == min(length, 8)


@pytest.mark.parametrize("value", [None, "", "bad", "2.3", -1, 0, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["high", "low", "volume"])
def test_dirty_numeric_values_preserve_fallbacks(field, value):
    rows = bars(9, 60)
    for i in (0, 20, 57, 59):
        rows[i][field] = value
    expected = scalar_resolve(rows, 10)
    actual = p._resolve_platform_cycle_context(rows, 10)
    if expected is None:
        assert not actual["qualifies"]
    else:
        assert actual == expected


def test_overlapping_windows_do_not_renormalize_every_bar(monkeypatch):
    rows = bars(3, 260)
    calls = 0
    original = p._safe_float
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(p, "_safe_float", counted)
    p._resolve_platform_cycle_context(rows, 10)
    assert calls < 2500, f"overlapping windows repeatedly normalize OHLCV: {calls}"


@pytest.mark.asyncio
async def test_kline_confirmation_yields_without_changing_code_order(monkeypatch):
    data = [
        (f"60{i:04d}", date(2026, 9, 21), 10, 10, 11, 9, 100, 1, 0, "test")
        for i in range(49)
    ]
    class DB:
        async def stream(self, statement):
            return Rows(data)
    ticks = 0
    seen = []
    def build(rows):
        seen.append(ticks)
        return {"count": len(rows), "last": rows[-1]}
    monkeypatch.setattr(p, "_build_first_board_kline_confirmation", build)
    stop = False
    async def observer():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(observer())
    try:
        result = await p._load_first_board_kline_context(
            DB(), [r[0] for r in data], date(2026, 9, 21),
            as_of_date=date(2026, 9, 21), session_name="morning")
    finally:
        stop = True
        await task
    assert list(result) == [r[0] for r in data]
    assert len(set(seen)) > 1, "all stock confirmation work blocks the event loop"
    assert all(v["last"]["is_provisional"] is False for v in result.values())


@pytest.mark.asyncio
async def test_kline_confirmation_cancellation_is_not_swallowed(monkeypatch):
    class DB:
        async def stream(self, statement):
            return Rows([
                (str(i), date(2026, 9, 21), 10, 10, 11, 9, 100, 1, 0, "test")
                for i in range(35)])
    calls = 0
    def build(rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError()
        return {}
    monkeypatch.setattr(p, "_build_first_board_kline_confirmation", build)
    with pytest.raises(asyncio.CancelledError):
        await p._load_first_board_kline_context(DB(), ["x"], date(2026,9,21),
                                                session_name="morning")
    assert calls == 2


@pytest.mark.parametrize("latest", [0, -1, 10])
def test_older_bars_outside_configured_windows_are_not_normalized(latest):
    rows = bars(11, 260)
    rows[0]["high"] = 10 ** 1000  # float conversion would raise OverflowError.
    expected = scalar_resolve(rows, latest)
    actual = p._resolve_platform_cycle_context(rows, latest)
    if expected is None:
        assert not actual["qualifies"]
    else:
        assert actual == expected


def test_numeric_reuse_does_not_mutate_bar_dicts():
    rows = bars(7, 60)
    before = [dict(row) for row in rows]
    p._resolve_platform_cycle_context(rows, 10)
    assert rows == before


@pytest.mark.asyncio
async def test_cancellation_delivered_between_bounded_confirmation_groups(monkeypatch):
    class DB:
        async def stream(self, statement):
            return Rows([
                (str(i), date(2026, 9, 21), 10, 10, 11, 9, 100, 1, 0, "test")
                for i in range(100)])
    calls = 0
    def build(rows):
        nonlocal calls
        calls += 1
        if calls == 1:
            task = asyncio.current_task()
            asyncio.get_running_loop().call_soon(task.cancel)
        return {}
    monkeypatch.setattr(p, "_build_first_board_kline_confirmation", build)
    with pytest.raises(asyncio.CancelledError):
        await p._load_first_board_kline_context(
            DB(), ["x"], date(2026, 9, 21), session_name="morning")
    assert calls <= 16


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["read", "cancel", "close_only"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_stream_cleanup_preserves_primary_failure(kind, close_fails):
    class ReadFailure(RuntimeError):
        pass
    class CloseFailure(RuntimeError):
        pass
    primary = asyncio.CancelledError() if kind == "cancel" else ReadFailure("read failed")
    class BrokenRows(Rows):
        def partitions(self, size):
            async def batches():
                if kind != "close_only":
                    raise primary
                yield []
            return batches()
        async def close(self):
            self.closed = True
            if close_fails:
                raise CloseFailure("close failed")
    result = BrokenRows([])
    class DB:
        async def stream(self, statement):
            assert statement.get_execution_options()["yield_per"] == 2048
            return result
    if kind == "close_only" and not close_fails:
        assert await p._load_first_board_kline_context(DB(), ["x"], date(2026,9,21)) == {}
    else:
        expected = CloseFailure if kind == "close_only" else type(primary)
        with pytest.raises(expected) as caught:
            await p._load_first_board_kline_context(DB(), ["x"], date(2026,9,21))
        if kind != "close_only":
            assert caught.value is primary
    assert result.closed


@pytest.mark.asyncio
async def test_stream_close_precedes_confirmation_and_owner_untouched(monkeypatch):
    result = Rows([("600001", date(2026,9,21),10,10,11,9,100,1,0,"spot_fallback")])
    class DB:
        async def stream(self, statement):
            return result
        async def commit(self):
            pytest.fail("loader cannot commit owner transaction")
        async def rollback(self):
            pytest.fail("loader cannot roll back owner transaction")
    def confirm(rows):
        assert result.closed
        assert rows[0]["is_provisional"] is True
        return {"fixture": True}
    monkeypatch.setattr(p, "_build_first_board_kline_confirmation", confirm)
    assert await p._load_first_board_kline_context(
        DB(), ["600001"], date(2026,9,21), as_of_date=date(2026,9,21),
        session_name="morning") == {"600001": {"fixture": True}}
