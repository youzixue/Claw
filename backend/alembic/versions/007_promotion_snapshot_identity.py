"""晋级预测快照独立持久化与版本索引

Revision ID: 007_promotion_snapshot_identity
Revises: 006_dashboard_snapshot_lookup_index
Create Date: 2026-08-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "007_promotion_snapshot_identity"
down_revision: Union[str, None] = "006_dashboard_snapshot_lookup_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_UNIQUE = "uq_promotion_prediction_code_target_date_route"
NEW_UNIQUE = "uq_promotion_prediction_code_target_date_route_snapshot"
LOOKUP_INDEX = "ix_promotion_prediction_snapshot_lookup"
INDEXED_COLUMNS = {
    "snapshot_source": "ix_promotion_prediction_record_snapshot_source",
    "snapshot_context": "ix_promotion_prediction_record_snapshot_context",
    "snapshot_batch_key": "ix_promotion_prediction_record_snapshot_batch_key",
    "snapshot_recorded_at": "ix_promotion_prediction_record_snapshot_recorded_at",
    "model_version": "ix_promotion_prediction_record_model_version",
}


_COLUMNS = (
    sa.Column("snapshot_source", sa.String(20), nullable=False, server_default="legacy"),
    sa.Column("snapshot_context", sa.String(40), nullable=False, server_default="legacy"),
    sa.Column("snapshot_batch_key", sa.String(100), nullable=True),
    sa.Column("snapshot_recorded_at", sa.DateTime(), nullable=True),
    sa.Column("model_version", sa.String(40), nullable=True),
)


def _table_columns(conn) -> set[str]:
    return {column["name"] for column in sa.inspect(conn).get_columns("promotion_prediction_record")}


def _index_names(conn) -> set[str]:
    return {index["name"] for index in sa.inspect(conn).get_indexes("promotion_prediction_record")}


def _unique_names(conn) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(conn).get_unique_constraints("promotion_prediction_record")
        if constraint.get("name")
    }


def upgrade() -> None:
    conn = op.get_bind()
    existing_columns = _table_columns(conn)
    for column in _COLUMNS:
        if column.name not in existing_columns:
            op.add_column("promotion_prediction_record", column)

    # Backfill explicit columns from the historical JSON metadata. json_valid keeps
    # malformed legacy rows safe and the code still has a factors_json fallback.
    conn.execute(
        sa.text(
            """
            UPDATE promotion_prediction_record
               SET snapshot_source = CASE
                       WHEN json_valid(factors_json)
                       THEN COALESCE(NULLIF(json_extract(factors_json, '$.prediction_snapshot_source'), ''), 'legacy')
                       ELSE 'legacy'
                   END,
                   snapshot_context = CASE
                       WHEN json_valid(factors_json)
                       THEN COALESCE(NULLIF(json_extract(factors_json, '$.prediction_snapshot_context'), ''), 'legacy')
                       ELSE 'legacy'
                   END,
                   snapshot_batch_key = CASE
                       WHEN json_valid(factors_json)
                       THEN NULLIF(json_extract(factors_json, '$.prediction_snapshot_batch_key'), '')
                       ELSE NULL
                   END,
                   snapshot_recorded_at = CASE
                       WHEN json_valid(factors_json)
                       THEN NULLIF(json_extract(factors_json, '$.prediction_snapshot_recorded_at'), '')
                       ELSE NULL
                   END,
                   model_version = CASE
                       WHEN json_valid(factors_json)
                       THEN NULLIF(json_extract(factors_json, '$.prediction_model_version'), '')
                       ELSE NULL
                   END
            """
        )
    )

    unique_names = _unique_names(conn)
    with op.batch_alter_table("promotion_prediction_record") as batch_op:
        if OLD_UNIQUE in unique_names:
            batch_op.drop_constraint(OLD_UNIQUE, type_="unique")
        if NEW_UNIQUE not in unique_names:
            batch_op.create_unique_constraint(
                NEW_UNIQUE,
                [
                    "code",
                    "target_board",
                    "prediction_trade_date",
                    "candidate_route",
                    "snapshot_source",
                    "snapshot_context",
                ],
            )

    index_names = _index_names(conn)
    for column_name, index_name in INDEXED_COLUMNS.items():
        if index_name not in index_names:
            op.create_index(index_name, "promotion_prediction_record", [column_name], unique=False)
    if LOOKUP_INDEX not in index_names:
        op.create_index(
            LOOKUP_INDEX,
            "promotion_prediction_record",
            ["prediction_trade_date", "target_board", "snapshot_source", "snapshot_context"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    index_names = _index_names(conn)
    if LOOKUP_INDEX in index_names:
        op.drop_index(LOOKUP_INDEX, table_name="promotion_prediction_record")
    for index_name in INDEXED_COLUMNS.values():
        if index_name in index_names:
            op.drop_index(index_name, table_name="promotion_prediction_record")

    # The old schema allowed only one row per route. Keep the latest row before
    # restoring that constraint so downgrade remains deterministic.
    conn.execute(
        sa.text(
            """
            DELETE FROM promotion_prediction_record
             WHERE id NOT IN (
                 SELECT MAX(id)
                   FROM promotion_prediction_record
                  GROUP BY code, target_board, prediction_trade_date, candidate_route
             )
            """
        )
    )
    unique_names = _unique_names(conn)
    with op.batch_alter_table("promotion_prediction_record") as batch_op:
        if NEW_UNIQUE in unique_names:
            batch_op.drop_constraint(NEW_UNIQUE, type_="unique")
        if OLD_UNIQUE not in unique_names:
            batch_op.create_unique_constraint(
                OLD_UNIQUE,
                ["code", "target_board", "prediction_trade_date", "candidate_route"],
            )
        for column in reversed(_COLUMNS):
            if column.name in _table_columns(conn):
                batch_op.drop_column(column.name)
