"""Evaluate only declared research CronTrigger calls, never import/start scheduler."""
import ast
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

METHODS = {"_review_snapshot_automation", "_market_regime_automation", "_gpt_review_report",
           "_news_raw_refresh", "_news_ai_refresh", "_news_weekend_refresh",
           "_publish_paper_research", "_freeze_regular_close_research",
           "_collect_after_hours_research", "_expire_after_hours_intents"}


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_guard():
    yield


def declarations():
    tree = ast.parse((Path(__file__).parents[1]/"app/data/scheduler.py").read_text())
    rows = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_job" and len(node.args)>1
                and isinstance(node.args[0], ast.Attribute) and node.args[0].attr in METHODS):
            trigger = node.args[1]
            if isinstance(trigger, ast.Call) and isinstance(trigger.func, ast.Name) and trigger.func.id=="CronTrigger":
                rows.append((node.args[0].attr, trigger))
    return rows


@pytest.mark.parametrize("method,expression", declarations())
def test_research_triggers_ignore_host_timezone(method, expression, monkeypatch):
    monkeypatch.setattr("apscheduler.triggers.cron.get_localzone", lambda: timezone.utc)
    variables = {"CronTrigger": CronTrigger, "review_hour":8,"review_minute":45,
                 "regime_hour":20,"regime_minute":10, "gpt_hour":20,"gpt_minute":40,
                 "hour":20,"minute":45}
    trigger = eval(compile(ast.Expression(expression), "<declared research trigger>", "eval"),
                   {"__builtins__":{}}, variables)
    assert str(trigger.timezone)=="Asia/Shanghai"
    next_time = trigger.get_next_fire_time(None, datetime(2026,10,8,tzinfo=timezone.utc))
    assert next_time.utcoffset().total_seconds()==8*3600
    assert next_time.astimezone(ZoneInfo("Asia/Shanghai")).minute==next_time.minute


def test_all_expected_research_producers_are_checked():
    rows = declarations()
    assert {name for name,_ in rows}==METHODS
    assert len(rows)==11  # loop declaration covers two after-hours collector slots
