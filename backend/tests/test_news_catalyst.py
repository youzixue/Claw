import json
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app.news.catalyst import (
    classify_news_event,
    load_direct_stock_catalyst_map,
    score_news_catalyst,
)
from app.news.dedup import NewsDeduplicator
from app.news.sources.base import NewsItem
from app.news.sources.cninfo import CninfoSource
from app.news.sources.em import EmSource


def _news(title: str, **overrides):
    values = {
        "title": title,
        "source": "cninfo",
        "publish_time": datetime(2026, 8, 10, 18, 30),
        "importance": 5,
        "bull_bear": "neutral",
        "sentiment": "neutral",
        "bull_bear_confidence": 0,
        "impact_scope": "stock",
    }
    values.update(overrides)
    # Scoring tests receive already-resolved PIT inputs; DB/version gating is
    # covered by test_news_evidence_versions with real isolated SQL queries.
    values.update({
        "news_id": 1, "content_version_id": 1, "analysis_version_id": None,
        "first_received_at": values["publish_time"],
        "content_available_at": values["publish_time"],
        "analysis_completed_at": None, "available_at": values["publish_time"],
        "as_of_at": datetime(2026, 8, 18, 9, 25), "evidence_protocol": "news_pit_v1",
        "entity_evidence": [],
    })
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_em_source_prefers_global_flash_for_overseas_catalysts(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(
        ak,
        "stock_info_global_em",
        lambda: pd.DataFrame(
            [
                {
                    "标题": "海外mRNA肿瘤疫苗III期达到主要终点",
                    "摘要": "产业链原料与研发服务需求受到关注",
                    "发布时间": "2026-08-20 07:20:00",
                    "链接": "https://example.test/mrna",
                },
                {
                    "标题": "海外存储厂商上调资本开支",
                    "摘要": "AI服务器需求继续增长",
                    "发布时间": "2026-08-20 07:10:00",
                    "链接": "https://example.test/memory",
                },
            ]
        ),
    )

    rows = await EmSource().fetch_latest(limit=2)

    assert len(rows) == 2
    assert rows[0].category == "global_flash"
    assert rows[0].publish_time == datetime(2026, 8, 20, 7, 20)
    assert "mRNA" in rows[0].title


def test_raw_risk_disclosure_cannot_be_scored_as_positive_catalyst():
    grade, event_type, _ = classify_news_event("股票交易严重异常波动暨风险提示公告")
    assert grade == "risk"
    assert event_type == "risk_disclosure"
    assert score_news_catalyst(
        _news("股票交易严重异常波动暨风险提示公告"),
        date(2026, 8, 10),
    ) == 0


@pytest.mark.parametrize(
    ("title", "event_type"),
    [
        (
            "关于申请撤销对公司股票交易实施其他风险警示的公告",
            "risk_warning_review",
        ),
        (
            "关于公司被债权人申请预重整及重整的提示性公告",
            "distress_restructuring",
        ),
        (
            "关于与产业投资人签署重整投资协议的公告",
            "distress_restructuring",
        ),
    ],
)
def test_special_situation_events_are_observation_only(title, event_type):
    grade, actual_type, adjustment = classify_news_event(title)

    assert grade == "risk"
    assert actual_type == event_type
    assert adjustment < 0
    assert score_news_catalyst(
        _news(
            title,
            bull_bear="bull",
            sentiment="positive",
            bull_bear_confidence=0.99,
            importance=10,
        ),
        date(2026, 8, 20),
    ) == 0


def test_raw_major_restructuring_gets_hard_event_bonus():
    grade, event_type, _ = classify_news_event("发行股份及支付现金购买资产暨重大资产重组报告书")
    score = score_news_catalyst(
        _news("发行股份及支付现金购买资产暨重大资产重组报告书"),
        date(2026, 8, 10),
    )
    assert grade == "hard"
    assert event_type == "major_restructuring"
    assert score >= 70


@pytest.mark.parametrize(
    ("title", "event_type"),
    [
        (
            "关于发行股份购买资产事项获得上海证券交易所并购重组审核委员会审核通过的公告",
            "m&a_review_approved",
        ),
        (
            "关于收到深圳证券交易所并购重组审核委员会审核公司发行股份购买资产事项会议安排的公告",
            "m&a_review_scheduled",
        ),
        (
            "关于发行股份及支付现金购买资产事项获得上海市国资委批复的公告",
            "regulatory_approval",
        ),
        (
            "关于签署补充协议暨控制权拟发生变更的进展公告",
            "control_change",
        ),
    ],
)
def test_formal_restructuring_milestones_are_hard_events(title, event_type):
    grade, actual_type, _ = classify_news_event(title)

    assert grade == "hard"
    assert actual_type == event_type


@pytest.mark.parametrize(
    "title",
    [
        "关于重大资产重组的进展公告",
        "关于重大资产重组进展的提示性公告",
        "关于发行股份及支付现金购买资产事项的进展公告",
        "关于延期回复发行股份购买资产审核问询函的公告",
        "关于延长公司发行股份购买资产股东会决议有效期的公告",
        "收购报告书摘要",
    ],
)
def test_non_decisive_restructuring_progress_is_not_hard_catalyst(title):
    grade, event_type, _ = classify_news_event(title)

    assert grade == "routine"
    assert event_type in {"restructuring_progress", "routine_disclosure"}


def test_numeric_profit_change_is_classified_symmetrically():
    grade, event_type, adjustment = classify_news_event(
        "宁波韵升：上半年净利润同比增长154.18%"
    )
    assert grade == "hard"
    assert event_type == "earnings_surge"
    assert adjustment > 0

    grade, event_type, adjustment = classify_news_event(
        "某公司：上半年归母净利润同比下降51.2%"
    )
    assert grade == "risk"
    assert event_type == "earnings_decline"
    assert adjustment < 0


def test_buyback_company_shares_wording_is_recognized_as_medium_event():
    grade, event_type, adjustment = classify_news_event(
        "新华百货：拟2亿元至4亿元回购公司股份"
    )

    assert grade == "medium"
    assert event_type == "buyback"
    assert adjustment > 0


@pytest.mark.asyncio
async def test_direct_stock_catalyst_prefers_formal_hard_event_over_mapped_market_news(monkeypatch):
    market_news = _news(
        "有色板块震荡回升，关联个股走强",
        source="ths",
        publish_time=datetime(2026, 8, 12, 14, 30),
        importance=9,
        bull_bear="bull",
        sentiment="positive",
        bull_bear_confidence=0.95,
        related_codes=json.dumps(["600962"]),
    )
    formal_notice = _news(
        "关于发行股份购买资产事项获得上海证券交易所并购重组审核委员会审核通过的公告",
        publish_time=datetime(2026, 8, 8, 18, 0),
        related_codes=json.dumps(["600962"]),
    )

    async def resolved_evidence(*args, **kwargs):
        return [market_news, formal_notice]

    monkeypatch.setattr("app.news.catalyst.load_news_evidence_as_of", resolved_evidence)

    class FakeSession:
        pass

    result = await load_direct_stock_catalyst_map(
        FakeSession(),
        date(2026, 8, 12),
        candidate_codes={"600962"},
    )

    assert result["600962"]["news_source"] == "cninfo"
    assert result["600962"]["news_event_type"] == "m&a_review_approved"


@pytest.mark.asyncio
async def test_next_session_midnight_disclosure_is_marked_as_fresh_after_close(monkeypatch):
    midnight_notice = _news(
        "关于发行股份购买资产事项获得交易所并购重组审核委员会审核通过的公告",
        publish_time=datetime(2026, 8, 18, 0, 5),
        related_codes=json.dumps(["600962"]),
    )

    async def resolved_evidence(*args, **kwargs):
        return [midnight_notice]

    monkeypatch.setattr("app.news.catalyst.load_news_evidence_as_of", resolved_evidence)

    class FakeSession:
        pass

    result = await load_direct_stock_catalyst_map(
        FakeSession(),
        date(2026, 8, 17),
        candidate_codes={"600962"},
        news_end_time=datetime(2026, 8, 18, 8, 30),
    )

    assert result["600962"]["news_fresh_after_trade_close"] is True
    assert result["600962"]["news_publish_time"].startswith("2026-08-18T00:05")


@pytest.mark.asyncio
async def test_direct_news_aggregates_repeated_high_impact_and_overnight_evidence(monkeypatch):
    rows = [
        _news(
            "公司签订重大合同公告",
            publish_time=datetime(2026, 8, 17, 13, 30),
            importance=7,
            related_codes=json.dumps(["600962"]),
        ),
        _news(
            "公司重大项目获得批复公告",
            publish_time=datetime(2026, 8, 17, 18, 10),
            importance=8,
            related_codes=json.dumps(["600962"]),
        ),
        _news(
            "公司业绩大幅增长公告",
            publish_time=datetime(2026, 8, 18, 0, 5),
            importance=8,
            related_codes=json.dumps(["600962"]),
        ),
    ]

    async def resolved_evidence(*args, **kwargs):
        return rows

    monkeypatch.setattr("app.news.catalyst.load_news_evidence_as_of", resolved_evidence)

    class FakeSession:
        pass

    result = await load_direct_stock_catalyst_map(
        FakeSession(),
        date(2026, 8, 17),
        candidate_codes={"600962"},
        news_end_time=datetime(2026, 8, 18, 9, 25),
    )

    context = result["600962"]
    assert context["news_count"] == 3
    assert context["news_before_close_count"] == 1
    assert context["news_after_close_count"] == 2
    assert context["news_high_impact_count"] == 3
    assert context["news_repeated_direct"] is True
    assert context["news_direct_high_impact"] is True
    assert context["news_information_phase"] == "overnight"


@pytest.mark.asyncio
async def test_explicit_news_asof_is_hard_query_cutoff():
    cutoff = datetime(2026, 8, 17, 14, 0)
    captured = {}

    class FakeSession:
        async def execute(self, stmt):
            captured.update(stmt.compile().params)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [])
            )

    await load_direct_stock_catalyst_map(
        FakeSession(),
        date(2026, 8, 17),
        news_end_time=cutoff,
    )

    assert cutoff in captured.values()
    assert datetime(2026, 8, 18, 0, 0) not in captured.values()


def test_disclosure_dedup_keeps_same_template_title_for_different_stocks():
    dedup = NewsDeduplicator()
    items = [
        NewsItem(source="cninfo", title="关于股票交易异常波动的公告", related_codes=["000001"]),
        NewsItem(source="cninfo", title="关于股票交易异常波动的公告", related_codes=["000002"]),
    ]

    assert len(dedup.dedup(items)) == 2


@pytest.mark.asyncio
async def test_cninfo_source_uses_current_disclosure_rows(monkeypatch):
    source = CninfoSource()
    calls = []
    df = pd.DataFrame([{
        "代码": "537",
        "简称": "绿发电力",
        "公告标题": "关于投资天津滨海新区46万千瓦风电项目的公告",
        "公告时间": "2026-08-10 18:20:00",
        "公告链接": "https://www.cninfo.com.cn/example",
    }])

    async def fake_fetch_disclosures(*, code="", lookback_days=7):
        calls.append((code, lookback_days))
        return df

    monkeypatch.setattr(source, "_fetch_disclosures", fake_fetch_disclosures)

    latest = await source.fetch_latest(limit=10)
    by_code = await source.fetch_by_code("537", limit=10)

    assert calls == [("", 7), ("000537", 45)]
    assert latest[0].related_codes == ["000537"]
    assert by_code[0].publish_time == datetime(2026, 8, 10, 18, 20)
    assert by_code[0].url.startswith("https://www.cninfo.com.cn/")


@pytest.mark.asyncio
async def test_cninfo_latest_reserves_older_material_notice_beyond_latest_limit(monkeypatch):
    source = CninfoSource()
    rows = [
        {
            "代码": f"{index + 1:06d}",
            "公告标题": f"第{index + 1}份公司治理制度公告",
            "公告时间": f"2026-08-22 20:{index:02d}:00",
            "公告链接": f"https://www.cninfo.com.cn/routine/{index}",
        }
        for index in range(20)
    ]
    rows.append({
        "代码": "601700",
        "公告标题": "关于南方电网项目中标的公告",
        "公告时间": "2026-08-20 18:30:00",
        "公告链接": "https://www.cninfo.com.cn/material/601700",
    })

    async def fake_fetch_disclosures(*, code="", lookback_days=7):
        return pd.DataFrame(rows)

    monkeypatch.setattr(source, "_fetch_disclosures", fake_fetch_disclosures)

    latest = await source.fetch_latest(limit=5)

    assert any(item.related_codes == ["601700"] for item in latest)
    assert len(latest) <= 20


def test_cninfo_material_reserve_is_not_retruncated_in_dense_reporting_window():
    newer_material = [
        {
            "代码": f"{index:06d}",
            "公告标题": f"公司{index}业绩预告",
            "公告时间": pd.Timestamp("2026-08-24 18:00:00")
            - pd.Timedelta(seconds=index),
            "公告链接": f"https://www.cninfo.com.cn/material/{index}",
        }
        for index in range(320)
    ]
    newer_material.append({
        "代码": "601700",
        "公告标题": "关于南方电网项目中标的公告",
        "公告时间": pd.Timestamp("2026-08-20 18:30:00"),
        "公告链接": "https://www.cninfo.com.cn/material/601700",
    })

    selected = CninfoSource._select_latest_with_material_reserve(
        pd.DataFrame(newer_material),
        limit=100,
    )

    assert "601700" in set(selected["代码"])


@pytest.mark.asyncio
async def test_cninfo_global_fetch_caps_latest_pages_and_uses_material_keyword_streams(
    monkeypatch,
):
    source = CninfoSource()
    calls = []

    def fake_page_set(*, start_date, end_date, keyword="", max_pages=1):
        calls.append((keyword, max_pages, start_date, end_date))
        suffix = keyword or "latest"
        return pd.DataFrame([{
            "代码": "601700",
            "简称": "风范股份",
            "公告标题": f"{suffix}公告",
            "公告时间": "2026-08-20 18:30:00",
            "公告链接": f"https://www.cninfo.com.cn/{suffix}",
        }])

    monkeypatch.setattr(source, "_fetch_global_page_set", fake_page_set)

    frame = await source._fetch_disclosures(code="", lookback_days=7)

    assert len(calls) == 1 + len(source._MATERIAL_SEARCH_KEYWORDS)
    assert calls[0][0] == ""
    assert calls[0][1] == source._GLOBAL_LATEST_MAX_PAGES
    assert all(
        max_pages == source._GLOBAL_MATERIAL_MAX_PAGES
        for _keyword, max_pages, _start, _end in calls[1:]
    )
    assert "中标公告" in set(frame["公告标题"])
