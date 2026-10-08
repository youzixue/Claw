"""Fragment receipt STRUCTURE only: fake book rows, no broker/permit/source or cash."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, select, text, update, func
from sqlalchemy.exc import IntegrityError
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.session import Base
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import (TradeOrder, TradeFill,
    PaperAfterHoursResourceScope as State, PaperAfterHoursResourceReceipt as Receipt)
from app.data.after_hours import _json
from app.trading.paper_after_hours_resource_schema import (
    FULL_PROTOCOL, PARTIAL_PROTOCOL, sqlite_guards)
from test_paper_after_hours_allocation_20261002 import AT, NOW

ORDER = "schema.intent"
KEY = hashlib.sha256(_json(dict(account_numeric_id=7, account_name="default",
    code="600000", trade_date=AT.date().isoformat(), source="schema_fixture",
    source_version="fixture.v1", session_id="fixture.session")).encode()).hexdigest()
TABLES = [PaperAccount.__table__, TradeOrder.__table__, PaperTradeLog.__table__,
          TradeFill.__table__, State.__table__, Receipt.__table__]


@pytest.fixture
def connection():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=TABLES)
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("PRAGMA recursive_triggers=OFF")
        seed(conn)
        yield conn
    engine.dispose()


def seed(conn):
    conn.execute(insert(PaperAccount).values(id=7, account_name="default", status="active",
        initial_capital=50000, current_capital=50000, total_assets=50000))
    conn.execute(insert(TradeOrder).values(id=1, order_id=ORDER, broker="paper",
        account_id="default", order_type="after_hours_fixed", code="600000", side="buy",
        price=10.5, quantity=300, filled_quantity=0, status="submitted", trade_date=AT.date(),
        created_at=AT-timedelta(seconds=1), decision_at=AT-timedelta(seconds=1)))
    conn.execute(insert(State).values(scope_key=KEY, account_numeric_id=7, account_name="default",
        code="600000", trade_date=AT.date(), source="schema_fixture", source_version="fixture.v1",
        session_id="fixture.session", revision=0, terminal_sequence=0, lifecycle_prefix_hash="b"*64,
        source_quote_at=AT, source_available_at=AT, checked_at=NOW))


def header(conn, number=1, *, quantity=100, before=None, payload_changes=None,
           protocol=PARTIAL_PROTOCOL, book_changes=None, finalize=True, checked_at=None,
           serialized_payload=None, order_id=ORDER, original_quantity=300,
           fragment_index=None):
    """Fake grouped row structure; this deliberately does not call a book/ledger."""
    fragment_index = number if fragment_index is None else fragment_index
    before = (fragment_index-1)*100 if before is None else before
    at, request_id = NOW+timedelta(seconds=number-1), "afp-"+f"{number:031x}"
    trade = dict(id=number, account_id=7, code="600000", trade_type="buy", price=10,
        amount=quantity, commission=5, tax=0, trade_time=at)
    fill = dict(id=number, fill_id="fill-"+request_id, order_id=order_id, broker="paper",
        broker_trade_id=str(number), code="600000", side="buy", price=10, quantity=quantity,
        commission=5, tax=0, trade_date=AT.date(), filled_at=at, raw_json="{}")
    if book_changes:
        trade.update(book_changes.get("trade", {}))
        fill.update(book_changes.get("fill", {}))
    conn.execute(insert(PaperTradeLog).values(**trade))
    conn.execute(insert(TradeFill).values(**fill))
    conn.execute(update(State).where(State.scope_key==KEY).values(
        revision=State.revision+1, checked_at=at if checked_at is None else checked_at))
    payload = dict(protocol_version=protocol, scope_key=KEY, order_id=order_id,
        allocation_id=f"{number:064x}", account_numeric_id=7, trade_fill_id=number,
        paper_trade_id=number, request_id=request_id, fragment_index=fragment_index,
        original_quantity=original_quantity, fixed_price=10,
        cumulative_before=before, cumulative_after=before+quantity,
        remaining_quantity_after=original_quantity-before-quantity)
    payload.update(payload_changes or {})
    if callable(serialized_payload):
        serialized_payload = serialized_payload(payload)
    values = dict(allocation_id=f"{number:064x}", scope_key=KEY, order_id=order_id,
        trade_fill_id=number, paper_trade_id=number, protocol_version=protocol,
        payload_json=_json(payload) if serialized_payload is None else serialized_payload,
        content_hash=hashlib.sha256(_json(payload).encode()).hexdigest(), recorded_at=at)
    conn.execute(insert(Receipt).values(**values))
    if finalize:
        conn.execute(update(TradeOrder).where(TradeOrder.order_id==order_id).values(
            filled_quantity=before+quantity,
            status="filled" if before+quantity==original_quantity else "partial"))
    return values


def count(conn, model):
    return conn.scalar(select(func.count()).select_from(model))


def migration(monkeypatch, conn):
    path = Path(__file__).resolve().parents[1]/"alembic/versions/040_after_hours_partial_receipt_guards.py"
    spec = importlib.util.spec_from_file_location("partial_schema_migration", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    context = MigrationContext.configure(conn)
    assert len(result.revision) <= context._version.c.version_num.type.length
    monkeypatch.setattr(result, "op", Operations(context))
    return result


def legacy(conn):
    conn.exec_driver_sql("DROP TRIGGER af_resource_receipt_book_binding")
    for statement in sqlite_guards(include_partial=False):
        conn.exec_driver_sql(statement)


def test_three_immutable_headers_in_existing_table_need_exact_cumulative_cas(connection):
    conn = connection
    assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
    assert conn.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 0
    for number in (1, 2, 3):
        header(conn, number)
    assert count(conn, Receipt) == 3
    assert conn.scalar(select(State.revision)) == 3
    assert conn.scalar(select(TradeOrder.filled_quantity)) == 300
    assert conn.scalar(select(TradeOrder.status)) == "filled"
    for sql in ("DELETE FROM paper_after_hours_resource_receipt WHERE id=1",
                "UPDATE paper_after_hours_resource_receipt SET payload_json='{}' WHERE id=1",
                "DELETE FROM trade_fill WHERE id=1",
                "DELETE FROM paper_trade_log WHERE id=1"):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.exec_driver_sql(sql)
    assert count(conn, Receipt) == count(conn, TradeFill) == 3


@pytest.mark.parametrize("key,value", [
    ("fragment_index", 2), ("fragment_index", 1.0), ("fragment_index", True),
    ("cumulative_before", 100), ("cumulative_before", False),
    ("cumulative_after", 200), ("remaining_quantity_after", 100),
    ("original_quantity", 300.0), ("original_quantity", 100),
    ("fixed_price", True), ("fixed_price", "10"), ("fixed_price", 11),
    ("trade_fill_id", 1.0), ("paper_trade_id", True), ("account_numeric_id", 7.0),
    ("scope_key", "d"*64), ("order_id", "other"), ("allocation_id", "e"*64),
    ("request_id", "afp-"+"g"*31), ("request_id", "afp-"+"1"*32),
    ("request_id", "afp-"+"2"*31), ("protocol_version", FULL_PROTOCOL),
])
def test_bad_fragment_coordinates_rollback_all_fake_rows(connection, key, value):
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, payload_changes={key:value})
    assert count(connection, Receipt) == count(connection, TradeFill) == count(connection, PaperTradeLog) == 0
    assert connection.scalar(select(State.revision)) == 0
    assert connection.scalar(select(TradeOrder.filled_quantity)) == 0
    header(connection)  # The failed fragment has not consumed structural revision.


@pytest.mark.parametrize("quantity", [0, 50, 101, -100, 400])
def test_invalid_fragment_size_or_overfill_rejected(connection, quantity):
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, quantity=quantity)
    assert connection.scalar(select(State.revision)) == 0


@pytest.mark.parametrize("change", [
    {"trade":{"account_id":8}}, {"fill":{"broker_trade_id":"999"}},
    {"fill":{"side":"sell"}}, {"trade":{"price":11}}, {"fill":{"commission":0}},
    {"fill":{"trade_date":AT.date()-timedelta(days=1)}},
    {"trade":{"trade_time":NOW-timedelta(days=1)}},
])
def test_actual_row_structural_conflicts_are_not_json_authority(connection, change):
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, book_changes=change)


@pytest.mark.parametrize("status,filled", [("canceled",0), ("filled",0), ("submitted",100)])
def test_terminal_or_unrecorded_quantity_cannot_register_fragment(connection, status, filled):
    connection.execute(update(TradeOrder).values(status=status, filled_quantity=filled))
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection)



@pytest.mark.parametrize("damage", ["unreceipted_fill","fixed_price_changes","root_precedes_book"])
def test_partial_history_cannot_hide_unbound_rows_or_clock_price_conflicts(connection, damage):
    if damage=="unreceipted_fill":
        connection.execute(insert(TradeFill).values(id=9, fill_id="unbound", order_id=ORDER,
            broker="paper", code="600000", side="buy", price=10, quantity=100,
            trade_date=AT.date(), filled_at=NOW))
        kwargs = {}
    else:
        header(connection)
        kwargs = ({"book_changes":{"trade":{"price":11}, "fill":{"price":11}},
                   "payload_changes":{"fixed_price":11}}
                  if damage=="fixed_price_changes" else {"checked_at":NOW})
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, 1 if damage=="unreceipted_fill" else 2, **kwargs)


def test_closed_account_is_not_reopened_by_structure_check(connection):
    connection.execute(update(PaperAccount).values(status="closed"))
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection)
    assert connection.scalar(select(PaperAccount.status)) == "closed"


def test_full_and_partial_protocols_cannot_mix_for_one_original(connection):
    header(connection)
    # Even modifying the mutable projection cannot make a full replay legal.
    connection.execute(update(TradeOrder).values(filled_quantity=0, status="submitted"))
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, 2, quantity=300, before=0, protocol=FULL_PROTOCOL)


def test_partial_cannot_follow_a_full_receipt_or_unknown_version(connection):
    header(connection, quantity=300, protocol=FULL_PROTOCOL)
    connection.execute(update(TradeOrder).values(filled_quantity=0, status="submitted"))
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, 2, before=0)
    for protocol in ("unknown", "after_hours_partial_candidate_v3_20261002"):
        with pytest.raises(IntegrityError), connection.begin_nested():
            header(connection, 2, quantity=300, protocol=protocol)


@pytest.mark.parametrize("field", ["id","allocation_id","trade_fill_id","paper_trade_id"])
def test_header_replace_never_releases_a_bound_receipt(connection, field):
    values = header(connection)
    row = dict(connection.execute(select(Receipt.__table__)).mappings().one())
    replacement = deepcopy(row)
    replacement.update(id=99, allocation_id="9"*64, trade_fill_id=99, paper_trade_id=99)
    replacement[field] = row[field]
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(insert(Receipt).prefix_with("OR REPLACE").values(**replacement))
    assert dict(connection.execute(select(Receipt.__table__)).mappings().one()) == row


@pytest.mark.parametrize("target", ["id","fill_id"])
def test_update_replace_unbound_destination_cannot_delete_bound_book(connection, target):
    header(connection)
    connection.execute(insert(TradeFill).values(id=9, fill_id="unbound", order_id="unbound",
        broker="paper", code="600000", side="buy", price=10, quantity=100,
        trade_date=AT.date(), filled_at=NOW))
    value = 1 if target=="id" else "fill-afp-"+f"{1:031x}"
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(text(f"UPDATE OR REPLACE trade_fill SET {target}=:value WHERE id=9"), {"value":value})
    assert count(connection, TradeFill) == 2
    assert count(connection, Receipt) == 1


def test_migration_upgrades_legacy_guard_and_preserves_full_history(connection, monkeypatch):
    legacy(connection)
    header(connection, quantity=300, protocol=FULL_PROTOCOL)
    before = list(connection.execute(select(Receipt.__table__)).mappings())
    mod = migration(monkeypatch, connection)
    mod.upgrade()
    assert list(connection.execute(select(Receipt.__table__)).mappings()) == before
    mod.downgrade()
    assert list(connection.execute(select(Receipt.__table__)).mappings()) == before
    mod.upgrade()


def test_partial_nonempty_downgrade_is_lossless_refusal(connection, monkeypatch):
    header(connection)
    mod = migration(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="downgrade prohibited"):
        mod.downgrade()
    assert count(connection, Receipt) == 1
    assert "json_valid" in connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name='af_resource_receipt_book_binding'").scalar()


@pytest.mark.parametrize("damage", ["missing_guard","unknown_protocol","duplicate_full"])
def test_upgrade_refuses_bad_legacy_metadata_before_ddl(connection, monkeypatch, damage):
    legacy(connection)
    if damage=="missing_guard":
        connection.exec_driver_sql("DROP TRIGGER af_resource_receipt_no_delete")
    elif damage=="unknown_protocol":
        header(connection, quantity=300, protocol="unknown")
    else:
        header(connection, quantity=300, protocol=FULL_PROTOCOL)
        header(connection, 2, quantity=300, before=0, protocol=FULL_PROTOCOL)
    old = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name='af_resource_receipt_book_binding'").scalar()
    with pytest.raises(RuntimeError):
        migration(monkeypatch, connection).upgrade()
    assert connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name='af_resource_receipt_book_binding'").scalar() == old



@pytest.mark.parametrize("serialized", ["{", "null", "[]",
    lambda p:_json(p)[:-1]+',"fragment_index":1}',
    lambda p:_json({**p, "note":"中"*700000})])
def test_json_object_unique_keys_and_utf8_limit_are_structural_requirements(connection, serialized):
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, serialized_payload=serialized)


def test_nul_request_cannot_hide_after_sqlite_text_length(connection):
    request_id = "afp-"+f"{1:031x}"+"\x00"
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, payload_changes={"request_id":request_id},
               book_changes={"fill":{"fill_id":"fill-"+request_id}})


def test_failed_create_restores_old_guard_without_a_dbapi_begin(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        Base.metadata.create_all(conn, tables=TABLES)
        legacy(conn)
        assert conn.connection.driver_connection.in_transaction is False
        old = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name='af_resource_receipt_book_binding'").scalar()
        mod = migration(monkeypatch, conn)
        execute = mod.op.execute
        def fail_create(statement, *args, **kwargs):
            if str(statement).startswith("CREATE TRIGGER"):
                raise RuntimeError("injected CREATE failure")
            return execute(statement, *args, **kwargs)
        monkeypatch.setattr(mod.op, "execute", fail_create)
        with pytest.raises(RuntimeError, match="injected CREATE"):
            mod.upgrade()
        assert conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name='af_resource_receipt_book_binding'").scalar() == old
    engine.dispose()



def test_interleaved_originals_share_one_revision_but_separate_cumulative_history(connection):
    other = ORDER+".other"
    connection.execute(insert(TradeOrder).values(id=2, order_id=other, broker="paper",
        account_id="default", order_type="after_hours_fixed", code="600000", side="buy",
        price=10.5, quantity=200, filled_quantity=0, status="submitted", trade_date=AT.date(),
        created_at=AT-timedelta(seconds=1), decision_at=AT-timedelta(seconds=1)))
    header(connection, 1)
    header(connection, 2, order_id=other, original_quantity=200, fragment_index=1)
    header(connection, 3, fragment_index=2)
    header(connection, 4, fragment_index=3)
    header(connection, 5, order_id=other, original_quantity=200, fragment_index=2)
    assert connection.scalar(select(State.revision)) == count(connection, Receipt) == 5
    assert dict(connection.execute(select(TradeOrder.order_id, TradeOrder.filled_quantity)).all()) == {
        ORDER:300, other:200}


@pytest.mark.parametrize("key", ["request_id","fragment_index","cumulative_before","fixed_price"])
@pytest.mark.parametrize("missing", [False, True])
def test_null_or_missing_json_leaf_is_not_sql_null_authority(connection, key, missing):
    def damaged(payload):
        payload = dict(payload)
        if missing:
            del payload[key]
        else:
            payload[key] = None
        return _json(payload)
    with pytest.raises(IntegrityError), connection.begin_nested():
        header(connection, serialized_payload=damaged)


@pytest.mark.asyncio
async def test_valid_partial_header_is_not_filtered_or_upgraded_by_full_reader():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.trading import paper_after_hours_resources as resources
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c:Base.metadata.create_all(c, tables=TABLES))
            await conn.run_sync(seed)
            await conn.run_sync(header)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await resources._verify_sqlite_guards(db)
            untouched = {"sentinel":True}
            with pytest.raises(ValueError, match="durable_resource_receipt_hash_or_identity"):
                await resources._durable_priors(db, KEY, SimpleNamespace(id=7), cutoff=NOW,
                                               verified_bindings=untouched)
            assert untouched == {"sentinel":True}
            with pytest.raises(ValueError, match="durable_resource_receipt_hash_or_identity"):
                await resources._verified_receipt_bindings(db, account_numeric_id=7,
                    code="600000", trade_date=AT.date(), cutoff=NOW)
            assert await db.scalar(select(func.count()).select_from(Receipt)) == 1
            assert await db.scalar(select(State.revision)) == 1
    finally:
        await engine.dispose()


def test_non_sqlite_migration_never_touches_tables(connection, monkeypatch):
    mod = migration(monkeypatch, connection)
    fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.setattr(mod, "op", fake)
    with pytest.raises(RuntimeError, match="verified SQLite"):
        mod.upgrade()
