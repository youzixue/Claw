"""add immutable quote rounds and causal paper execution audit

Revision ID: 024_quote_round_execution
Revises: 023_health_completeness
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "024_quote_round_execution"
down_revision: Union[str, None] = "023_health_completeness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    if table_name not in _table_names():
        return set()
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    if table_name not in _table_names():
        return set()
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
        if item.get("name")
    }


def _add_missing_columns(
    table_name: str,
    columns: dict[str, sa.Column],
) -> None:
    existing = _column_names(table_name)
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    tables = _table_names()
    if "quote_round" not in tables:
        op.create_table(
            "quote_round",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("round_id", sa.String(length=64), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("source", sa.String(length=20), nullable=False),
            sa.Column("source_min_at", sa.DateTime(), nullable=True),
            sa.Column("source_max_at", sa.DateTime(), nullable=True),
            sa.Column("received_min_at", sa.DateTime(), nullable=True),
            sa.Column("received_max_at", sa.DateTime(), nullable=True),
            sa.Column("committed_at", sa.DateTime(), nullable=False),
            sa.Column("as_of_at", sa.DateTime(), nullable=False),
            sa.Column("expected_count", sa.Integer(), nullable=False),
            sa.Column("received_count", sa.Integer(), nullable=False),
            sa.Column("source_time_count", sa.Integer(), nullable=False),
            sa.Column("coverage", sa.Float(), nullable=False),
            sa.Column("source_time_coverage", sa.Float(), nullable=False),
            sa.Column("quality_status", sa.String(length=16), nullable=False),
            sa.Column("quality_reason", sa.Text(), nullable=True),
            sa.Column("component_watermarks_json", sa.Text(), nullable=False),
            sa.Column("config_version", sa.String(length=64), nullable=False),
            sa.Column("code_version", sa.String(length=64), nullable=False),
            sa.Column("archive_status", sa.String(length=16), nullable=False),
            sa.Column("archive_path", sa.Text(), nullable=True),
            sa.Column("minute_archive_path", sa.Text(), nullable=True),
            sa.Column("focus_path", sa.Text(), nullable=True),
            sa.Column("focus_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("round_id", name="uq_quote_round_round_id"),
        )
        op.create_index(
            "ix_quote_round_trade_date_committed",
            "quote_round",
            ["trade_date", "committed_at"],
        )
        op.create_index(
            "ix_quote_round_quality_committed",
            "quote_round",
            ["quality_status", "committed_at"],
        )

    _add_missing_columns(
        "stock_spot",
        {
            "quote_round_id": sa.Column(
                "quote_round_id", sa.String(length=64), nullable=True
            ),
        },
    )
    if (
        "stock_spot" in _table_names()
        and "ix_stock_spot_quote_round_id" not in _index_names("stock_spot")
    ):
        op.create_index(
            "ix_stock_spot_quote_round_id",
            "stock_spot",
            ["quote_round_id"],
        )

    _add_missing_columns(
        "paper_trade_log",
        {
            "tax": sa.Column(
                "tax", sa.Float(), nullable=False, server_default="0"
            ),
            "decision_round_id": sa.Column(
                "decision_round_id", sa.String(length=64), nullable=True
            ),
            "fill_round_id": sa.Column(
                "fill_round_id", sa.String(length=64), nullable=True
            ),
            "forced_probe": sa.Column(
                "forced_probe",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            "excluded_from_performance": sa.Column(
                "excluded_from_performance",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        },
    )
    if "paper_trade_log" in _table_names():
        with op.batch_alter_table("paper_trade_log") as batch_op:
            batch_op.alter_column(
                "signal_id",
                existing_type=sa.String(length=40),
                type_=sa.String(length=80),
                existing_nullable=True,
            )
        for name, column in (
            ("ix_paper_trade_log_decision_round_id", "decision_round_id"),
            ("ix_paper_trade_log_fill_round_id", "fill_round_id"),
        ):
            if name not in _index_names("paper_trade_log"):
                op.create_index(name, "paper_trade_log", [column])

    _add_missing_columns(
        "paper_auto_trade_log",
        {
            "quote_round_id": sa.Column(
                "quote_round_id", sa.String(length=64), nullable=True
            ),
            "as_of_at": sa.Column("as_of_at", sa.DateTime(), nullable=True),
            "stage_code": sa.Column(
                "stage_code", sa.String(length=32), nullable=True
            ),
            "reason_code": sa.Column(
                "reason_code", sa.String(length=48), nullable=True
            ),
            "metric_value": sa.Column("metric_value", sa.Float(), nullable=True),
            "threshold_value": sa.Column(
                "threshold_value", sa.Float(), nullable=True
            ),
            "config_version": sa.Column(
                "config_version", sa.String(length=64), nullable=True
            ),
            "code_version": sa.Column(
                "code_version", sa.String(length=64), nullable=True
            ),
        },
    )
    if "paper_auto_trade_log" in _table_names():
        for name, column in (
            ("ix_paper_auto_trade_log_quote_round_id", "quote_round_id"),
            ("ix_paper_auto_trade_log_stage_code", "stage_code"),
            ("ix_paper_auto_trade_log_reason_code", "reason_code"),
        ):
            if name not in _index_names("paper_auto_trade_log"):
                op.create_index(name, "paper_auto_trade_log", [column])

    _add_missing_columns(
        "trade_order",
        {
            "strategy_version": sa.Column(
                "strategy_version", sa.String(length=64), nullable=True
            ),
            "idempotency_key": sa.Column(
                "idempotency_key", sa.String(length=160), nullable=True
            ),
            "decision_round_id": sa.Column(
                "decision_round_id", sa.String(length=64), nullable=True
            ),
            "last_fill_round_id": sa.Column(
                "last_fill_round_id", sa.String(length=64), nullable=True
            ),
            "decision_at": sa.Column("decision_at", sa.DateTime(), nullable=True),
            "as_of_at": sa.Column("as_of_at", sa.DateTime(), nullable=True),
            "config_version": sa.Column(
                "config_version", sa.String(length=64), nullable=True
            ),
            "code_version": sa.Column(
                "code_version", sa.String(length=64), nullable=True
            ),
        },
    )
    if "trade_order" in _table_names():
        for name, columns, unique in (
            ("uq_trade_order_idempotency_key", ["idempotency_key"], True),
            ("ix_trade_order_decision_round_id", ["decision_round_id"], False),
            ("ix_trade_order_last_fill_round_id", ["last_fill_round_id"], False),
        ):
            if name not in _index_names("trade_order"):
                op.create_index(
                    name,
                    "trade_order",
                    columns,
                    unique=unique,
                )

    _add_missing_columns(
        "trade_fill",
        {
            "decision_round_id": sa.Column(
                "decision_round_id", sa.String(length=64), nullable=True
            ),
            "fill_round_id": sa.Column(
                "fill_round_id", sa.String(length=64), nullable=True
            ),
        },
    )
    if "trade_fill" in _table_names():
        for name, column in (
            ("ix_trade_fill_decision_round_id", "decision_round_id"),
            ("ix_trade_fill_fill_round_id", "fill_round_id"),
        ):
            if name not in _index_names("trade_fill"):
                op.create_index(name, "trade_fill", [column])

    if "paper_control_sample" not in _table_names():
        op.create_table(
            "paper_control_sample",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("sample_key", sa.String(length=128), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("challenger_account_id", sa.Integer(), nullable=True),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("strategy_version", sa.String(length=64), nullable=False),
            sa.Column("quote_round_id", sa.String(length=64), nullable=True),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=True),
            sa.Column("code", sa.String(length=10), nullable=True),
            sa.Column("name", sa.String(length=20), nullable=True),
            sa.Column("price", sa.Float(), nullable=True),
            sa.Column("candidate_score", sa.Float(), nullable=True),
            sa.Column("decision", sa.String(length=24), nullable=False),
            sa.Column("reason_code", sa.String(length=48), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column(
                "candidate_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column("next_round_id", sa.String(length=64), nullable=True),
            sa.Column("next_round_price", sa.Float(), nullable=True),
            sa.Column(
                "next_round_fillable_amount", sa.Integer(), nullable=True
            ),
            sa.Column("close_price", sa.Float(), nullable=True),
            sa.Column("return_pct", sa.Float(), nullable=True),
            sa.Column(
                "forced_probe",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "excluded_from_performance",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("finalized_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "sample_key", name="uq_paper_control_sample_key"
            ),
        )
        op.create_index(
            "ix_paper_control_sample_account_date",
            "paper_control_sample",
            ["account_id", "trade_date"],
        )
        op.create_index(
            "ix_paper_control_sample_challenger_account_id",
            "paper_control_sample",
            ["challenger_account_id"],
        )
        op.create_index(
            "ix_paper_control_sample_quote_round_id",
            "paper_control_sample",
            ["quote_round_id"],
        )
        op.create_index(
            "ix_paper_control_sample_code",
            "paper_control_sample",
            ["code"],
        )

    if "paper_daily_outcome" not in _table_names():
        op.create_table(
            "paper_daily_outcome",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("strategy_version", sa.String(length=64), nullable=False),
            sa.Column(
                "terminal_status",
                sa.String(length=32),
                nullable=False,
                server_default="running",
            ),
            sa.Column(
                "reason_code",
                sa.String(length=48),
                nullable=False,
                server_default="not_finalized",
            ),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column(
                "scan_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "decision_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "submitted_order_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "fill_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "blocked_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("first_round_id", sa.String(length=64), nullable=True),
            sa.Column("last_round_id", sa.String(length=64), nullable=True),
            sa.Column("control_sample_id", sa.Integer(), nullable=True),
            sa.Column(
                "is_terminal",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "details_json", sa.Text(), nullable=False, server_default="{}"
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("finalized_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "account_id",
                "trade_date",
                "strategy_version",
                name="uq_paper_daily_outcome_account_date_version",
            ),
        )
        op.create_index(
            "ix_paper_daily_outcome_account_id",
            "paper_daily_outcome",
            ["account_id"],
        )
        op.create_index(
            "ix_paper_daily_outcome_date_status",
            "paper_daily_outcome",
            ["trade_date", "terminal_status"],
        )


def downgrade() -> None:
    for table_name in ("paper_daily_outcome", "paper_control_sample"):
        if table_name in _table_names():
            op.drop_table(table_name)

    indexed_columns = {
        "trade_fill": (
            "ix_trade_fill_fill_round_id",
            "ix_trade_fill_decision_round_id",
        ),
        "trade_order": (
            "ix_trade_order_last_fill_round_id",
            "ix_trade_order_decision_round_id",
            "uq_trade_order_idempotency_key",
        ),
        "paper_auto_trade_log": (
            "ix_paper_auto_trade_log_reason_code",
            "ix_paper_auto_trade_log_stage_code",
            "ix_paper_auto_trade_log_quote_round_id",
        ),
        "paper_trade_log": (
            "ix_paper_trade_log_fill_round_id",
            "ix_paper_trade_log_decision_round_id",
        ),
        "stock_spot": ("ix_stock_spot_quote_round_id",),
    }
    for table_name, indexes in indexed_columns.items():
        for index_name in indexes:
            if (
                table_name in _table_names()
                and index_name in _index_names(table_name)
            ):
                op.drop_index(index_name, table_name=table_name)

    columns = {
        "trade_fill": ("fill_round_id", "decision_round_id"),
        "trade_order": (
            "code_version",
            "config_version",
            "as_of_at",
            "decision_at",
            "last_fill_round_id",
            "decision_round_id",
            "idempotency_key",
            "strategy_version",
        ),
        "paper_auto_trade_log": (
            "code_version",
            "config_version",
            "threshold_value",
            "metric_value",
            "reason_code",
            "stage_code",
            "as_of_at",
            "quote_round_id",
        ),
        "paper_trade_log": (
            "excluded_from_performance",
            "forced_probe",
            "fill_round_id",
            "decision_round_id",
            "tax",
        ),
        "stock_spot": ("quote_round_id",),
    }
    for table_name, column_names in columns.items():
        existing = _column_names(table_name)
        if not existing:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            for column_name in column_names:
                if column_name in existing:
                    batch_op.drop_column(column_name)

    if "quote_round" in _table_names():
        op.drop_table("quote_round")
