"""新闻面判断口径测试."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.ai.sentiment import normalize_sentiment_result
from app.api.v1.news import (
    _impact_score,
    _is_news_analyzed,
    _is_major_news,
    _news_payload,
    _news_window_summary,
    _stock_name_alias_allowed,
)


def test_ai_sentiment_result_is_normalized_and_clamped():
    result = normalize_sentiment_result({
        "sentiment": "Bullish",
        "confidence": 1.8,
        "impact_scope": "bad-scope",
        "related_sectors": [" 半导体 ", ""],
        "key_points": ["政策催化"],
    })

    assert result["sentiment"] == "bullish"
    assert result["confidence"] == 1.0
    assert result["impact_scope"] == "stock"
    assert result["related_sectors"] == ["半导体"]


def test_ai_sentiment_result_rejects_invalid_direction():
    assert normalize_sentiment_result({"sentiment": "up-only", "confidence": 0.9}) is None


def test_major_news_requires_confirmed_direction_context():
    assert not _is_major_news(
        sentiment="bullish",
        impact_scope="market",
        confidence=0.56,
        importance=6,
        source="em",
        events=[],
    )

    assert _is_major_news(
        sentiment="bullish",
        impact_scope="market",
        confidence=0.56,
        importance=7,
        source="cls",
        events=[],
    )


def test_impact_score_uses_event_direction_and_recency():
    now = datetime.now()
    news = SimpleNamespace(
        source="cninfo",
        impact_scope="stock",
        publish_time=now - timedelta(hours=2),
    )
    score = _impact_score(
        news,
        [{"direction": "bearish", "impact": "high"}],
        confidence=0.8,
        sentiment="bullish",
    )

    assert score < 0
    assert abs(score) > 0.8


def test_stock_name_alias_filters_ambiguous_short_names():
    assert not _stock_name_alias_allowed("银行")
    assert not _stock_name_alias_allowed("ST鹏博")
    assert _stock_name_alias_allowed("宁德时代")


def test_raw_news_is_marked_unanalyzed_and_has_no_direction_score():
    raw = SimpleNamespace(
        id=1,
        source="cls",
        title="某公司公告重大事项",
        content="",
        url="",
        publish_time=datetime.now(),
        category="announcement",
        related_codes="[]",
        related_sectors="[]",
        bull_bear_confidence=0.0,
        sentiment="neutral",
        impact_scope="stock",
        importance=5,
        summary="",
        events_json="[]",
        bull_bear="neutral",
        nlp_status="raw",
    )

    payload = _news_payload(raw)

    assert not _is_news_analyzed(raw)
    assert payload["nlp_status"] == "raw"
    assert not payload["is_major"]
    assert payload["direction_score"] == 0.0


def test_news_analysis_status_uses_explicit_nlp_status():
    analyzed = SimpleNamespace(nlp_status="analyzed", bull_bear_confidence=0.0)
    fallback = SimpleNamespace(nlp_status="fallback", bull_bear_confidence=0.0)
    raw_with_confidence = SimpleNamespace(nlp_status="raw", bull_bear_confidence=0.9)
    legacy = SimpleNamespace(nlp_status=None, bull_bear_confidence=0.6)

    assert _is_news_analyzed(analyzed)
    assert _is_news_analyzed(fallback)
    assert not _is_news_analyzed(raw_with_confidence)
    assert _is_news_analyzed(legacy)


def test_news_window_summary_uses_only_analyzed_news_for_direction():
    summary = _news_window_summary(
        [
            {
                "is_analyzed": True,
                "is_major": True,
                "sentiment": "bullish",
                "direction_score": 1.2,
                "related_sectors": ["机器人"],
                "related_codes": ["300001"],
                "direct_related_codes": ["300001"],
                "title": "机器人产业政策落地",
                "summary": "政策支持机器人产业链，利好核心设备公司",
                "source": "cls",
                "publish_time": "2026-05-06T09:30:00",
            },
            {
                "is_analyzed": False,
                "is_major": False,
                "sentiment": "bearish",
                "direction_score": -2.0,
                "related_sectors": ["银行"],
                "related_codes": ["600000"],
                "direct_related_codes": [],
                "title": "未精洗利空不能参与方向",
            },
        ],
        {"fetched_count": 2, "analyzed_count": 1, "pending_count": 1, "analyzing_count": 0},
        {"300001": "测试科技"},
    )

    assert summary["direction"] == "bullish"
    assert summary["net_score"] == 1.2
    assert summary["major_count"] == 1
    assert summary["top_sectors"] == [{"name": "机器人", "count": 1}]
    assert summary["core_bullish_stocks"][0]["name"] == "测试科技"
    assert "利好主线" in summary["interpretation"][1]
    assert "待分析" in summary["key_points"][-1]
