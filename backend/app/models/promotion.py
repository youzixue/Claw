"""Versioned promotion-model registry and append-only prediction ledger."""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)

from app.db.session import Base


class PromotionModelArtifact(Base):
    """A reproducible trained/rule-model artifact and its validation evidence."""

    __tablename__ = "promotion_model_artifact"
    __table_args__ = (
        Index("ix_promotion_model_artifact_status", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_version = Column(String(80), nullable=False, unique=True, index=True)
    feature_version = Column(String(80), nullable=False)
    data_version = Column(String(80), nullable=False)
    algorithm = Column(String(60), nullable=False, default="legacy_rule_ensemble")
    status = Column(String(20), nullable=False, default="registered")
    artifact_uri = Column(Text)
    params_json = Column(Text, nullable=False, default="{}")
    metrics_json = Column(Text, nullable=False, default="{}")
    training_start_date = Column(Date)
    training_end_date = Column(Date)
    validation_start_date = Column(Date)
    validation_end_date = Column(Date)
    code_commit = Column(String(64))
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    activated_at = Column(DateTime)
    retired_at = Column(DateTime)


class PromotionPredictionRun(Base):
    """Immutable identity and summary for one prediction execution."""

    __tablename__ = "promotion_prediction_run"
    __table_args__ = (
        Index(
            "ix_promotion_prediction_run_lookup",
            "reference_trade_date",
            "snapshot_context",
            "created_at",
        ),
        Index("ix_promotion_prediction_run_model", "model_version", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_key = Column(String(64), nullable=False, unique=True, index=True)
    snapshot_batch_key = Column(String(255), nullable=False, default="")
    reference_trade_date = Column(Date, nullable=False, index=True)
    as_of_at = Column(DateTime, nullable=False)
    snapshot_source = Column(String(20), nullable=False)
    snapshot_context = Column(String(40), nullable=False)
    model_version = Column(String(80), nullable=False, index=True)
    feature_version = Column(String(80), nullable=False)
    data_version = Column(String(80), nullable=False)
    runtime_mode = Column(String(20), nullable=False, default="legacy")
    status = Column(String(20), nullable=False, default="completed")
    gate_passed = Column(Boolean)
    candidate_count = Column(Integer, nullable=False, default=0)
    ranked_count = Column(Integer, nullable=False, default=0)
    actionable_count = Column(Integer, nullable=False, default=0)
    payload_hash = Column(String(64), nullable=False)
    metadata_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    completed_at = Column(DateTime)


class PromotionPredictionSnapshot(Base):
    """Append-only point-in-time candidate prediction belonging to a run."""

    __tablename__ = "promotion_prediction_snapshot"
    __table_args__ = (
        UniqueConstraint("run_id", "record_key", name="uq_promotion_snapshot_run_record"),
        Index(
            "ix_promotion_ledger_snapshot_lookup",
            "prediction_trade_date",
            "target_board",
            "code",
        ),
        Index("ix_promotion_ledger_snapshot_rank", "run_id", "rank_scope", "rank_position"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        Integer,
        ForeignKey("promotion_prediction_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    record_key = Column(String(64), nullable=False)
    # Deliberately not a foreign key: the compatibility table may replace pending
    # rows, while this ledger must retain the original append-only execution.
    legacy_record_id = Column(Integer)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(30))
    target_board = Column(Integer, nullable=False)
    prediction_trade_date = Column(Date, nullable=False)
    horizon_days = Column(Integer, nullable=False, default=1)
    candidate_route = Column(String(60), nullable=False, default="")
    learning_bucket = Column(String(100))
    rank_scope = Column(String(30), nullable=False, default="pool_unranked")
    pool_rank = Column(Integer)
    rank_position = Column(Integer)
    recall_rank_position = Column(Integer)
    raw_probability = Column(Float)
    calibrated_probability = Column(Float)
    confidence_level = Column(String(20))
    signal_status = Column(String(30))
    trade_gate_passed = Column(Boolean, nullable=False, default=False)
    actionable = Column(Boolean, nullable=False, default=False)
    watch_only = Column(Boolean, nullable=False, default=False)
    reason_json = Column(Text, nullable=False, default="{}")
    features_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class PromotionTrainingRun(Base):
    """Auditable offline training and walk-forward evaluation execution."""

    __tablename__ = "promotion_training_run"
    __table_args__ = (
        Index("ix_promotion_training_run_lookup", "target_board", "status", "started_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    training_key = Column(String(80), nullable=False, unique=True, index=True)
    target_board = Column(Integer, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="running")
    model_version = Column(String(80))
    feature_version = Column(String(80), nullable=False)
    data_version = Column(String(80))
    config_json = Column(Text, nullable=False, default="{}")
    dataset_json = Column(Text, nullable=False, default="{}")
    metrics_json = Column(Text, nullable=False, default="{}")
    acceptance_json = Column(Text, nullable=False, default="{}")
    artifact_id = Column(Integer, ForeignKey("promotion_model_artifact.id"))
    error_message = Column(Text)
    started_at = Column(DateTime, nullable=False, default=datetime.now)
    completed_at = Column(DateTime)


class PromotionExperiment(Base):
    """Champion/challenger evaluation contract and promotion decision."""

    __tablename__ = "promotion_experiment"
    __table_args__ = (
        Index("ix_promotion_experiment_status", "status", "started_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    experiment_key = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    champion_model_version = Column(String(80), nullable=False)
    challenger_model_version = Column(String(80), nullable=False)
    status = Column(String(20), nullable=False, default="draft")
    allocation_mode = Column(String(30), nullable=False, default="shadow")
    acceptance_json = Column(Text, nullable=False, default="{}")
    metrics_json = Column(Text, nullable=False, default="{}")
    decision = Column(String(30))
    decision_reason = Column(Text)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class PromotionShadowRun(Base):
    """Immutable challenger inference over one frozen production candidate run."""

    __tablename__ = "promotion_shadow_run"
    __table_args__ = (
        UniqueConstraint(
            "prediction_run_id",
            "artifact_id",
            "target_board",
            name="uq_promotion_shadow_run_scope",
        ),
        Index(
            "ix_promotion_shadow_run_lookup",
            "artifact_id",
            "target_board",
            "snapshot_context",
            "reference_trade_date",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    shadow_key = Column(String(64), nullable=False, unique=True, index=True)
    prediction_run_id = Column(
        Integer,
        ForeignKey("promotion_prediction_run.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    artifact_id = Column(
        Integer,
        ForeignKey("promotion_model_artifact.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_board = Column(Integer, nullable=False, index=True)
    snapshot_context = Column(String(40), nullable=False)
    reference_trade_date = Column(Date, nullable=False, index=True)
    as_of_at = Column(DateTime, nullable=False)
    champion_model_version = Column(String(80), nullable=False)
    challenger_model_version = Column(String(80), nullable=False)
    feature_version = Column(String(80), nullable=False)
    data_version = Column(String(80), nullable=False)
    status = Column(String(20), nullable=False, default="completed")
    candidate_count = Column(Integer, nullable=False, default=0)
    payload_hash = Column(String(64), nullable=False)
    metadata_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    completed_at = Column(DateTime, nullable=False, default=datetime.now)


class PromotionShadowPrediction(Base):
    """Immutable paired Champion/Challenger score for the same candidate."""

    __tablename__ = "promotion_shadow_prediction"
    __table_args__ = (
        UniqueConstraint(
            "shadow_run_id",
            "prediction_snapshot_id",
            name="uq_promotion_shadow_prediction_snapshot",
        ),
        Index(
            "ix_promotion_shadow_prediction_lookup",
            "prediction_trade_date",
            "target_board",
            "code",
        ),
        Index(
            "ix_promotion_shadow_prediction_rank",
            "shadow_run_id",
            "challenger_rank_position",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    shadow_run_id = Column(
        Integer,
        ForeignKey("promotion_shadow_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    prediction_snapshot_id = Column(
        Integer,
        ForeignKey("promotion_prediction_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(30))
    target_board = Column(Integer, nullable=False)
    prediction_trade_date = Column(Date, nullable=False)
    candidate_route = Column(String(60), nullable=False, default="")
    market_regime = Column(String(40), nullable=False, default="unknown")
    champion_probability = Column(Float, nullable=False)
    challenger_raw_probability = Column(Float, nullable=False)
    challenger_probability = Column(Float, nullable=False)
    champion_rank_position = Column(Integer)
    challenger_rank_position = Column(Integer, nullable=False)
    features_hash = Column(String(64), nullable=False)
    metadata_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class PromotionShadowEvaluation(Base):
    """Immutable cumulative out-of-time evidence for one shadow challenger lane."""

    __tablename__ = "promotion_shadow_evaluation"
    __table_args__ = (
        Index(
            "ix_promotion_shadow_evaluation_lookup",
            "artifact_id",
            "target_board",
            "snapshot_context",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    evaluation_key = Column(String(64), nullable=False, unique=True, index=True)
    artifact_id = Column(
        Integer,
        ForeignKey("promotion_model_artifact.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    trigger_shadow_run_id = Column(
        Integer,
        ForeignKey("promotion_shadow_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_board = Column(Integer, nullable=False, index=True)
    snapshot_context = Column(String(40), nullable=False)
    challenger_model_version = Column(String(80), nullable=False)
    label_version = Column(String(80), nullable=False)
    outcome_end_date = Column(Date, nullable=False)
    evaluated_shadow_run_count = Column(Integer, nullable=False)
    trade_day_count = Column(Integer, nullable=False)
    sample_count = Column(Integer, nullable=False)
    positive_count = Column(Integer, nullable=False)
    decision = Column(String(40), nullable=False)
    metrics_json = Column(Text, nullable=False, default="{}")
    acceptance_json = Column(Text, nullable=False, default="{}")
    metadata_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class PromotionDeploymentEvent(Base):
    """Append-only manual approval/rollback event; latest event is authoritative."""

    __tablename__ = "promotion_deployment_event"
    __table_args__ = (
        Index(
            "ix_promotion_deployment_event_current",
            "target_board",
            "created_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(80), nullable=False, unique=True, index=True)
    target_board = Column(Integer, nullable=False, index=True)
    action = Column(String(20), nullable=False)
    deployment_mode = Column(String(40), nullable=False, default="probability_overlay")
    artifact_id = Column(
        Integer,
        ForeignKey("promotion_model_artifact.id", ondelete="RESTRICT"),
    )
    evidence_evaluation_id = Column(
        Integer,
        ForeignKey("promotion_shadow_evaluation.id", ondelete="RESTRICT"),
    )
    from_model_version = Column(String(80), nullable=False)
    to_model_version = Column(String(80), nullable=False)
    operator = Column(String(80), nullable=False)
    reason = Column(Text, nullable=False)
    confirmation_phrase = Column(String(40), nullable=False)
    metadata_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


def _reject_ledger_mutation(_mapper, _connection, target) -> None:
    raise RuntimeError(
        f"{target.__class__.__name__} is append-only; create a new prediction run instead"
    )


for _immutable_model in (
    PromotionModelArtifact,
    PromotionPredictionRun,
    PromotionPredictionSnapshot,
    PromotionShadowRun,
    PromotionShadowPrediction,
    PromotionShadowEvaluation,
    PromotionDeploymentEvent,
):
    event.listen(_immutable_model, "before_update", _reject_ledger_mutation)
    event.listen(_immutable_model, "before_delete", _reject_ledger_mutation)
