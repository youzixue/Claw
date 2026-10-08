"""Prospective shared 50k signal/decision ledgers. No seed or account creation."""
from alembic import op
import sqlalchemy as sa

revision = "035_shared_paper_portfolio"
down_revision = "034_data_watermark_revisions"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Fail before changing anything: an unverified pre-existing table may have
    # lost evidence or immutability. Never silently bless create_all/manual DDL.
    for table in ("paper_portfolio_signal", "paper_portfolio_decision"):
        if inspector.has_table(table):
            raise RuntimeError(f"existing {table} requires explicit schema/evidence verification")
    op.create_table(
        "paper_portfolio_signal",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("signal_key", sa.String(96), nullable=False, unique=True),
        sa.Column("portfolio_version", sa.String(64), nullable=False),
        sa.Column("origin_account", sa.String(30), nullable=False),
        sa.Column("origin_account_id", sa.Integer(), nullable=False),
        sa.Column("origin_version", sa.String(64), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("name", sa.String(40)),
        sa.Column("source_signal_id", sa.String(128), nullable=False),
        sa.Column("shadow_event_key", sa.String(128)),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False),
        sa.Column("decision_round_id", sa.String(64), nullable=False),
        sa.Column("as_of_at", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("entry_policy_json", sa.Text(), nullable=False),
        sa.Column("exit_policy_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_portfolio_signal_observed", "paper_portfolio_signal",
                    ["portfolio_version", "observed_at"])
    op.create_index("ix_portfolio_signal_origin_code", "paper_portfolio_signal",
                    ["origin_account", "code", "confirmed_at"])
    op.create_table(
        "paper_portfolio_decision",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("decision_key", sa.String(128), nullable=False, unique=True),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("paper_portfolio_signal.id"), nullable=False),
        sa.Column("portfolio_version", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("decision_round_id", sa.String(64), nullable=False),
        sa.Column("as_of_at", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("order_id", sa.String(40)),
        sa.Column("budget_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_portfolio_decision_signal", "paper_portfolio_decision",
                    ["signal_id", "observed_at"])
    op.create_index("ix_portfolio_decision_order", "paper_portfolio_decision", ["order_id"])
    if bind.dialect.name == "sqlite":
        for table, key in (("paper_portfolio_signal", "signal_key"),
                           ("paper_portfolio_decision", "decision_key")):
            for action in ("UPDATE", "DELETE"):
                op.execute(sa.text(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'portfolio evidence is append-only'); END"))
            op.execute(sa.text(
                f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS "
                f"(SELECT 1 FROM {table} WHERE id=NEW.id OR {key}=NEW.{key}) "
                "BEGIN SELECT RAISE(ABORT, 'portfolio evidence is append-only'); END"))


def downgrade():
    raise RuntimeError("Portfolio evidence must be retained; roll back consumers, not history")
