"""New-computer chart bootstrap: bounded files, no historical projection writes."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data.kline_observations import (
    load_kline_download, save_kline_download, write_download_json,
)
from scripts.backfill_stock_kline_codes import download_history, parse_args

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
START, END = date(2018, 1, 1), date(2026, 9, 30)


def bar(code="000001", day="2026-09-29", **changes):
    return {
        "code": code, "trade_date": day, "open": 10.0, "high": 11.0,
        "low": 9.0, "close": 10.5, "volume": 1000, "amount": 10000.0,
        "turnover": None, "change_pct": None, "prev_close": None,
        "source": "ths", **changes,
    }


def save(root, code="000001", records=None, **kwargs):
    return save_kline_download(
        root, code, records or [bar(code)], start=START, end=END,
        downloaded_at=NOW, coverage={"unavailable_years": [], "certified": False}, **kwargs,
    )


def args(codes=None, **changes):
    return argparse.Namespace(
        download=True, all=False, status=False, refresh=False, limit=None,
        start_date=START, end_date=END, codes=codes or ["000001"], **changes,
    )


def source_for(rows):
    source = argparse.Namespace(rate_limit=0)
    async def collect(code, *, coverage):
        coverage.update(year_rows={"2026": len(rows)}, unavailable_years=[], certified=False)
        return rows
    source.collect_init = AsyncMock(side_effect=collect)
    return source


def test_save_load_preserves_unknown_and_zero_and_versions(tmp_path):
    first = save(tmp_path, records=[bar(volume=0, amount=0)])
    snapshot = load_kline_download(tmp_path, "000001")
    assert snapshot["klines"][0]["volume"] == 0
    assert snapshot["klines"][0]["prev_close"] is None
    assert snapshot["historical_pit_eligible"] is False
    assert snapshot["scope"] == "chart_only"
    second = save(tmp_path, records=[bar(close=10.8)])
    assert first["sha256"] != second["sha256"]
    assert (tmp_path / "000001" / (first["sha256"] + ".json")).is_file()
    assert load_kline_download(tmp_path, "000001")["klines"][0]["close"] == 10.8


@pytest.mark.parametrize("changes", [
    {"close": float("nan")}, {"volume": -1}, {"source": "tencent_close"},
    {"code": "000002"}, {"trade_date": "2027-01-01"}, {"high": 8},
])
def test_invalid_new_download_cannot_replace_last_success(tmp_path, changes):
    before = save(tmp_path)
    pointer = (tmp_path / "000001/latest.json").read_bytes()
    with pytest.raises(ValueError):
        save(tmp_path, records=[{**bar(), **changes}])
    assert (tmp_path / "000001/latest.json").read_bytes() == pointer
    assert load_kline_download(tmp_path, "000001")["klines"][0]["close"] == 10.5


@pytest.mark.parametrize("code", ["../abc", "123", "１２３４５６", "000001/"])
def test_code_path_traversal_rejected(tmp_path, code):
    with pytest.raises(ValueError):
        load_kline_download(tmp_path, code)


def test_duplicate_dates_rejected(tmp_path):
    with pytest.raises(ValueError):
        save(tmp_path, records=[bar(), bar()])


def test_corrupt_hash_rejected_without_fallback(tmp_path):
    saved = save(tmp_path)
    path = tmp_path / "000001" / (saved["sha256"] + ".json")
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="不可用"):
        load_kline_download(tmp_path, "000001")


def test_unavailable_file_and_invalid_pointer(tmp_path):
    assert load_kline_download(tmp_path, "000001") is None
    write_download_json(tmp_path / "000001/latest.json", {"sha256": "../secret"})
    with pytest.raises(ValueError, match="不可用"):
        load_kline_download(tmp_path, "000001")


@pytest.mark.parametrize("argv", [
    ["--download"], ["--download", "--all", "000001"],
    ["--download", "000001", "--apply"],
    ["--download", "000001", "--database", "claw.db"],
    ["--download", "--all", "--limit", "0"], ["--download", "x00001"],
    ["--all", "--database", "claw.db"], ["000001"],
])
def test_invalid_cli_combinations_rejected_before_access(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_cli_backward_compatibility_and_status():
    old = parse_args(["000001", "--database", "fixture.db", "--apply"])
    assert old.apply and not old.download
    assert parse_args(["--download", "--status"]).status
    assert parse_args(["--download", "--all"]).all


@pytest.mark.asyncio
async def test_download_and_resume_never_open_database(tmp_path, monkeypatch):
    from app.db.session import engine
    monkeypatch.setattr(type(engine), "connect", lambda *a, **k: pytest.fail("database connect"))
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: NOW)
    source = source_for([bar(), bar(day="2026-09-30")])
    assert await download_history(args(), source=source, root=tmp_path, now=NOW) == 0
    source.collect_init.assert_awaited_once()
    source.collect_init.reset_mock()
    assert await download_history(args(), source=source, root=tmp_path, now=NOW) == 0
    source.collect_init.assert_not_awaited()
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert len(list(tmp_path.glob("progress-*.json"))) == 2
    assert progress["requested_codes"] == ["000001"]
    assert progress["counts"]["skipped_existing"] == 1
    assert progress["unprocessed_count"] == 0
    assert progress["coverage_certified"] is False


@pytest.mark.asyncio
async def test_real_naive_shanghai_source_clock_is_zoned_in_download_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: NOW.replace(tzinfo=None))
    await download_history(args(), source=source_for([bar()]), root=tmp_path)
    snapshot = load_kline_download(tmp_path, "000001")
    assert snapshot["downloaded_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_empty_failure_and_partial_have_explicit_denominators(tmp_path, monkeypatch):
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: NOW)
    source = argparse.Namespace(rate_limit=0)
    async def collect(code, *, coverage):
        if code == "000001":
            return []
        if code == "000002":
            raise RuntimeError("SECRET_WEBHOOK_MUST_NOT_BE_LOGGED")
        coverage.update(unavailable_years=[2020, 2027], certified=False)
        return [bar(code)]
    source.collect_init = AsyncMock(side_effect=collect)
    assert await download_history(args(["000001", "000002", "000003"]),
                                  source=source, root=tmp_path, now=NOW) == 2
    text = (tmp_path / "progress.json").read_text()
    progress = json.loads(text)
    assert progress["counts"] == {
        "downloaded": 0, "skipped_existing": 0, "partial": 1, "empty": 1, "failed": 1,
    }
    assert progress["requested_count"] == 3
    assert "SECRET_WEBHOOK" not in text
    assert progress["stocks"]["000003"]["coverage"]["unavailable_years"] == [2020]
    source.collect_init.reset_mock()
    await download_history(args(["000003"]), source=source, root=tmp_path, now=NOW)
    source.collect_init.assert_awaited_once()  # partial files aren't called complete


@pytest.mark.asyncio
async def test_refresh_failure_keeps_previous_snapshot(tmp_path, monkeypatch):
    save(tmp_path)
    before = (tmp_path / "000001/latest.json").read_bytes()
    source = source_for([])
    request = args()
    request.refresh = True
    assert await download_history(request, source=source, root=tmp_path, now=NOW) == 2
    assert (tmp_path / "000001/latest.json").read_bytes() == before


@pytest.mark.asyncio
async def test_all_uses_cached_current_universe_and_limit(tmp_path, monkeypatch):
    from app.data.sources.akshare_source import AkShareSource
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: NOW)
    get_list = AsyncMock(return_value=pd.DataFrame({
        "code": ["600519", "000001"], "name": ["x", "y"],
    }))
    monkeypatch.setattr(AkShareSource, "get_stock_list", get_list)
    request = args()
    request.all, request.codes, request.limit = True, [], 1
    await download_history(request, source=source_for([bar()]), root=tmp_path, now=NOW)
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["requested_count"] == 1 and progress["universe_count"] == 2
    assert progress["limited"] is True
    get_list.assert_awaited_once()
    get_list.reset_mock()
    await download_history(request, source=source_for([bar()]), root=tmp_path, now=NOW)
    get_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_future_request_and_invalid_universe_do_not_create_files(tmp_path, monkeypatch):
    request = args()
    request.end_date = date(2027, 1, 1)
    source = source_for([bar()])
    with pytest.raises(ValueError, match="日期"):
        await download_history(request, source=source, root=tmp_path / "future", now=NOW)
    source.collect_init.assert_not_awaited()
    assert not (tmp_path / "future").exists()
    from app.data.sources.akshare_source import AkShareSource
    monkeypatch.setattr(AkShareSource, "get_stock_list", AsyncMock(return_value=pd.DataFrame({
        "code": ["bad"], "name": ["unknown"],
    })))
    request = args()
    request.all, request.codes = True, []
    with pytest.raises(ValueError, match="代码"):
        await download_history(request, source=source, root=tmp_path / "invalid", now=NOW)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.asyncio
async def test_interrupt_records_unprocessed_and_retains_committed_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: NOW)
    source = argparse.Namespace(rate_limit=0)
    async def collect(code, *, coverage):
        if code == "000002":
            raise asyncio.CancelledError()
        coverage.update(unavailable_years=[])
        return [bar(code)]
    import asyncio
    source.collect_init = AsyncMock(side_effect=collect)
    with pytest.raises(asyncio.CancelledError):
        await download_history(args(["000001", "000002"]), source=source, root=tmp_path, now=NOW)
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["state"] == "interrupted" and progress["unprocessed_count"] == 1
    assert load_kline_download(tmp_path, "000001") is not None


@pytest.mark.asyncio
async def test_explicit_chart_view_cannot_query_projection_or_change_default(tmp_path, monkeypatch):
    from app.api.v1.spot import kline_list
    from app.config.settings import settings
    save(tmp_path)
    monkeypatch.setattr(settings, "KLINE_DOWNLOAD_DIR", tmp_path)
    db = argparse.Namespace(execute=AsyncMock(side_effect=AssertionError("projection query")))
    response = await kline_list("000001", limit=800, db=db, view="downloaded")
    assert response["count"] == 1 and response["klines"][0]["close"] == 10.5
    assert response["historical_pit_eligible"] is False
    assert response["downloaded_at"] == NOW.isoformat()
    db.execute.assert_not_awaited()
    missing = await kline_list("000002", limit=800, db=db, view="downloaded")
    assert missing["count"] == 0
    with pytest.raises(AssertionError, match="projection query"):
        await kline_list("000001", limit=800, db=db, view="projection")


@pytest.mark.asyncio
async def test_corrupt_download_api_reports_unavailable(tmp_path, monkeypatch):
    from app.api.v1.spot import kline_list
    from app.config.settings import settings
    from fastapi import HTTPException
    save(tmp_path)
    write_download_json(tmp_path / "000001/latest.json", {})
    monkeypatch.setattr(settings, "KLINE_DOWNLOAD_DIR", tmp_path)
    db = argparse.Namespace(execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await kline_list("000001", limit=800, db=db, view="downloaded")
    assert error.value.status_code == 503
    db.execute.assert_not_awaited()
