import asyncio
from argparse import Namespace
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from scripts import capture_source_responses as cli
from app.data.source_capture import ResponseCapture
from app.data.sources.eastmoney_source import EastMoneySource
from app.data.sources.ths_kline_source import ThsKlineSource
from app.promotion.modeling.daily_materials import MaterialArchive, decode


@pytest.fixture
def args(tmp_path,monkeypatch):
    monkeypatch.setattr(cli,"ROOT",tmp_path)
    (tmp_path/"outputs").mkdir()
    return Namespace(source="ths_kline",codes=["000001"],archive_root=tmp_path/"outputs/capture")


@pytest.mark.parametrize("change", [
    {"codes":[]},{"codes":["1"]},{"codes":["０００００１"]},{"codes":["000001"]*2},
    {"codes":["000001","000002","000003","000004"]},
    {"source":"eastmoney_fund","codes":["000001"]},{"source":"other"},
])
def test_invalid_request_before_any_network_or_files(args,change):
    vars(args).update(change)
    with pytest.raises(ValueError): cli.validate(args)
    assert not args.archive_root.exists()


def test_existing_outside_traversal_and_symlink_rejected(args,tmp_path):
    outside=tmp_path/"escape"
    for path in [tmp_path/"outputs",outside,tmp_path/"outputs/../escape"]:
        args.archive_root=path
        with pytest.raises(ValueError):cli.validate(args)
    outside.mkdir()
    link=tmp_path/"outputs/link";link.symlink_to(outside,target_is_directory=True)
    args.archive_root=link/"capture"
    with pytest.raises(ValueError):cli.validate(args)


@pytest.mark.asyncio
async def test_explicit_capture_uses_source_worker_and_never_db(args,monkeypatch):
    from sqlalchemy.engine import Engine
    monkeypatch.setattr(Engine,"connect",lambda *_a,**_k:pytest.fail("business DB forbidden"))
    async def fetch(source,code,suffix,max_attempts):
        assert code=="000001" and suffix==f"{datetime.now().year}.js" and max_attempts==1
        source._response_capture.observe(source="ths_kline",request_key=(code,suffix,1),
            raw=b"raw-rejected-by-formal-policy",received_at=datetime.now())
        return []
    monkeypatch.setattr(ThsKlineSource,"_fetch_kline",fetch)
    real_thread=asyncio.to_thread
    worker_calls=[]
    async def thread(fn,*a,**kw):
        assert fn.__self__.__class__ in (ResponseCapture, MaterialArchive)
        assert all(not hasattr(v,"execute") for v in a)
        worker_calls.append(fn.__self__.__class__.__name__)
        return await real_thread(fn,*a,**kw)
    monkeypatch.setattr(cli.asyncio,"to_thread",thread)
    result=await cli.run(args)
    assert result["formal_ready"] is False and result["scheduled"] is False
    assert result["business_db_opened"] is False and worker_calls==["ResponseCapture", "MaterialArchive"]
    assert result["response_count_retained"]==1
    coverage = result["probe_date_coverage"]
    assert coverage["requested_code_count"] == 1
    assert coverage["codes_without_unique_requested_date_row"] == ["000001"]
    assert coverage["requested_date_coverage_status"] == "partial_or_unavailable"
    persisted = decode(MaterialArchive(args.archive_root).read_bytes(result["probe_ref"]))
    assert persisted == {k: v for k, v in result.items() if k != "probe_ref"}
    assert not (args.archive_root/"ready").exists()
    report=decode(MaterialArchive(args.archive_root).read_bytes(result["report_ref"]))
    assert report["operation_status"]=="returned"
    with pytest.raises(ValueError):await cli.run(args)


@pytest.mark.asyncio
async def test_failed_original_source_sealed_as_empty_not_restored(args,monkeypatch):
    args.source="eastmoney_fund";args.codes=[]
    async def fail(*_a,**_kw):raise RuntimeError("secret-url-credential")
    monkeypatch.setattr(EastMoneySource,"get_individual_fund_flow",fail)
    result=await cli.run(args)
    assert result["error_type"]=="RuntimeError" and result["operation_status"]=="failed"
    assert result["status"]=="incomplete_or_empty" and result["response_count_retained"]==0
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cancel_stops_without_archive_thread(args,monkeypatch):
    async def cancel(*_a,**_kw):raise asyncio.CancelledError()
    monkeypatch.setattr(ThsKlineSource,"_fetch_kline",cancel)
    monkeypatch.setattr(cli.asyncio,"to_thread",lambda *_a,**_k:pytest.fail("no worker after cancellation"))
    with pytest.raises(asyncio.CancelledError):await cli.run(args)
    assert args.archive_root.is_dir() and list(args.archive_root.iterdir())==[]


@pytest.mark.asyncio
async def test_archive_failure_surfaces_not_reported_as_success(args,monkeypatch):
    async def empty(*_a,**_kw):return []
    def denied(*_a,**_kw):raise OSError("disk full")
    monkeypatch.setattr(ThsKlineSource,"_fetch_kline",empty)
    monkeypatch.setattr(ResponseCapture,"seal",denied)
    with pytest.raises(OSError):await cli.run(args)
    assert not (args.archive_root/"ready").exists()


@pytest.mark.parametrize("rows,count,invalid,latest", [
    (None, None, None, None), ([], 0, 0, None),
    ([{"trade_date": "2026-09-08"}], 0, 0, "2026-09-08"),
    ([{"trade_date": "2026-09-09"}], 1, 0, "2026-09-09"),
    ([{"trade_date": "2026-09-09"}]*2, 2, 0, "2026-09-09"),
    ([{"trade_date": "20260909"}, {"trade_date": True}, None], 0, 3, None),
    ([{"trade_date": "2026-02-30"}, {"trade_date": "2026-09-09"}], 1, 1, "2026-09-09"),
])
def test_date_diagnostic_never_hides_missing_duplicate_or_bad_rows(rows, count, invalid, latest):
    result = cli._parsed_date_diagnostic(rows, "2026-09-09")
    assert result["requested_date_row_count"] == count
    assert result["invalid_parsed_date_count"] == invalid
    assert result["latest_parsed_date"] == latest


@pytest.mark.asyncio
async def test_annual_probe_success_keeps_missing_today_code_in_persisted_denominator(args, monkeypatch):
    args.codes = ["600103", "000564", "600791"]
    requested = []
    async def fetch(source, code, suffix, max_attempts):
        requested.append((code, suffix, max_attempts))
        day = datetime.now().date().isoformat()
        rows = [{"code": code, "trade_date": day}] if code != "000564" else [{"code": code, "trade_date": "2000-01-01"}]
        source._response_capture.observe(source="ths_kline", request_key=(code, suffix, 1),
            raw=json.dumps(rows).encode(), received_at=datetime.now())
        return rows
    monkeypatch.setattr(ThsKlineSource, "_fetch_kline", fetch)
    monkeypatch.setattr(ThsKlineSource, "rate_limit", 0)
    result = await cli.run(args)
    assert len(requested) == 3 and all(r[2] == 1 for r in requested)
    assert result["response_count_retained"] == 3 and result["operation_status"] == "returned"
    coverage = result["probe_date_coverage"]
    assert coverage["requested_code_count"] == 3
    assert coverage["codes_without_unique_requested_date_row"] == ["000564"]
    assert coverage["requested_date_coverage_status"] == "partial_or_unavailable"
    assert all(p["retained_http_200_response_count"] == 1 for p in coverage["probes"])
    assert coverage["price_finality_and_basis_verified"] is False
    persisted = decode(MaterialArchive(args.archive_root).read_bytes(result["probe_ref"]))
    assert persisted["probe_date_coverage"] == coverage
    assert not (args.archive_root / "ready").exists()


@pytest.mark.asyncio
async def test_mid_probe_failure_retains_not_started_codes_without_credentials(args, monkeypatch):
    args.codes = ["600103", "000564", "600791"]
    async def fetch(source, code, suffix, max_attempts):
        if code == "000564":
            raise RuntimeError("secret-cookie-url")
        return [{"trade_date": datetime.now().date().isoformat()}]
    monkeypatch.setattr(ThsKlineSource, "_fetch_kline", fetch)
    monkeypatch.setattr(ThsKlineSource, "rate_limit", 0)
    result = await cli.run(args)
    assert result["operation_status"] == "failed" and result["error_type"] == "RuntimeError"
    coverage = result["probe_date_coverage"]
    assert [p["request_status"] for p in coverage["probes"]] == ["returned", "failed", "not_started"]
    assert coverage["requested_code_count"] == 3
    assert coverage["codes_without_unique_requested_date_row"] == ["000564", "600791"]
    assert "secret" not in json.dumps(result)
    assert result["formal_ready"] is False


@pytest.mark.asyncio
async def test_summary_archive_failure_is_not_success_or_formal_ready(args, monkeypatch):
    async def empty(*_a, **_kw):
        return []
    monkeypatch.setattr(ThsKlineSource, "_fetch_kline", empty)
    monkeypatch.setattr(ThsKlineSource, "rate_limit", 0)
    original = MaterialArchive.put
    def reject_summary(self, raw, **kw):
        if b"probe_date_coverage" in raw:
            raise OSError("disk full in probe index")
        return original(self, raw, **kw)
    monkeypatch.setattr(MaterialArchive, "put", reject_summary)
    with pytest.raises(OSError):
        await cli.run(args)
    assert not (args.archive_root / "ready").exists()
    documents = [decode(p.read_bytes()) for p in (args.archive_root / "objects").iterdir()]
    assert not any("probe_date_coverage" in d for d in documents)
