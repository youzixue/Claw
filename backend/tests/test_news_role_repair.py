"""Original 9/14 review examples plus independent boundary cases; isolated DB."""
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.news import FinanceNews, NewsContentVersion, NewsAnalysisVersion
from app.models.stock import StockSpot
from app.news.catalyst import verify_news_entities, load_news_evidence_as_of, load_direct_stock_catalyst_map, score_news_catalyst
from app.news.engine import NewsEngine
from app.news.roles import assess_news_roles, guard_news_analysis
from app.news.sources.base import NewsItem

T0 = datetime(2026, 9, 10, 10)
MODEL = dict(sentiment="bullish", confidence=0.99, sentiment_method="ai",
             events_method="ai", impact_scope="stock", related_codes=[],
             related_sectors=["人工智能"], summary="已完成收购并确认重大订单",
             events=[{"type": "m&a", "title": "已完成收购并确认重大订单"}])

# Titles/bodies transcribed from production through SQLite mode=ro/query_only.
ORIGINALS = [
    (30314, "CRO概念表现活跃 万邦医药涨超12%",
     "CRO概念表现活跃，万邦医药涨超12%，近岸蛋白、诚达药业、泓博医药、成都先导跟涨。",
     "price_action_retrospective"),
    (29476, "数码视讯：拟转让博汇科技4.10%股份 交易对价6698.56万元",
     "【数码视讯：拟转让博汇科技4.10%股份 交易对价6698.56万元】数码视讯(300079.SZ)公告称，公司与明心泓智签署《股权转让协议》，将所持博汇科技328.36万股股份(占总股本4.10%)转让予明心泓智，每股转让价格20.40元，交易对价6698.56万元。转让完成后，公司仍持有博汇科技331.03万股，占总股本4.13%。",
     "equity_transfer_roles"),
    (30299, "拟收购哪吒汽车的关联公司直线封板",
     "【拟收购哪吒汽车的关联公司直线封板】9月14日，山子高科开盘涨停，涨幅10.04%，集合竞价成交额超9897万元。消息面上，哪吒汽车母公司合众新能源重整迎新进展：浙江太乙圣莲拟出资30亿元，换取其约70.62%的股权。太乙圣莲为山子高科董事长叶骥的关联主体。",
     "affiliate_not_issuer"),
    (29361, "2连板中新赛克：AI应用产品营业收入占公司整体营业收入不超过公司2026年上半年营业收入的2%",
     "【2连板中新赛克：AI应用产品营业收入占公司整体营业收入不超过公司2026年上半年营业收入的2%】中新赛克(002912.SZ)公告称，公司AI应用产品以公司AIWorkForce平台为核心，面向政企客户提供AI应用场景开发、智能体部署及AI安全治理等服务。目前该类产品所处市场为新兴市场，尚处于商业化起步阶段。该类产品的营业收入占公司整体营业收入不超过公司2026年上半年营业收入的2%，公司郑重提醒广大投资者理性决策，审慎投资，注意二级市场交易风险。",
     "limited_business_exposure"),
    (30138, "9月14日投资避雷针：超声电子、金安国纪双双澄清 产品通过英伟达认证为不实信息",
     "【9月14日投资避雷针：超声电子、金安国纪双双澄清 产品通过英伟达认证为不实信息】近日A股及海外市场潜在风险事件如下。公司方面重点关注包括：1）5天3板超声电子公告，\"超声电子高频板通过英伟达认证\"情况不属实，目前无产品供货给英伟达；2）金安国纪提示，网上传播关于公司产品纳入英伟达、华为供应链体系认证等相关信息均为不实信息等。",
     "denial_or_unconfirmed"),
]


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        for model in (FinanceNews, NewsContentVersion, NewsAnalysisVersion, StockSpot):
            await c.run_sync(model.__table__.create)
    clock = [T0]
    monkeypatch.setattr("app.news.engine._news_now", lambda: clock[0])
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all([StockSpot(code=code, name=name) for code, name in [
            ("301520", "万邦医药"), ("300079", "数码视讯"), ("688004", "博汇科技"),
            ("000981", "山子高科"), ("002912", "中新赛克"),
            ("000823", "超声电子"), ("002636", "金安国纪"),
            ("600001", "测试股份"), ("600002", "另一股份")]])
        await session.commit()
        yield session, clock
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("article_id,title,body,reason", ORIGINALS)
async def test_originals_are_candidates_not_positive_beneficiaries(db, article_id, title, body, reason):
    session, clock = db
    raw = NewsItem(source="cls", source_id=str(article_id), title=title, content=body,
                   publish_time=T0 - timedelta(hours=1))
    engine = NewsEngine()
    await engine.cache_raw_items(session, [raw])
    clock[0] += timedelta(minutes=2)
    await engine._save_to_db(session, raw, MODEL)
    await session.commit()
    view = (await load_news_evidence_as_of(session, as_of_at=clock[0]))[0]
    assert reason in view.role_evidence["reasons"]
    assert view.research_only and view.sentiment == "neutral"
    assert view.positive_beneficiary_codes == [] and view.related_sectors == []
    assert await load_direct_stock_catalyst_map(session, T0.date(), news_end_time=clock[0], min_score=0) == {}
    page = await session.scalar(sa.select(FinanceNews))
    assert page.summary == (title + "\n" + body).strip()[:500]
    assert page.sentiment == "neutral"
    assert "已完成收购" not in page.events_json
    attempt = await session.scalar(sa.select(NewsAnalysisVersion))
    frozen = json.loads(attempt.result_json)
    assert frozen["model_hypotheses"]["summary"] == MODEL["summary"]
    for e in view.role_evidence["evidence"]:
        assert (title + "\n" + body)[e["start"]:e["end"]] == e["text"]
    if article_id == 30314:
        assert "301520" in view.related_codes
        assert "301520" in json.loads(page.related_codes)
    if article_id == 29476:
        assert set(view.related_codes) == {"300079", "688004"}
        roles = {e["code"]: e["role"] for e in view.role_evidence["entities"]}
        assert roles == {"300079": "transaction_subject", "688004": "transaction_target"}
    if article_id == 30299:
        assert view.related_codes == ["000981"]
        assert view.role_evidence["entities"][0]["role"] == "affiliate_context_not_direct_acquirer"


@pytest.mark.parametrize("title,body", [
    ("测试股份：订单业务说明", "公司没有取得客户认证。"),
    ("测试股份：AI业务进展", "AI业务收入≤2%。"),
    ("测试股份：业务说明", "公司相关业务营业收入占比不足5%。"),
    ("测试股份：签订合同", "上述获得订单消息为不实信息。"),
    ("测试股份拟出资重整另一企业", ""),
    ("测试股份：产品业务", "尚未收到订单"),
    ("测试股份：AI收入仅2%", ""),
    ("测试股份：认证情况", "目前无产品供货给客户。"),
    ("测试股份涨超8%", "公司签订重大合同"),
    ("测试股份：出售另一股份5%股权", ""),
])
def test_generalized_veto_is_not_code_specific(title, body):
    evidence = assess_news_roles(title, body)
    assert evidence["block_positive_catalyst"]
    guarded = guard_news_analysis(title, body, MODEL)
    assert guarded["research_only"] and guarded["sentiment"] == "neutral"
    assert score_news_catalyst(SimpleNamespace(title=title, content=body, sentiment="bullish"), T0.date()) == 0


def test_empty_and_normal_single_subject_keep_legacy_policy():
    assert assess_news_roles("", "")["candidate_codes"] == []
    title = "测试股份签订重大合同"
    assert not assess_news_roles(title, "")["block_positive_catalyst"]
    assert guard_news_analysis(title, "", MODEL)["summary"] == MODEL["summary"]


@pytest.mark.asyncio
async def test_exact_title_coverage_local_numeric_conflicts_and_weak_names(db):
    session, _ = db
    assert {e["code"] for e in await verify_news_entities(
        session, "万邦医药涨超12%", "参考数码视讯(300079.SZ)，报道编号654321")} == {"301520", "300079"}
    assert await verify_news_entities(session, "万邦医药(688004)涨超12%", "") == []
    assert await verify_news_entities(session, "万邦医药（证券代码：688004）", "") == []
    assert await verify_news_entities(session, "山子董事长关联企业拟投资，中新有进展", "") == []
    session.add(StockSpot(code="600003", name="测试股份"))
    await session.commit()
    assert await verify_news_entities(session, "测试股份签订合同", "") == []


@pytest.mark.asyncio
async def test_new_mapping_analysis_has_real_first_availability_and_no_old_rewrite(db):
    session, clock = db
    stock = await session.get(StockSpot, "301520")
    stock.name = "旧简称"
    await session.commit()
    raw = NewsItem(source="cls", source_id="repair", title="万邦医药签订重大合同",
                   publish_time=T0 - timedelta(hours=1))
    engine = NewsEngine()
    await engine.cache_raw_items(session, [raw])
    await engine._save_to_db(session, raw, MODEL)
    await session.commit()
    first = await session.scalar(sa.select(NewsAnalysisVersion))
    old_bytes = first.result_json
    old_hash = first.result_hash
    assert (await load_news_evidence_as_of(session, as_of_at=T0))[0].related_codes == []
    clock[0] += timedelta(hours=1)
    stock.name = "万邦医药"
    await session.commit()
    await engine._save_to_db(session, raw, MODEL)
    await session.commit()
    before = (await load_news_evidence_as_of(session, as_of_at=clock[0] - timedelta(microseconds=1)))[0]
    after = (await load_news_evidence_as_of(session, as_of_at=clock[0]))[0]
    assert before.related_codes == []
    assert after.related_codes == ["301520"] and after.available_at == clock[0]
    assert after.first_received_at == after.content_available_at == T0
    assert first.result_json == old_bytes and first.result_hash == old_hash
    assert hashlib.sha256(old_bytes.encode()).hexdigest() == old_hash
    assert await session.scalar(sa.select(sa.func.count()).select_from(NewsContentVersion)) == 1
    assert await session.scalar(sa.select(sa.func.count()).select_from(NewsAnalysisVersion)) == 2
    # Identical reanalysis is not a new fact or a refreshed availability.
    clock[0] += timedelta(hours=1)
    await engine._save_to_db(session, raw, MODEL)
    await session.commit()
    assert await session.scalar(sa.select(sa.func.count()).select_from(NewsAnalysisVersion)) == 2


@pytest.mark.asyncio
async def test_raw_projection_includes_title_without_ai(db):
    session, _ = db
    await NewsEngine().cache_raw_items(session, [NewsItem(
        source="cls", source_id="raw", title="CRO概念表现活跃 万邦医药涨超12%",
        related_codes=["600001"], publish_time=T0)])
    page = await session.scalar(sa.select(FinanceNews))
    assert set(json.loads(page.related_codes)) == {"600001", "301520"}


@pytest.mark.asyncio
async def test_processor_cannot_publish_ai_denial_as_confirmed_order(monkeypatch):
    from unittest.mock import AsyncMock
    from app.news.nlp.processor import news_processor
    monkeypatch.setattr("app.news.nlp.processor.analyze_sentiment", AsyncMock(
        return_value={**MODEL, "method": "ai"}))
    monkeypatch.setattr("app.news.nlp.processor.extract_events", AsyncMock(
        return_value={"events": MODEL["events"], "method": "ai"}))
    result = await news_processor.process(NewsItem(
        source="test", title="测试股份：通过客户认证为不实信息"))
    assert result["sentiment"] == "neutral" and result["research_only"]
    assert result["events"][0]["type"] == "research_context"
    assert "已完成收购" not in result["summary"]
    assert result["model_hypotheses"]["summary"] == MODEL["summary"]


@pytest.mark.asyncio
async def test_legacy_ai_result_veto_uses_only_visible_original_no_history_edit(db):
    session, clock = db
    raw = NewsItem(source="cls", source_id="legacy-analysis",
                   title="测试股份：产品通过认证", content="该信息不属实。",
                   publish_time=T0 - timedelta(hours=1))
    await NewsEngine().cache_raw_items(session, [raw])
    version = await session.scalar(sa.select(NewsContentVersion))
    encoded = json.dumps({**MODEL, "importance": 10})
    attempt = NewsAnalysisVersion(
        content_version_id=version.id, status="analyzed",
        analysis_completed_at=T0, available_at=T0,
        result_json=encoded, result_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        protocol_version="news_pit_v1")
    session.add(attempt)
    await session.commit()
    output = (await load_news_evidence_as_of(session, as_of_at=T0))[0]
    assert output.research_only and output.related_codes == ["600001"]
    assert output.related_sectors == [] and output.sentiment == "neutral"
    assert attempt.result_json == encoded
    assert await load_direct_stock_catalyst_map(session, T0.date(), news_end_time=T0, min_score=0) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_role", [3, "not-a-list", None])
async def test_malformed_analysis_roles_do_not_leak_partially_copied_bullish_fields(db, bad_role):
    session, clock = db
    raw = NewsItem(source="cls", source_id="bad-role", title="测试股份发布公告",
                   content="测试股份公布董事会议案。", publish_time=T0 - timedelta(minutes=5))
    await NewsEngine().cache_raw_items(session, [raw])
    version = await session.scalar(sa.select(NewsContentVersion))
    result = {**MODEL, "importance": 10,
              "role_evidence": {"protocol": "news_roles_v1", "entities": bad_role}}
    encoded = json.dumps(result)
    session.add(NewsAnalysisVersion(
        content_version_id=version.id, status="analyzed",
        analysis_completed_at=T0, available_at=T0,
        result_json=encoded, result_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        protocol_version="news_pit_v1"))
    await session.commit()
    view = (await load_news_evidence_as_of(session, as_of_at=T0))[0]
    assert view.analysis_version_id is None
    assert view.sentiment == "neutral" and view.bull_bear_confidence == 0
    assert view.summary == "" and view.importance == 5
    assert view.related_sectors == [] and view.related_codes == ["600001"]
