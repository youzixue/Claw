"""Versioned fragment headers in the existing receipt table; no execution permit."""
import re

from alembic import op
import sqlalchemy as sa
from app.trading.paper_after_hours_resource_schema import (
    FULL_PROTOCOL, PARTIAL_PROTOCOL, sqlite_guards,
)

revision = "040_after_hours_partial_receipts"
down_revision = "039_after_hours_resource_receipts"
branch_labels = None
depends_on = None


def _ddl(statements):
    return {s.split("IF NOT EXISTS ", 1)[1].split()[0]: s for s in statements}


def _normalize(statement):
    return re.sub(r"\s+", " ", statement.replace("IF NOT EXISTS ", "")).strip().rstrip(";").lower()


def _verify(connection, expected):
    if connection.dialect.name != "sqlite":
        raise RuntimeError("partial fixed-price receipts require verified SQLite guards")
    installed = dict(connection.execute(sa.text(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger'")).all())
    if any(name not in installed or _normalize(installed[name]) != _normalize(sql)
           for name, sql in expected.items()):
        raise RuntimeError("partial receipt migration requires intact preceding guards")


def _replace(previous, following):
    # A real SAVEPOINT covers DDL even with SQLite legacy transaction control;
    # Alembic/SQLAlchemy logical BEGIN alone need not start a DBAPI transaction.
    with op.get_bind().begin_nested():
        for name, sql in following.items():
            if _normalize(sql) != _normalize(previous[name]):
                op.execute(sa.text(f"DROP TRIGGER {name}"))
                op.execute(sa.text(sql))


def upgrade():
    connection = op.get_bind()
    old, new = _ddl(sqlite_guards(include_partial=False)), _ddl(sqlite_guards())
    _verify(connection, old)
    # Never reinterpret unknown historical protocols, nor duplicate full orders.
    if connection.execute(sa.text("""
        SELECT count(*) FROM paper_after_hours_resource_receipt
        WHERE protocol_version IS NULL OR protocol_version<>:version
    """), {"version": FULL_PROTOCOL}).scalar():
        raise RuntimeError("unknown pre-partial receipt protocol; audit required")
    if connection.execute(sa.text("""
        SELECT count(*) FROM (SELECT order_id FROM paper_after_hours_resource_receipt
        GROUP BY order_id HAVING count(*)>1)
    """)).scalar():
        raise RuntimeError("duplicate historical full receipts; audit required")
    _replace(old, new)


def downgrade():
    connection = op.get_bind()
    old, new = _ddl(sqlite_guards(include_partial=False)), _ddl(sqlite_guards())
    _verify(connection, new)
    if connection.execute(sa.text("""
        SELECT count(*) FROM paper_after_hours_resource_receipt
        WHERE protocol_version=:version
    """), {"version": PARTIAL_PROTOCOL}).scalar():
        raise RuntimeError("partial receipt evidence exists; downgrade prohibited")
    if connection.execute(sa.text("""
        SELECT count(*) FROM paper_after_hours_resource_receipt
        WHERE protocol_version<>:version OR protocol_version IS NULL
    """), {"version": FULL_PROTOCOL}).scalar():
        raise RuntimeError("unknown receipt evidence exists; downgrade prohibited")
    _replace(new, old)
