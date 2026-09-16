"""静默失效告警测试（D3）。

背景（2026-09-16 复盘）：13 个模拟账户中 9 个当日 0 成交，原因全部是数据质量类阻断，
但系统没有任何告警 —— 只留下决策日志，事后靠人工翻 43,956 行才发现。

这些用例锁定告警的**边界**：只报「0 成交 且 原因为数据质量类」，
不把正常空仓、仓位上限拦截、买入窗口未开误报成故障。
"""
from datetime import date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.paper import PaperAutoTradeLog
from app.models.review import ReviewAutomationAlert, ReviewAutomationRun
from app.review.automation import (
    _DATA_QUALITY_REASONS,
    _DATA_QUALITY_STAGES,
    alert_silent_data_quality_blockage,
)

TRADE_DATE = date(2026, 9, 16)

# 项目未开启 asyncio_mode=auto，异步用例需显式标记
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'silent.db'}", future=True
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with maker() as db:
        yield db
    await engine.dispose()


async def _run(db: AsyncSession, phase: str = "postmarket") -> ReviewAutomationRun:
    run = ReviewAutomationRun(
        run_key="k-" + phase,
        logical_key="l-" + phase,
        job_name="daily_review_snapshot",
        review_date=TRADE_DATE,
        phase=phase,
        trigger="schedule",
        attempt=1,
        status="completed",
        started_at=datetime.now(),
    )
    db.add(run)
    await db.flush()
    return run


def _log(account_id: int, *, action: str, decision: str,
         stage_code: str | None = None, reason_code: str | None = None,
         trade_date: date = TRADE_DATE) -> PaperAutoTradeLog:
    return PaperAutoTradeLog(
        run_id=f"paper-auto-{account_id}",
        trade_date=trade_date,
        created_at=datetime.now(),
        action=action,
        decision=decision,
        account_id=account_id,
        stage_code=stage_code,
        reason_code=reason_code,
    )


async def _alert_rows(db: AsyncSession) -> list[ReviewAutomationAlert]:
    return list(
        (
            await db.scalars(
                select(ReviewAutomationAlert).where(
                    ReviewAutomationAlert.alert_type
                    == "account_silent_data_quality_blockage"
                )
            )
        ).all()
    )


# ── 该报的必须报 ──────────────────────────────────────────────────────


async def test_alerts_account_blocked_by_data_gate(session: AsyncSession):
    """0 成交 + data_gate/candidate_data_missing 必须告警。"""
    run = await _run(session)
    session.add_all([
        _log(2, action="skip_buy", decision="skipped",
             stage_code="data_gate", reason_code="candidate_data_missing"),
        _log(2, action="skip_buy", decision="skipped",
             stage_code="data_gate", reason_code="candidate_data_missing"),
    ])
    await session.flush()

    alerts = await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    )

    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert alerts[0].review_date == TRADE_DATE
    assert alerts[0].phase == "postmarket"
    assert "账户 2" in alerts[0].message
    assert "0 成交" in alerts[0].message


async def test_alert_evidence_records_top_reasons(session: AsyncSession):
    """告警必须带可检索的证据，而不是只有一句文案。"""
    import json

    run = await _run(session)
    session.add_all([
        _log(3, action="skip_buy", decision="skipped",
             stage_code="data_gate", reason_code="candidate_data_missing"),
        _log(3, action="skip_buy", decision="skipped",
             stage_code="candidate_scan", reason_code="data_quality"),
    ])
    await session.flush()

    alerts = await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    )
    evidence = json.loads(alerts[0].evidence_json)

    assert evidence["account_id"] == 3
    assert evidence["executed_buys"] == 0
    assert evidence["data_quality_blocks"] == 2
    assert evidence["total_logs"] == 2
    reasons = {(r["stage_code"], r["reason_code"]) for r in evidence["top_blocking_reasons"]}
    assert ("data_gate", "candidate_data_missing") in reasons
    assert ("candidate_scan", "data_quality") in reasons


async def test_alerts_each_blocked_account_independently(session: AsyncSession):
    """每个被阻断账户各出一条告警，不合并。"""
    import json

    run = await _run(session)
    for account_id in (5, 10, 13):
        session.add(
            _log(account_id, action="skip_buy", decision="skipped",
                 stage_code="data_gate", reason_code="candidate_data_missing")
        )
    await session.flush()

    alerts = await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    )
    assert len(alerts) == 3
    assert sorted(json.loads(a.evidence_json)["account_id"] for a in alerts) == [5, 10, 13]


# ── 不该报的一律不报 ──────────────────────────────────────────────────


async def test_no_alert_when_account_executed(session: AsyncSession):
    """当日有成交的账户不告警，即使同时存在数据质量阻断记录。"""
    run = await _run(session)
    session.add_all([
        _log(2, action="buy", decision="executed"),
        _log(2, action="skip_buy", decision="skipped",
             stage_code="data_gate", reason_code="candidate_data_missing"),
    ])
    await session.flush()

    assert await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    ) == []


async def test_no_alert_for_non_quality_blocking_reason(session: AsyncSession):
    """0 成交但原因是仓位上限/窗口未开等非数据质量因素时，不得误报。"""
    run = await _run(session)
    session.add_all([
        _log(9, action="skip_buy", decision="skipped",
             stage_code="entry_decision", reason_code="position_limit"),
        _log(9, action="skip_buy", decision="skipped",
             stage_code="entry_decision", reason_code="entry_constraint_empty"),
    ])
    await session.flush()

    assert await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    ) == []


async def test_no_alert_for_account_without_logs(session: AsyncSession):
    """当日完全没有日志的账户不告警（无法判定为数据质量阻断）。"""
    run = await _run(session)
    session.add(_log(2, action="skip_buy", decision="skipped",
                     stage_code="data_gate", reason_code="candidate_data_missing"))
    await session.flush()

    alerts = await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    )
    assert len(alerts) == 1
    assert "账户 2" in alerts[0].message


async def test_no_alert_for_other_trade_date(session: AsyncSession):
    """只评估目标交易日，前一天的数据不得混入。"""
    run = await _run(session)
    session.add(
        _log(7, action="skip_buy", decision="skipped",
             stage_code="data_gate", reason_code="candidate_data_missing",
             trade_date=date(2026, 9, 15))
    )
    await session.flush()

    assert await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    ) == []


# ── 幂等 ──────────────────────────────────────────────────────────────


async def test_alerts_are_idempotent(session: AsyncSession):
    """重复运行（retry/replay）不得重复插入同一告警。"""
    run = await _run(session)
    session.add(_log(2, action="skip_buy", decision="skipped",
                     stage_code="data_gate", reason_code="candidate_data_missing"))
    await session.flush()

    first = await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    )
    second = await alert_silent_data_quality_blockage(
        session, run=run, review_date=TRADE_DATE
    )

    assert len(first) == 1
    assert len(second) == 1
    total = await session.scalar(
        select(func.count()).select_from(ReviewAutomationAlert)
    )
    assert total == 1, "重复调用插入了重复告警"


# ── 判据本身 ──────────────────────────────────────────────────────────


async def test_criteria_are_stage_code_based_not_text_matching():
    """判据必须基于 stage_code/reason_code，不得依赖中文文案。

    文案会随产品措辞变化，码才是稳定契约。
    """
    assert "data_gate" in _DATA_QUALITY_STAGES
    assert "candidate_data_missing" in _DATA_QUALITY_REASONS
    assert "data_quality" in _DATA_QUALITY_REASONS
    assert "auction_quality" in _DATA_QUALITY_REASONS
    for value in (*_DATA_QUALITY_STAGES, *_DATA_QUALITY_REASONS):
        assert value.isascii(), f"判据里混入非 ASCII 值：{value}"
