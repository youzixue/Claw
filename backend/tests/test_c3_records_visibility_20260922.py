"""Offline only: in-memory SQLite, no scanner, notification or app lifespan."""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.paper import PaperAutoTradeLog, PaperShadowEvent
from app.paper.c3_records import SNAPSHOT_MAX_CHARS, list_c3_records
from app.paper.strategy_iteration_shadow import ROUTE_C3, route_version_for


def record(i, **changes):
    fields = dict(
        id=i, event_key=f"offline-event-{i}", route_id=ROUTE_C3,
        route_version="old-offline-version", trade_date=date(2026, 9, 22),
        observed_at=datetime(2026, 9, 22, 10), code=f"{i:06d}", name=f"测试{i}",
        event_type="confirmed", status="confirmed", price=12.34,
        snapshot_json=json.dumps({
            "prior_structure": {"reason": "原始原因", "private": "must-not-leak",
                                "confirmation": {"ready": True, "sample_count": 3}},
            "secret": "must-not-leak",
        }),
    )
    fields.update(changes)
    return PaperShadowEvent(**fields)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(PaperShadowEvent.__table__.create)
        await conn.run_sync(PaperAutoTradeLog.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_where_before_limit_real_total_versions_and_no_autoflush(db):
    db.add_all([record(i) for i in range(1, 81)])
    db.add(record(81, route_version=route_version_for(ROUTE_C3)))
    db.add(record(82, route_id="other-route"))
    await db.commit()
    statements = []
    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)
    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        pending = record(999)  # would be inserted by implicit ORM autoflush
        db.add(pending)
        first = await list_c3_records(db, page_size=20)
        assert first["total"] == 81
        assert [r["id"] for r in first["items"]] == list(range(81, 61, -1))
        assert first["items"][0]["reason"] == "原始原因"
        assert first["items"][0]["confirmed_price"] == 12.34
        assert "snapshot_json" not in first["items"][0]
        assert "private" not in json.dumps(first, default=str)
        assert all(not r["execution_signal"] for r in first["items"])
        second = await list_c3_records(db, page=5, page_size=20)
        assert second["total"] == 81 and [r["id"] for r in second["items"]] == [1]
        assert (await list_c3_records(db, keyword="000001"))["total"] == 1
        assert (await list_c3_records(db, keyword="测试1"))["total"] == 11
        assert (await list_c3_records(db, keyword="%"))["total"] == 0
        assert (await list_c3_records(db, keyword="_"))["total"] == 0
        assert (await list_c3_records(db, version="current"))["total"] == 1
        assert (await list_c3_records(db, trade_date=date(2026, 9, 21)))["total"] == 0
        assert pending in db.new
        assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
        page_sql = next(s for s in statements if "LIMIT" in s)
        assert page_sql.index("WHERE") < page_sql.index("LIMIT")
        assert "snapshot_json" not in page_sql
        snapshot_sql = next(s for s in statements if "bounded_snapshot" in s)
        assert "CASE WHEN" in snapshot_sql and "WHERE paper_shadow_event.id IN" in snapshot_sql
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_all_failed_events_kept_and_bounded_json(db):
    types = ["eligible", "confirmation_reset", "coverage_blocked", "evidence_blocked",
             "session_blocked", "outcome_blocked", "session_outcome"]
    db.add_all([record(i, event_type=kind, status="failed") for i, kind in enumerate(types, 1)])
    db.add(record(8, snapshot_json="{" + "x" * SNAPSHOT_MAX_CHARS))
    db.add(record(9, snapshot_json="{bad"))
    db.add(record(10, snapshot_json="[]"))
    db.add(record(11, snapshot_json=json.dumps({"prior_structure": {"reason": {"unexpected": 1}}})))
    await db.commit()
    assert (await list_c3_records(db, event_type="all"))["total"] == 11
    assert (await list_c3_records(db, event_type="eligible"))["total"] == 1
    assert (await list_c3_records(db, event_type="reset"))["total"] == 1
    assert (await list_c3_records(db, event_type="block"))["total"] == 4
    rows = {r["id"]: r for r in (await list_c3_records(db))["items"]}
    assert rows[8]["snapshot_status"] == "oversized"
    assert rows[9]["snapshot_status"] == rows[10]["snapshot_status"] == "invalid"
    assert rows[11]["reason"] is None
    assert "unexpected" not in json.dumps(rows, default=str)


@pytest.mark.asyncio
async def test_endpoint_query_contract_and_validation(db):
    # Mount only the existing router, no main app startup or scheduler.
    from app.api.v1.paper import router
    from app.db.session import get_db
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/paper")
    async def override():
        yield db
    app.dependency_overrides[get_db] = override
    db.add(record(1))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
        path = "/api/v1/paper/research/c3/events"
        response = await client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["filters"]["version"] == "all"
        assert body["filters"]["event_type"] == "confirmed"
        assert body["total"] == 1 and body["items"][0]["id"] == 1
        for query in ("page=0", "page_size=101", "page=1000001", "version=old",
                      "event_type=unknown", "trade_date=bad", "keyword=" + "x" * 81):
            assert (await client.get(path + "?" + query)).status_code == 422


@pytest.mark.asyncio
async def test_frozen_confirmed_examples_remain_queryable_across_versions(db):
    # Pre-existing frozen extract is evidence, not a full-market denominator.
    path = Path(__file__).resolve().parents[2] / "outputs/close_review_20260922/supplement/shadow_confirmed.jsonl"
    assert path.exists(), "Required pre-existing frozen evidence missing"
    with path.open() as source:
        for line in source:
            item = json.loads(line)
            if item["route_id"] != ROUTE_C3:
                continue
            item["trade_date"] = date.fromisoformat(item["trade_date"])
            item["observed_at"] = datetime.fromisoformat(item["observed_at"])
            if item.get("created_at"):
                item["created_at"] = datetime.fromisoformat(item["created_at"])
            db.add(PaperShadowEvent(**item))
    await db.commit()
    for code in ("605398", "002768", "603039", "002313", "600418"):
        result = await list_c3_records(db, trade_date=date(2026, 9, 22), keyword=code)
        assert result["total"] >= 1, code
        row = result["items"][0]
        assert row["code"] == code and row["id"] > 0
        assert row["event_key"] and row["route_version"]
        assert row["confirmed_price"] == row["price"]


@pytest.mark.asyncio
async def test_complete_baseline_228_pagination_filters_and_version_rotation(db, monkeypatch):
    root = Path(__file__).resolve().parents[2] / "outputs/c3_delivery_visibility_20260922/baseline"
    raw = (root / "c3_confirmed.jsonl").read_bytes()
    meta = json.loads((root / "c3_confirmed.meta.json").read_text())
    assert meta["complete"] and meta["rows"] == meta["total_rows"] == 228
    assert hashlib.sha256(raw).hexdigest() == meta["sha256"]
    source_rows = [json.loads(line) for line in raw.splitlines()]
    assert len(source_rows) == 228
    counts = [json.loads(line) for line in (root / "c3_counts.jsonl").read_text().splitlines()]
    assert len(counts) == 7
    assert sum(row["count"] for row in counts if row["event_type"] == "confirmed") == 228
    for source in source_rows:
        item = dict(source)
        for key in ("observed_at", "created_at"):
            item[key] = datetime.fromisoformat(item[key])
        item["trade_date"] = date.fromisoformat(item["trade_date"])
        # This leaf-only baseline intentionally has no snapshot; do not invent it.
        db.add(PaperShadowEvent(**item))
    await db.commit()
    from app.paper import strategy_iteration_shadow as shadow
    baseline_version = source_rows[0]["route_version"]
    monkeypatch.setattr(shadow, "route_version_for", lambda route: baseline_version)
    day = date(2026, 9, 22)
    expected = sorted(source_rows, key=lambda row: (row["observed_at"], row["id"]), reverse=True)
    for size in (20, 50, 100):
        collected = []
        for page in range(1, (228 + size - 1) // size + 1):
            result = await list_c3_records(db, trade_date=day, page=page, page_size=size)
            assert result["total"] == 228
            assert len(result["items"]) <= size
            collected.extend(row["id"] for row in result["items"])
        assert collected == [row["id"] for row in expected]
        assert len(set(collected)) == 228  # no duplicate/drop at page boundaries
    for code in ("605398", "002768", "603039", "002313", "600418"):
        matching = [row for row in source_rows if row["code"] == code]
        assert matching
        for keyword in (code, matching[0]["name"]):
            result = await list_c3_records(db, trade_date=day, keyword=keyword)
            assert result["total"] == len(matching)
            actual = result["items"][0]
            original = next(row for row in matching if row["id"] == actual["id"])
            for key in ("id", "event_key", "route_version", "code", "name", "price"):
                assert actual[key] == original[key]
    assert (await list_c3_records(db, trade_date=day, version="current"))["total"] == 228
    monkeypatch.setattr(shadow, "route_version_for", lambda route: "offline-new-version")
    assert (await list_c3_records(db, trade_date=day, version="current"))["total"] == 0
    assert (await list_c3_records(db, trade_date=day))["total"] == 228
    assert (await list_c3_records(db, trade_date=date(2026, 9, 21)))["total"] == 0


@pytest.mark.asyncio
async def test_delivery_receipts_read_only_join_and_missing_history(db):
    statuses = ("sent", "failed", "expired", "rejected", "throttled", "disabled", "attempting", "pending")
    for i, status in enumerate(statuses, 1):
        row = record(i)
        db.add(row)
        payload = {
            "shadow_event_key": row.event_key, "shadow_event_id": i,
            "signal_observed_at": "2026-09-22T10:00:00",
            "checked_at": "2026-09-22T10:00:01", "cause": "frozen_test_cause",
            "send_started_at": "2026-09-22T10:00:02" if status == "sent" else None,
            "send_completed_at": "2026-09-22T10:00:03" if status == "sent" else None,
            "private": "must-not-leak",
        }
        db.add(PaperAutoTradeLog(
            account_id=None,
            run_id="c3notify:" + hashlib.sha256(row.event_key.encode()).hexdigest()[:30],
            trade_date=date(2026, 9, 22), created_at=datetime(2026, 9, 22, 10),
            source=ROUTE_C3,
            stage_code="c3_research_signal" if status == "pending" else "c3_research_delivery",
            action="research_signal" if status == "pending" else "research_push",
            decision="confirmed" if status == "pending" else status,
            candidate_json=json.dumps(payload),
        ))
    db.add(record(20))
    db.add(record(21, event_type="eligible", status="eligible"))
    await db.commit()
    statements = []
    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)
    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        db.add(record(999))
        result = await list_c3_records(db, event_type="all")
        rows = {row["id"]: row for row in result["items"]}
        for i, status in enumerate(statuses, 1):
            assert rows[i]["notification"]["status"] == status
            assert rows[i]["notification"]["shadow_event_id"] == i
            assert rows[i]["notification"]["user_received_at"] is None
        assert rows[1]["notification"]["send_started_at"] == "2026-09-22T10:00:02"
        assert rows[1]["observed_at"] == datetime(2026, 9, 22, 10)
        assert rows[20]["notification"]["status"] == "not_queued"
        assert rows[21]["notification"]["status"] == "not_applicable"
        assert "must-not-leak" not in json.dumps(result, default=str)
        assert len(db.new) == 1
        assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)

