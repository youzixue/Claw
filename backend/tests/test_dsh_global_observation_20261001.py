"""Adapter contract only; mock dependency, no app startup or network."""
import json
import sys
from types import SimpleNamespace

import pandas as pd
import pytest
from app.news.sources.global_market import GlobalMarketSource


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_guard():
    yield


@pytest.mark.asyncio
async def test_fixed_export_and_finite_observations_not_fake_us_futures(monkeypatch):
    calls = []
    def spot():
        calls.append("spot")
        return pd.DataFrame([
            {"代码": "NDX", "名称": "纳指", "涨跌幅": 0, "最新行情时间": "2026-10-01 04:00:00"},
            {"代码": "SPX", "名称": "标普", "涨跌幅": -0.5},
            {"名称": "missing", "涨跌幅": None},
            {"名称": "bad", "涨跌幅": "bad"},
            {"名称": "nan", "涨跌幅": float("nan")},
            {"名称": "infinity", "涨跌幅": float("inf")},
            {"名称": "", "涨跌幅": 3},
            {"名称": "small", "涨跌幅": 0.5},
        ])
    def forbidden():
        raise AssertionError("obsolete API or domestic futures was called")
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(
        index_global_spot_em=spot, index_global_em=forbidden, futures_main_sina=forbidden))
    items = await GlobalMarketSource().fetch_latest()
    assert calls == ["spot"]
    assert len(items) == 2
    assert {row.category for row in items} == {"global"}
    data = json.loads(items[0].content)
    assert data["session"] == "unknown"
    assert data["us_index_futures_available"] is False
    assert data["quote_clock_certified"] is False
    assert data["kind"] == "index_quote_observation"
    assert {json.loads(row.content)["change_pct"] for row in items} == {0, -0.5}
    assert data["price"] is None and data["previous_close"] is None
    assert len(data["response_row_hash"]) == 64
    assert data["source_timezone"] == "index_local_unknown"
    assert items[0].publish_time.tzinfo is None
    assert len(await GlobalMarketSource().fetch_latest(limit=1)) == 1
    assert await GlobalMarketSource().fetch_latest(limit=0) == []
    with pytest.raises(ValueError):
        await GlobalMarketSource().fetch_latest(limit=-1)


@pytest.mark.asyncio
async def test_target_beyond_top20_missing_values_and_duplicate_conflict(monkeypatch):
    rows = [{"代码": f"other{i}", "名称": "非目标", "涨跌幅": 3} for i in range(22)]
    rows += [{"代码": "DJIA", "名称": "道指", "最新价": float("nan"), "涨跌幅": float("inf")},
             {"代码": "SPX", "名称": "标普", "最新价": 5000, "涨跌幅": 0},
             {"代码": "SPX", "名称": "标普", "最新价": 5001, "涨跌幅": 0}]
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_global_spot_em=lambda: pd.DataFrame(rows)))
    source = GlobalMarketSource()
    items = await source.fetch_latest()
    assert len(items) == 1
    data = json.loads(items[0].content)
    assert data["code"] == "DJIA" and data["price"] is None and data["change_pct"] is None
    assert source.last_observation["conflicting_codes"] == ["SPX"]


@pytest.mark.asyncio
async def test_typed_validator_rejects_self_hashed_wrong_types_and_fake_futures(monkeypatch):
    from app.news.sources.global_market import validate_index_observation
    import hashlib
    monkeypatch.setitem(sys.modules,"akshare",SimpleNamespace(index_global_spot_em=lambda: pd.DataFrame([
        {"代码":"SPX","名称":"标普500","最新价":5000,"昨收价":5000,"涨跌幅":0} ])))
    item=(await GlobalMarketSource().fetch_latest())[0]
    data=json.loads(item.content)
    assert validate_index_observation(data) is True
    for key,value in (("price",True),("change_pct","0"),("us_index_futures_available",True),
                      ("parser_version","unknown"),("session_date","2026-10-07"),("missing_fields",["price"])):
        changed={**data,key:value}
        fields={key:changed[key] for key in ("code","name","price","previous_close","change_pct","raw_source_time")}
        changed["response_row_hash"]=hashlib.sha256(json.dumps(fields,ensure_ascii=False,sort_keys=True,
                                   separators=(",",":")).encode()).hexdigest()
        with pytest.raises(ValueError):
            validate_index_observation(changed)


@pytest.mark.asyncio
async def test_provider_failure_does_not_substitute_zero_or_domestic_futures(monkeypatch):
    def fails():
        raise OSError("offline")
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_global_spot_em=fails))
    assert await GlobalMarketSource().fetch_latest() == []
    assert await GlobalMarketSource().fetch_by_code("600001") == []
