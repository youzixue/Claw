"""Process-local call-chain guard, not a sandbox against hostile Python code.

Only the trading service opens a scope after risk/matching. Broker and ledger
each consume one stage, bound to the same task, DB and exact request values.
Metadata contexts (strategy/round/Challenger) are never execution authorization.
"""
import asyncio
import hashlib
import json
from datetime import datetime, time, timedelta
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException

_SCOPE = ContextVar("paper_execution_scope", default=None)
_PAPER_TRANSACTION = ContextVar("paper_order_transaction", default=None)
PAPER_ORDER_CHECKPOINT_FIELDS = (
    "id", "order_id", "status", "filled_quantity", "avg_fill_price",
    "last_fill_round_id", "quantity", "order_type", "price", "side",
    "code", "broker", "account_id", "strategy_id", "strategy_version",
    "signal_id", "source", "reason", "risk_level", "risk_json",
    "idempotency_key", "decision_round_id", "decision_at", "as_of_at",
    "trade_date", "config_version", "code_version",
)


def paper_order_checkpoint(order):
    """Immutable owned leaves, not an ORM object or a new execution authority."""
    return tuple((name, getattr(order, name)) for name in PAPER_ORDER_CHECKPOINT_FIELDS)


def mark_paper_execution_uncertain(exc):
    # Preserve original exception type for cancellation/SQLite handling. This is
    # a process-local failure annotation, NOT a persisted claim that a fill exists.
    exc._paper_execution_requires_reconciliation = True
    return exc


def paper_execution_requires_reconciliation(exc):
    return getattr(exc, "_paper_execution_requires_reconciliation", False) is True


def paper_transaction_active(db):
    """Account helpers may flush, never commit, while this task owns a fill."""
    owner = _PAPER_TRANSACTION.get()
    if owner is None:
        return False
    if owner != (db, asyncio.current_task()):
        raise HTTPException(403, "模拟成交事务不允许跨任务或数据库会话复用")
    return True


@asynccontextmanager
async def _paper_order_transaction(db, *, enabled=True, order=None):
    """One new fill: ledger, entry-fee evidence, receipt and order commit together.

    Checkpoint BEFORE waiting for the Python lock: an already-flushed order must
    not hold SQLite's writer lock while another fill needs it to finish. This
    checkpoint contains preflight/order audit only, never a new booked fill.
    No nested SAVEPOINT: SQLite legacy transaction mode can commit on RELEASE.
    """
    if not enabled:
        yield
        return
    if _PAPER_TRANSACTION.get() is not None:
        raise HTTPException(403, "禁止嵌套模拟成交事务")
    from app.api.v1 import paper
    expected = paper_order_checkpoint(order) if order is not None else None
    try:
        await db.commit()
        async with paper._TRADE_LOCK:
            token = _PAPER_TRANSACTION.set((db, asyncio.current_task()))
            try:
                if order is not None:
                    await db.refresh(order)
                    if paper_order_checkpoint(order) != expected:
                        raise HTTPException(409, "等待成交锁期间委托已变化，须重新读取而非沿用旧撮合")
                yield expected
                # A lost acknowledgement may follow a successful COMMIT. The
                # caller must propagate, not overwrite a durable fill as rejected.
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            finally:
                _PAPER_TRANSACTION.reset(token)
    except BaseException as exc:
        # Also release preflight work if checkpoint or lock acquisition fails.
        # Upstream must not translate an uncertain commit into a fabricated refusal.
        mark_paper_execution_uncertain(exc)
        try:
            await db.rollback()
        except BaseException as cleanup_error:
            mark_paper_execution_uncertain(cleanup_error)
            raise
        raise


@asynccontextmanager
async def paper_ledger_section(db):
    """The service, not the raw book helper, owns the lock through final commit."""
    _current(db, "ledger")
    if not paper_transaction_active(db):
        raise HTTPException(403, "模拟落账缺少统一交易服务的原子事务")
    yield


async def finish_account_write(db):
    if paper_transaction_active(db):
        await db.flush()
    else:
        await db.commit()


def _request_values(req):
    return tuple(getattr(req, name) for name in req.__dataclass_fields__)


@dataclass
class _Scope:
    db: object
    request: object
    values: tuple
    task: object
    stage: str = "issued"
    immediate_evidence_json: str = ""
    lock_checked_at: datetime | None = None
    ledger_timing: dict | None = None


@contextmanager
def _paper_execution_scope(db, req, *, immediate_evidence_json=""):
    if not isinstance(immediate_evidence_json, str):
        raise HTTPException(403, "模拟成交依据必须为服务冻结的JSON文本")
    scope = _Scope(db, req, _request_values(req), asyncio.current_task(),
                   immediate_evidence_json=immediate_evidence_json)
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        scope.stage = "closed"
        _SCOPE.reset(token)


def _current(db, stage):
    scope = _SCOPE.get()
    if (scope is None or scope.db is not db or scope.stage != stage
            or scope.task is not asyncio.current_task()
            or scope.values != _request_values(scope.request)):
        raise HTTPException(403, "模拟成交只能由统一交易服务已验收的请求落账")
    return scope


def authorize_broker_request(db, req):
    scope = _current(db, "issued")
    if scope.request is not req:
        raise HTTPException(403, "模拟成交请求与交易服务授权不一致")
    scope.stage = "broker"


def authorize_ledger_request(db, req, *, side, account_name):
    scope = _current(db, "broker")
    original = scope.request
    if (original.side != side or original.account_name != account_name
            or original.code != req.code or original.price != req.price
            or original.quantity != req.amount
            or (original.signal_id or original.order_id) != req.signal_id
            or original.reason != req.reason
            or (side == "buy" and any(
                getattr(original, field) != getattr(req, field)
                for field in ("stop_loss_price", "entry_sector_code", "entry_sector_name")
            ))):
        raise HTTPException(403, "模拟落账参数与已授权成交请求不一致")
    scope.stage = "ledger"


def _local_clock(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("invalid_local_clock")
    return value


def validate_ledger_clock(db, *, phase):
    """Synchronous final guard: no await may separate before_mutation from mutation.

    Rechecks the ORIGINAL accepted snapshot timing, not a new book/identity/PIT
    certificate. The legacy JSON argument name now carries an explicitly typed
    immediate OR pending timing contract, never an optional real-fill bypass.
    """
    scope = _current(db, "ledger")
    if not scope.immediate_evidence_json:
        raise HTTPException(409, "模拟落账缺少服务端冻结的成交时钟合同")
    from app.api.v1 import paper
    attempted = None
    try:
        attempted = paper._public_order_clock()
        now = _local_clock(attempted)
        p = json.loads(scope.immediate_evidence_json)
        req = scope.request
        if not isinstance(p, dict):
            raise ValueError("invalid_execution_contract")
        pending = p.get("contract_version") == "pending_paper_fill_timing_v1_20260914"
        immediate = p.get("contract_version") == "immediate_paper_fill_v2_20260914"
        if (not (pending or immediate)
                or p.get("status") != ("validated" if pending else "fillable")
                or p.get("mandatory") is not True
                or p.get("code") != req.code or p.get("side") != req.side
                or p.get("account_id") != req.account_name
                or p.get("fill_price") != req.price
                or type(p.get("filled_quantity")) is not int or p["filled_quantity"] != req.quantity
                or not req.fill_round_id or p.get("quote_round_id") != req.fill_round_id
                or (immediate and req.fill_round_id != req.decision_round_id)
                or (pending and (not req.decision_round_id or req.fill_round_id == req.decision_round_id
                    or p.get("decision_round_id") != req.decision_round_id
                    or p.get("request_id") != req.order_id))):
            raise ValueError("frozen_execution_identity_mismatch")
        source, received, committed, decision, dispatch, expiry, end = [
            _local_clock(p.get(key)) for key in ("source_quote_at", "received_at",
                "quote_committed_at", "decision_at", "dispatch_validated_at",
                "quote_expires_at", "session_end_at")]
        max_age = p.get("quote_max_age_sec")
        expected_end = datetime.combine(dispatch.date(),
            time(11,30) if dispatch.time() < time(11,30) else time(14,57))
        if pending:
            evaluation = _local_clock(p.get("evaluated_at"))
            evaluation_end = datetime.combine(evaluation.date(),
                time(11, 30) if evaluation.time() < time(11, 30) else time(14, 57))
            if (not decision < committed <= evaluation <= dispatch < evaluation_end
                    or not (time(9, 30) <= evaluation.time() < time(11, 30)
                            or time(13) <= evaluation.time() < time(14, 57))
                    or evaluation.date() != dispatch.date()
                    or p.get("execution_kind") not in ("deferred", "limit_up_queue")
                    or type(p.get("buy_validity_required")) is not bool):
                raise ValueError("invalid_pending_timing_contract")
            if p["buy_validity_required"]:
                confirmed = _local_clock(p.get("buy_confirmed_at"))
                buy_expiry = _local_clock(p.get("buy_expires_at"))
                ttl = p.get("buy_max_execution_delay_sec")
                if (type(ttl) not in (int, float) or ttl <= 0
                        or buy_expiry != confirmed + timedelta(seconds=ttl)
                        or confirmed.date() != decision.date() or confirmed > decision
                        or now > buy_expiry):
                    raise ValueError("original_buy_confirmation_expired_or_invalid")
            if p["execution_kind"] == "limit_up_queue":
                cancel_at = _local_clock(p.get("queue_cancel_at"))
                if req.side != "buy" or cancel_at.date() != dispatch.date() or now > cancel_at:
                    raise ValueError("original_queue_cancel_deadline_passed")
            elif p.get("queue_cancel_at") is not None:
                raise ValueError("unexpected_queue_deadline")
        if (type(max_age) is not int or max_age < 1
                or expiry != source + timedelta(seconds=max_age) or end != expected_end
                or req.filled_at != dispatch
                or not source <= received <= committed <= dispatch <= now
                or (immediate and not committed <= decision <= dispatch)
                or any(at.date() != dispatch.date() for at in (source, received, committed, decision, now))
                or not (time(9,30) <= dispatch.time() < time(11,30)
                        or time(13) <= dispatch.time() < time(14,57))
                or now > expiry or now >= end):
            raise ValueError("expired_rolled_back_or_outside_original_session")
        if phase == "lock_acquired":
            if scope.lock_checked_at is not None:
                raise ValueError("lock_clock_already_consumed")
            scope.lock_checked_at = now
        elif phase == "before_mutation":
            if scope.lock_checked_at is None or now < scope.lock_checked_at or scope.ledger_timing is not None:
                raise ValueError("missing_repeated_or_rolled_back_lock_clock")
            scope.ledger_timing = {
                "guard_version": "paper_ledger_timing_v1_20260914", "status": "validated",
                "scope": "original_accepted_quote_timing_not_book_refresh_or_physical_commit",
                "dispatch_validated_at": dispatch.isoformat(),
                "lock_acquired_checked_at": scope.lock_checked_at.isoformat(),
                "before_mutation_checked_at": now.isoformat(),
                "quote_round_id": req.fill_round_id, "quote_expires_at": expiry.isoformat(),
                "session_end_at": end.isoformat(), "physical_commit_at": None,
                "input_contract_version": p["contract_version"],
                "input_sha256": hashlib.sha256(scope.immediate_evidence_json.encode()).hexdigest(),
            }
        else:
            raise ValueError("invalid_ledger_clock_phase")
        return now
    except (ValueError, TypeError, OverflowError) as exc:
        observed = attempted.isoformat() if isinstance(attempted, datetime) else "invalid"
        raise HTTPException(409, f"模拟落账时钟验收失败[{phase}] at={observed}: {exc}") from None


def ledger_timing_evidence(db):
    """Return the owned terminal clock check, never another authority context."""
    scope = _SCOPE.get()
    if scope is None or not scope.immediate_evidence_json:
        return None
    scope = _current(db, "ledger")
    if scope.ledger_timing is None:
        raise HTTPException(409, "模拟落账缺少变更前时钟验收，不得产生回报")
    return dict(scope.ledger_timing)
