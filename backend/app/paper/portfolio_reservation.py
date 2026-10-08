"""SQLite BUY reservation ownership, separate from fill/ledger authorization.

The service holds the database writer from fresh budget reads to the durable
pending order. Account projection helpers may flush, but cannot release it.
This scope grants no permission to call a broker or book a trade.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import text

_OWNER = ContextVar("shared_portfolio_reservation", default=None)


def reservation_active(db):
    owner = _OWNER.get()
    if owner is None:
        return False
    if owner != (db, asyncio.current_task()):
        raise RuntimeError("shared reservation cannot cross task/session boundaries")
    return True


async def bind_reservation_clock(db, cmd):
    """Bind a NEW reservation to the real service clock and committed quote.

    Called after the existing-order idempotency read, never for a pending fill.
    Source confirmation/expiry clocks stay immutable. The next-round gate uses
    this accepted clock, not a caller-provided historical decision timestamp.
    """
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.api.v1 import paper
    from app.data.fund_flow_clock import evidence_clock
    from app.models.stock import QuoteRound

    def rejected(reason):
        raise HTTPException(status_code=409, detail="共享预约时钟无效: " + reason)

    if not reservation_active(db):
        raise RuntimeError("reservation clock requires the owned SQLite writer")
    context = paper._quote_round_context()
    round_id = str(context.get("round_id") or "")
    asof = evidence_clock(context.get("as_of_at"))
    committed = evidence_clock(context.get("committed_at"))
    requested = evidence_clock(cmd.decision_at)
    initial = evidence_clock(paper._public_order_clock())
    config = str(context.get("config_version") or "")
    code = str(context.get("code_version") or "")
    if (not round_id or context.get("quality_status") != "ok"
            or not config or not code or cmd.decision_round_id != round_id
            or cmd.config_version != config or cmd.code_version != code
            or asof is None or evidence_clock(cmd.as_of_at) != asof
            or committed is None or requested is None or initial is None):
        rejected("缺少当前健康行情或轮次/水位/版本不一致")
    if not (asof <= committed <= requested <= initial
            and asof.date() == initial.date()):
        rejected("请求时间早于行情提交、来自未来或跨日")
    with db.no_autoflush:
        row = await db.scalar(select(QuoteRound).where(
            QuoteRound.round_id == round_id).execution_options(populate_existing=True))
    if (row is None or row.source != "tencent" or row.quality_status != "ok"
            or row.trade_date != initial.date()
            or evidence_clock(row.as_of_at) != asof
            or evidence_clock(row.committed_at) != committed
            or row.config_version != config or row.code_version != code):
        rejected("真实已提交行情清单缺失或与当前上下文不一致")

    # Resample after the awaited read; never manufacture a fresh quote/TTL.
    accepted = evidence_clock(paper._public_order_clock())
    current = paper._quote_round_context()
    if (accepted is None or accepted < initial or accepted.date() != initial.date()
            or current.get("round_id") != round_id or current.get("quality_status") != "ok"
            or evidence_clock(current.get("as_of_at")) != asof
            or evidence_clock(current.get("committed_at")) != committed
            or current.get("config_version") != config or current.get("code_version") != code):
        rejected("校验期间真实时钟回拨/跨日或行情轮次变化")
    max_age = max(1, int(paper.settings.PAPER_EXECUTION_QUOTE_MAX_AGE_SEC))
    if not 0 <= (accepted - asof).total_seconds() <= max_age:
        rejected("当前行情已经过期")
    cmd.decision_at = accepted
    proof = {"schema": "shared_reservation_clock_v1",
             "requested_at": requested.isoformat(), "accepted_at": accepted.isoformat(),
             "quote_round_id": round_id, "quote_as_of_at": asof.isoformat(),
             "quote_committed_at": committed.isoformat(),
             "config_version": config, "code_version": code}
    for metadata in (cmd.deferred_metadata, cmd.queue_metadata):
        if isinstance(metadata, dict):
            metadata["portfolio_reservation_clock"] = dict(proof)


@asynccontextmanager
async def shared_order_reservation(db):
    from app.trading.paper_authorization import paper_transaction_active
    if _OWNER.get() is not None or paper_transaction_active(db):
        raise RuntimeError("shared new-order reservation cannot nest inside execution")
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("shared reservation requires a clean owned session")
    if db.get_bind().dialect.name != "sqlite":
        raise RuntimeError("shared reservation currently requires SQLite writer serialization")
    # Drop a previous read/preflight snapshot; no accepted budget is carried over.
    await db.commit()
    token = None
    try:
        await db.execute(text("BEGIN IMMEDIATE"))
        token = _OWNER.set((db, asyncio.current_task()))
        yield
        # submit_order commits its order outcome. This releases a trailing SELECT
        # transaction only, not an earlier partially reserved promise.
        await db.commit()
    except BaseException as exc:
        from app.trading.paper_authorization import mark_paper_execution_uncertain
        mark_paper_execution_uncertain(exc)
        try:
            await db.rollback()
        except BaseException as cleanup:
            mark_paper_execution_uncertain(cleanup)
            raise cleanup from exc
        raise
    finally:
        if token is not None:
            _OWNER.reset(token)
