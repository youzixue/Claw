"""The existing supervised heartbeat owns both independent notification loops."""
import ast
import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def loop_method(monkeypatch, buy, research):
    source = Path(__file__).resolve().parents[1] / "app/data/scheduler.py"
    tree = ast.parse(source.read_text())
    method = next(node for top in tree.body if isinstance(top, ast.ClassDef)
                  for node in top.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_paper_buy_point_push_loop")
    isolated = ModuleType("app.push.paper_buy_points")
    isolated.dispatch_buy_points = buy
    isolated.dispatch_c3_research = research
    monkeypatch.setitem(sys.modules, "app.push.paper_buy_points", isolated)
    original_sleep = asyncio.sleep

    async def quick_sleep(_seconds):
        await original_sleep(0)

    proxy = SimpleNamespace(
        sleep=quick_sleep, create_task=asyncio.create_task,
        gather=asyncio.gather, CancelledError=asyncio.CancelledError,
    )
    scope = {"asyncio": proxy, "settings": SimpleNamespace(PAPER_BUY_POINT_PUSH_INTERVAL_SEC=5),
             "logger": SimpleNamespace(error=lambda *args: None)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), scope)
    return scope["_paper_buy_point_push_loop"]


@pytest.mark.asyncio
async def test_slow_research_does_not_block_buy_and_parent_cancel_reaps_children(monkeypatch):
    research_started = asyncio.Event()
    enough_buy = asyncio.Event()
    ended = []
    calls = 0
    stuck = asyncio.Event()

    async def buy():
        nonlocal calls
        calls += 1
        if calls >= 5:
            enough_buy.set()
        await asyncio.sleep(0)

    async def research():
        research_started.set()
        try:
            await stuck.wait()
        finally:
            ended.append("research")

    method = loop_method(monkeypatch, buy, research)
    task = asyncio.create_task(method(SimpleNamespace(scheduler=SimpleNamespace(running=True))))
    try:
        await asyncio.wait_for(research_started.wait(), 1)
        await asyncio.wait_for(enough_buy.wait(), 1)
        assert not ended
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert ended == ["research"]
    assert calls >= 5


@pytest.mark.asyncio
async def test_research_exception_retries_without_killing_original_worker(monkeypatch):
    finished = asyncio.Event()
    owner = SimpleNamespace(scheduler=SimpleNamespace(running=True))
    calls = {"buy": 0, "research": 0}
    buy_continued = asyncio.Event()

    async def buy():
        calls["buy"] += 1
        if calls["buy"] >= 3:
            buy_continued.set()
        await asyncio.sleep(0)

    async def research():
        calls["research"] += 1
        if calls["research"] < 3:
            raise RuntimeError("isolated fake channel failure")
        # Independent loops need not advance in lockstep. Wait for the behavior
        # under test instead of stopping before the other loop gets its turn.
        await buy_continued.wait()
        owner.scheduler.running = False
        finished.set()

    method = loop_method(monkeypatch, buy, research)
    task = asyncio.create_task(method(owner))
    await asyncio.wait_for(finished.wait(), 1)
    await asyncio.wait_for(task, 1)
    assert calls["research"] == 3
    assert calls["buy"] >= 3


@pytest.mark.asyncio
async def test_stop_before_initial_wakeup_never_dispatches(monkeypatch):
    async def forbidden():
        raise AssertionError("stopped supervisor may not dispatch")
    method = loop_method(monkeypatch, forbidden, forbidden)
    await method(SimpleNamespace(scheduler=SimpleNamespace(running=False)))
