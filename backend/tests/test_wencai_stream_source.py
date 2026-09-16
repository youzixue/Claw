"""问财流式源：解析正确性与「失败绝不退化为空表」的契约。

背景：pywencai 自 2026-08 下旬起完全失效（上游从 JSON 改为 SSE 流），
本项目改用 `WencaiStreamSource`。这些用例不联网，只锁定：

1. SSE → result_page → DataFrame 的解析路径；
2. **取不到时必须抛错，绝不返回空 DataFrame** —— 否则调用方会把
   「登录失效/协议改版」误读成「今天没有涨停股」。这与
   `_update_stock_status` 既有的「列表缺席不是风险解除证据」语义一致。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.data.sources.wencai_stream_source import (
    WencaiStreamError,
    WencaiStreamSource,
    extract_table,
    normalize_dated_columns,
)


def _sse(*events: dict) -> str:
    return "\n\n".join("data:" + json.dumps(e, ensure_ascii=False) for e in events)


def _result_event(rows: list[dict], *, code_count: int | None = None) -> dict:
    return {
        "answer_path": "other/openAnswer",
        "section": {
            "show_type": "result_page",
            "result_page": {
                "components": [
                    {"uuid": "u1", "category": "generic",
                     "data": {"chunks_info": "[\"涨停原因 (91)\"]",
                              "code_count": code_count if code_count is not None else len(rows),
                              "row_count": len(rows), "columns": [], "datas": rows}}
                ]
            },
        },
    }


BASE_EVENT = {"type": "base_info", "answer_path": "logs/baseInfo",
              "base_info": {"user_id": "828738176", "question": "涨停原因"}}


# ── 解析：正常路径 ───────────────────────────────────────────────────


def test_extract_table_finds_rows():
    text = _sse(BASE_EVENT, _result_event([{"code": "688004", "股票简称": "博汇科技"}]))
    data = extract_table(text)
    assert data is not None
    assert data["code_count"] == 1
    assert data["datas"][0]["code"] == "688004"


def test_extract_table_skips_non_result_events():
    """base_info / sourceLink 这类无 result_page 的事件必须被跳过。"""
    text = _sse(
        BASE_EVENT,
        {"answer_path": "extraInfo/sourceLink", "extra": {}},
        _result_event([{"code": "000001"}]),
        {"answer_path": "logs/trace_debug", "is_last": True},
    )
    assert extract_table(text) is not None


def test_extract_table_ignores_malformed_lines():
    """坏行、空行、[DONE]、非 JSON 都不能让解析崩溃。"""
    text = "\n".join([
        "", "   ", "data:", "data:[DONE]", "data:not-json",
        "event: message", ":comment",
        "data:" + json.dumps(_result_event([{"code": "600000"}]), ensure_ascii=False),
    ])
    data = extract_table(text)
    assert data is not None and data["datas"][0]["code"] == "600000"


def test_extract_table_returns_none_without_result_page():
    assert extract_table(_sse(BASE_EVENT)) is None


def test_extract_table_returns_none_on_empty_stream():
    assert extract_table("") is None


# ── 列名归一 ─────────────────────────────────────────────────────────


def test_normalize_dated_columns_strips_suffix_and_records_date():
    frame = pd.DataFrame([{"code": "688004", "涨停原因[20260916]": "AI"}])
    out = normalize_dated_columns(frame)
    assert "涨停原因" in out.columns
    assert "涨停原因[20260916]" not in out.columns
    assert out.attrs["dated_columns"] == {"涨停原因": "20260916"}


def test_normalize_leaves_plain_columns_untouched():
    frame = pd.DataFrame([{"code": "688004", "最新价": 1.0}])
    out = normalize_dated_columns(frame)
    assert list(out.columns) == ["code", "最新价"]
    assert "dated_columns" not in out.attrs


def test_normalize_does_not_treat_bracket_text_as_date():
    """`涨停原因[概念]` 不是日期后缀，不得被剥掉。"""
    frame = pd.DataFrame([{"涨停原因[概念]": "AI"}])
    out = normalize_dated_columns(frame)
    assert "涨停原因[概念]" in out.columns


# ── 失败契约：绝不退化为空表 ─────────────────────────────────────────


def test_missing_cookie_raises_not_empty_frame(monkeypatch):
    from app.data.sources import wencai_stream_source as mod

    monkeypatch.setattr(mod.settings, "PYWENCAI_COOKIE", "", raising=False)
    with pytest.raises(WencaiStreamError, match="PYWENCAI_COOKIE"):
        WencaiStreamSource().query("涨停原因")


class _FakeResponse:
    def __init__(self, status_code=200, text_body="", lines=()):
        self.status_code = status_code
        self.text = text_body
        self._lines = list(lines)

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


def _patch_post(monkeypatch, response):
    from app.data.sources import wencai_stream_source as mod

    monkeypatch.setattr(mod.settings, "PYWENCAI_COOKIE", "v=abc", raising=False)
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: response)


def test_401_raises_session_expired(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(status_code=401))
    with pytest.raises(WencaiStreamError, match="401"):
        WencaiStreamSource().query("涨停原因")


def test_http_error_raises(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(status_code=502, text_body="bad gateway"))
    with pytest.raises(WencaiStreamError, match="502"):
        WencaiStreamSource().query("涨停原因")


def test_stream_without_result_page_raises_not_empty_frame(monkeypatch):
    """登录失效时上游可能返回 200 但流里没有 result_page —— 必须抛错。"""
    _patch_post(monkeypatch, _FakeResponse(
        lines=["data:" + json.dumps(BASE_EVENT, ensure_ascii=False)]))
    with pytest.raises(WencaiStreamError, match="result_page"):
        WencaiStreamSource().query("涨停原因")


def test_request_exception_raises(monkeypatch):
    from app.data.sources import wencai_stream_source as mod

    monkeypatch.setattr(mod.settings, "PYWENCAI_COOKIE", "v=abc", raising=False)

    def boom(*a, **k):
        raise mod.requests.RequestException("connection reset")

    monkeypatch.setattr(mod.requests, "post", boom)
    with pytest.raises(WencaiStreamError, match="请求失败"):
        WencaiStreamSource().query("涨停原因")


def test_success_returns_frame_with_attrs(monkeypatch):
    """成功路径：返回 DataFrame，并把 code_count 等元信息放进 attrs。"""
    rows = [{"code": "688004", "股票简称": "博汇科技"}]
    _patch_post(monkeypatch, _FakeResponse(
        lines=["data:" + json.dumps(_result_event(rows, code_count=91), ensure_ascii=False)]))
    frame = WencaiStreamSource().query("涨停原因")
    assert len(frame) == 1
    assert frame.attrs["code_count"] == 91
    assert frame.attrs["chunks_info"] == "[\"涨停原因 (91)\"]"


# ── 请求体契约 ───────────────────────────────────────────────────────


def test_payload_carries_finquery_tool_and_perpage():
    """请求体必须带 FinQuery 工具与 perpage —— 这是取到数据的必要条件。"""
    from app.data.sources.wencai_stream_source import _payload

    body = _payload("今天涨停", perpage=30)
    assert body["question"] == "今天涨停"
    tool = body["agent_tools"][0]
    assert tool["tool_id"] == "FinQuery"
    assert tool["tool_param"] == {"domain": "stock", "perpage": 30}
    assert body["source"] == "ths_iwencai_pc_xuangu"


def test_headers_use_event_stream_accept():
    from app.data.sources.wencai_stream_source import _headers

    headers = _headers("v=abc")
    assert headers["accept"] == "text/event-stream"
    assert headers["cookie"] == "v=abc"
    assert headers["x-source"] == "ths_iwencai_pc_xuangu"


# ── 缓存（性能优化） ─────────────────────────────────────────────────


def _counting_source(monkeypatch, rows=None):
    """装一个只调一次外部请求的源，用于验证缓存确实省掉了重复拉取。"""
    from app.data.sources import wencai_stream_source as mod

    monkeypatch.setattr(mod.settings, "PYWENCAI_COOKIE", "v=abc", raising=False)
    calls = {"n": 0}
    body = rows if rows is not None else [{"code": "688004", "股票简称": "博汇科技"}]

    class _Resp:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode=False):
            yield "data:" + json.dumps(_result_event(body), ensure_ascii=False)

    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(mod.requests, "post", fake_post)
    mod.WencaiStreamSource._cache.clear()
    return calls


def test_cache_avoids_second_fetch(monkeypatch):
    """ttl>0 时第二次同问句不得再发请求 —— 这是盘前/盘后重复拉取的优化点。"""
    calls = _counting_source(monkeypatch)
    src = WencaiStreamSource()
    first = src.query("全部A股 所属行业", perpage=10000, ttl_sec=3600)
    second = src.query("全部A股 所属行业", perpage=10000, ttl_sec=3600)

    assert calls["n"] == 1, "缓存未生效，仍发了第二次请求"
    assert first.attrs["cache_hit"] is False
    assert second.attrs["cache_hit"] is True
    assert list(first.columns) == list(second.columns)


def test_cache_disabled_by_default(monkeypatch):
    """ttl=0（默认）必须每次实拉，保持既有行为不变。"""
    calls = _counting_source(monkeypatch)
    src = WencaiStreamSource()
    src.query("涨停原因")
    src.query("涨停原因")
    assert calls["n"] == 2


def test_cache_returns_copy_so_callers_cannot_pollute_it(monkeypatch):
    """缓存必须返回副本：调用方改动不得污染后续命中。"""
    _counting_source(monkeypatch)
    src = WencaiStreamSource()
    first = src.query("涨停原因", ttl_sec=3600)
    first.loc[0, "股票简称"] = "被改了"
    second = src.query("涨停原因", ttl_sec=3600)
    assert second.loc[0, "股票简称"] == "博汇科技"


def test_cache_key_includes_perpage(monkeypatch):
    """不同 perpage 不得互相命中（返回的行数不同）。"""
    calls = _counting_source(monkeypatch)
    src = WencaiStreamSource()
    src.query("涨停原因", perpage=50, ttl_sec=3600)
    src.query("涨停原因", perpage=5000, ttl_sec=3600)
    assert calls["n"] == 2


def test_failure_never_poisones_cache(monkeypatch):
    """失败不得写入缓存 —— 否则一次抖动会被缓存 TTL 放大成持续故障。"""
    from app.data.sources import wencai_stream_source as mod

    monkeypatch.setattr(mod.settings, "PYWENCAI_COOKIE", "v=abc", raising=False)
    mod.WencaiStreamSource._cache.clear()
    monkeypatch.setattr(mod.requests, "post",
                        lambda *a, **k: _FakeResponse(status_code=401))
    src = WencaiStreamSource()
    with pytest.raises(WencaiStreamError):
        src.query("涨停原因", ttl_sec=3600)
    assert mod.WencaiStreamSource._cache == {}, "失败被写进了缓存"
