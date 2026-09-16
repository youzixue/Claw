"""HTTPX transport fixtures only; original source semantics stay authoritative."""
import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.data.source_capture import ResponseCapture, observe_response
from app.data.sources.eastmoney_source import EastMoneySource
from app.data.sources.ths_kline_source import ThsKlineSource
from app.promotion.modeling.daily_materials import MaterialArchive, decode


def capture_one(raw=b"not-json"):
    capture = ResponseCapture()
    capture.observe(source="eastmoney_fund", request_key=(1, 1), raw=raw, received_at=datetime.now())
    return capture


def seal(capture, root, status="returned", error=None):
    return capture.seal(root, operation_status=status, error_type=error)


def test_preserves_exact_body_and_two_clocks_without_approving_source(tmp_path):
    raw = b' { "data": {"total": 0, "diff": []} } \n'
    capture = capture_one(raw)
    observation = capture.responses[0]
    with pytest.raises(FrozenInstanceError):
        observation.raw = b"replacement"
    result = seal(capture, tmp_path)
    archive = MaterialArchive(tmp_path)
    assert decode(archive.read_bytes(result["report_ref"])) == result["report"]
    report = result["report"]
    response = report["responses"][0]
    material = archive.read(response["envelope_ref"])
    assert archive.read_bytes(material["manifest"]["raw_ref"]) == raw
    assert material["status"] == "blocked"
    assert material["reasons"] == ["observed_now_not_forward_evidence", "source_protocol_unreviewed"]
    assert report["formal_ready"] is False and report["historical_first_known_verified"] is False
    assert report["source_published_at"] is None and report["universe_complete"] is None
    assert observation.received_at <= observation.observed_at <= datetime.fromisoformat(material["manifest"]["received_at"])
    assert not (tmp_path / "ready").exists()
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in (tmp_path/"objects").iterdir())
    with pytest.raises(ValueError, match="already_sealed"):
        seal(capture, tmp_path)


@pytest.mark.parametrize("field,value", [
    ("source","unknown"), ("request_key",("1",1)), ("request_key",(True,1)),
    ("request_key",(1,0)), ("request_key",(1,1,"extra")), ("raw",b""),
    ("raw","text"), ("received_at",datetime(2000,1,1)),
    ("received_at",datetime.now()+timedelta(days=1)),
])
def test_invalid_observation_explicitly_incomplete(tmp_path, field, value):
    capture = ResponseCapture()
    kwargs = dict(source="eastmoney_fund", request_key=(1,1), raw=b"{}", received_at=datetime.now())
    kwargs[field] = value
    capture.observe(**kwargs)
    report = seal(capture, tmp_path)["report"]
    assert not report["responses"] and report["capture_errors"]
    assert report["status"] == "incomplete_or_empty"


@pytest.mark.parametrize("limit", ["MAX_BODY_BYTES","MAX_TOTAL_BYTES","MAX_RESPONSES"])
def test_budget_cap_does_not_truncate_or_fake_complete(tmp_path, monkeypatch, limit):
    monkeypatch.setattr("app.data.source_capture."+limit, 1)
    capture = capture_one(b"{}")
    capture.observe(source="eastmoney_fund", request_key=(2,1), raw=b"{}", received_at=datetime.now())
    report = seal(capture,tmp_path)["report"]
    assert report["capture_errors"] and report["status"] == "incomplete_or_empty"
    assert report["response_count_seen"] == 2
    assert all(r.raw == b"{}" for r in capture.responses)


def test_symlinks_and_corruption_fail_without_ready(tmp_path):
    elsewhere = tmp_path/"else"; elsewhere.mkdir()
    link = tmp_path/"link"; link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        seal(capture_one(),link)
    root = tmp_path/"archive"
    result = seal(capture_one(),root)
    ref = result["report"]["responses"][0]["envelope_ref"]
    (root/ref["path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        MaterialArchive(root).read(ref)
    assert not (root/"ready").exists()


def test_partial_seal_failure_never_publishes_batch_or_ready(tmp_path,monkeypatch):
    capture=capture_one()
    original=MaterialArchive.put
    calls=[]
    def fail_report(self,raw,**kwargs):
        calls.append(raw)
        if len(calls)==3:raise OSError("disk full before batch receipt")
        return original(self,raw,**kwargs)
    monkeypatch.setattr(MaterialArchive,"put",fail_report)
    with pytest.raises(OSError):seal(capture,tmp_path)
    assert len(list((tmp_path/"objects").iterdir()))==2
    assert not (tmp_path/"ready").exists()
    assert all(decode(raw).get("schema_version") != "source_transport_capture_v1"
               for raw in calls[1:2])


def test_duplicate_raw_dedup_keeps_each_real_observation(tmp_path):
    capture = capture_one()
    capture.observe(source="eastmoney_fund",request_key=(1,2),raw=b"not-json",received_at=datetime.now())
    report=seal(capture,tmp_path)["report"]
    archive=MaterialArchive(tmp_path)
    materials=[archive.read(r["envelope_ref"]) for r in report["responses"]]
    assert materials[0]["manifest"]["raw_ref"] == materials[1]["manifest"]["raw_ref"]
    assert [r["request_key"] for r in report["responses"]] == [[1,1],[1,2]]


def test_optional_observer_failure_redacted_and_not_cancellation(tmp_path,monkeypatch):
    capture=ResponseCapture()
    def fail(**_): raise OSError("secret-cookie")
    monkeypatch.setattr(capture,"observe",fail)
    observe_response(capture,source="ths_kline",request_key=("000001","last.js",1),
        response=httpx.Response(200,content=b"{}"),received_at=datetime.now())
    result=seal(capture,tmp_path)
    assert result["report"]["capture_errors"] == {"observer_failure":1}
    assert "secret" not in json.dumps(result)
    def cancel(**_): raise asyncio.CancelledError()
    monkeypatch.setattr(capture,"observe",cancel)
    with pytest.raises(asyncio.CancelledError):
        observe_response(capture,source="ths_kline",request_key=(),response=httpx.Response(200),received_at=datetime.now())


def test_non_200_is_not_mislabeled_or_archived(tmp_path):
    capture=ResponseCapture()
    observe_response(capture,source="ths_kline",request_key=(),response=httpx.Response(201,content=b"secret"),received_at=datetime.now())
    report=seal(capture,tmp_path)["report"]
    assert report["responses"] == [] and report["capture_errors"] == {"non_200_body_not_retained":1}


def install_transport(monkeypatch, handler):
    original=httpx.AsyncClient
    calls=[]
    async def dispatch(request):
        calls.append(request)
        return handler(request,len(calls))
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw: original(**kw,transport=httpx.MockTransport(dispatch)))
    monkeypatch.setattr("app.data.sources.eastmoney_source.asyncio.sleep",AsyncMock())
    return calls


def fund_row(code="000001", **extra):
    return {"f12":code,"f14":"测试","f124":int(datetime.now().timestamp()),
        "f2":10,"f3":1,"f62":3,"f184":1,"f66":1,"f69":1,"f72":2,"f75":1,
        "f78":-1,"f81":-1,"f84":-2,"f87":-1,**extra}


@pytest.mark.asyncio
async def test_fund_captures_all_pages_before_filter_and_preserves_output(tmp_path,monkeypatch):
    rows=[fund_row(f"{n:06}") for n in range(101)]
    rows[0]["f124"]=1
    bodies=[]
    def handler(req,_):
        pn=int(req.url.params["pn"]);assert req.url.params["fid"] == "f12"
        raw=json.dumps({"data":{"total":101,"diff":rows[:100] if pn==1 else rows[100:]}}).encode()
        bodies.append(raw);return httpx.Response(200,content=raw)
    calls=install_transport(monkeypatch,handler)
    capture=ResponseCapture()
    result=await EastMoneySource(response_capture=capture).get_individual_fund_flow()
    baseline=await EastMoneySource().get_individual_fund_flow()
    assert result.drop(columns=["received_at"]).equals(baseline.drop(columns=["received_at"]))
    assert len(result)==100 and result.attrs["fund_flow_expected_count"]==101
    assert [r.raw for r in capture.responses]==bodies[:2]
    assert [r.request_key for r in capture.responses]==[(1,1),(2,1)]
    assert list(result["received_at"].unique()) == [r.received_at for r in capture.responses]
    assert len(calls)==4 and seal(capture,tmp_path)["report"]["formal_ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b'{"data":null}',b"{broken-json",b'{"data":{"total":1,"diff":[]}}'])
async def test_fund_rejected_body_retained_not_reparsed_as_success(tmp_path,monkeypatch,body):
    calls=install_transport(monkeypatch,lambda *_:httpx.Response(200,content=body))
    capture=ResponseCapture()
    with pytest.raises(ValueError):
        await EastMoneySource(response_capture=capture).get_individual_fund_flow()
    report=seal(capture,tmp_path,status="failed",error="ValueError")["report"]
    assert len(calls)==1 and capture.responses[0].raw==body
    assert report["operation_status"]=="failed" and report["formal_ready"] is False


@pytest.mark.asyncio
async def test_retry_does_not_archive_status_body_or_renumber_attempt(tmp_path,monkeypatch):
    body=json.dumps({"data":{"total":1,"diff":[fund_row()]}}).encode()
    calls=install_transport(monkeypatch,lambda req,n:httpx.Response(503,content=b"do-not-store") if n==1 else httpx.Response(200,content=body))
    capture=ResponseCapture()
    assert len(await EastMoneySource(response_capture=capture).get_individual_fund_flow())==1
    assert len(calls)==2 and capture.responses[0].request_key==(1,2)
    assert capture.responses[0].raw==body


@pytest.mark.asyncio
async def test_ths_retains_each_cdn_body_not_only_chosen_bars(tmp_path,monkeypatch):
    bodies=[b'callback({"data":"20260908,10,12,9,11,1000,10000,1"})',
            b'callback({"data":"20260908,10,13,9,12,1000,10000,1"})']
    calls=install_transport(monkeypatch,lambda req,n:httpx.Response(200,content=bodies[(n-1)%2]))
    monkeypatch.setattr(ThsKlineSource,"_get_cookie",AsyncMock(return_value="secret-cookie"))
    capture=ResponseCapture()
    result=await ThsKlineSource(response_capture=capture)._fetch_kline("000001","2026.js")
    baseline=await ThsKlineSource()._fetch_kline("000001","2026.js")
    assert result==baseline and result[0]["close"]==12
    assert [r.raw for r in capture.responses]==bodies
    report=seal(capture,tmp_path)["report"]
    assert [r["request_key"] for r in report["responses"]]==[["000001","2026.js",1],["000001","2026.js",2]]
    assert "secret-cookie" not in json.dumps(report) and len(calls)==4


@pytest.mark.asyncio
async def test_default_sources_do_not_open_archive(monkeypatch):
    monkeypatch.setattr(MaterialArchive,"__init__",lambda *_a,**_k:pytest.fail("no filesystem archive in source"))
    install_transport(monkeypatch,lambda *_:httpx.Response(200,json={"data":{"total":1,"diff":[fund_row()]}}))
    assert len(await EastMoneySource().get_individual_fund_flow())==1


@pytest.mark.asyncio
async def test_cancellation_propagates_without_seal_or_retry(monkeypatch):
    def cancel(*_):raise asyncio.CancelledError()
    calls=install_transport(monkeypatch,cancel)
    capture=ResponseCapture()
    with pytest.raises(asyncio.CancelledError):
        await EastMoneySource(response_capture=capture).get_individual_fund_flow()
    assert len(calls)==1 and capture.responses==() and not capture._sealed
