"""信号相关数据模型 — 1张表"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, Date, DateTime, Text, Index, UniqueConstraint, event, DDL

from app.db.session import Base


class SignalPerformance(Base):
    """信号绩效追踪"""
    __tablename__ = "signal_performance"

    signal_id = Column(String(30), primary_key=True)
    stock_code = Column(String(10), nullable=False, index=True)
    signal_time = Column(DateTime, nullable=False, index=True)
    signal_price = Column(Float)
    signal_score = Column(Integer)
    signal_type = Column(String(20))            # ten_bagger/strong/trend
    signal_variant = Column(String(40))         # low_base_breakthrough/b1等细分形态
    setup_grade = Column(String(30))            # A1/A2/B

    # 收益跟踪
    return_1d = Column(Float)
    return_3d = Column(Float)
    return_5d = Column(Float)
    return_10d = Column(Float)
    net_return_1d = Column(Float)                # 扣除双边费用和滑点后的收益
    net_return_3d = Column(Float)
    net_return_5d = Column(Float)
    net_return_10d = Column(Float)
    benchmark_return_1d = Column(Float)          # 同期全市场等权收益
    benchmark_return_3d = Column(Float)
    benchmark_return_5d = Column(Float)
    benchmark_return_10d = Column(Float)
    excess_return_1d = Column(Float)             # 净收益减全市场基准收益
    excess_return_3d = Column(Float)
    excess_return_5d = Column(Float)
    excess_return_10d = Column(Float)
    max_return = Column(Float)
    max_drawdown = Column(Float)                 # 从信号价起算的最大不利波动(MAE)

    # 评估
    is_correct = Column(Boolean)
    failure_reason = Column(Text)
    evaluation_version = Column(String(20))

    # 归因
    top_factor = Column(String(30))             # 贡献最大因子
    board_tag = Column(String(20))              # tradeable/observe_only

    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AnomalyCandidateRecord(Base):
    """异动候选最新兼容投影；历史原因看Evidence，不参与推送额度或收益结算。"""

    __tablename__ = "anomaly_candidate_record"
    __table_args__ = (
        Index("ix_anomaly_candidate_date_status", "trade_date", "status"),
        Index("ix_anomaly_candidate_date_code", "trade_date", "code"),
    )

    record_id = Column(String(30), primary_key=True)
    trade_date = Column(Date, nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    identity = Column(Text, nullable=False)
    name = Column(String(30))
    event_type = Column(String(30), nullable=False, default="")
    first_seen_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    seen_count = Column(Integer, nullable=False, default=1)
    last_score = Column(Float)
    last_grade = Column(String(30))
    status = Column(String(20), nullable=False, default="candidate")
    reject_reasons_json = Column(Text, nullable=False, default="[]")
    pushed = Column(Boolean, nullable=False, default=False)
    pushed_at = Column(DateTime)
    snapshot_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AnomalyCandidateEvidence(Base):
    """Prospective immutable evaluation; not push quota, order or historical PIT."""
    __tablename__ = "anomaly_candidate_evidence"
    __table_args__ = (
        UniqueConstraint("capture_id", "record_id", name="uq_anomaly_evidence_capture_record"),
        Index("ix_anomaly_evidence_record_time", "record_id", "captured_at"),
        Index("ix_anomaly_evidence_day_code", "trade_date", "code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    capture_id = Column(String(40), nullable=False)
    record_id = Column(String(30), nullable=False)
    trade_date = Column(Date, nullable=False)
    code = Column(String(10), nullable=False)
    captured_at = Column(DateTime, nullable=False)
    protocol_version = Column(String(40), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)


def _reject_anomaly_evidence_mutation(mapper, connection, target):
    raise ValueError("anomaly candidate evidence is append-only")


event.listen(AnomalyCandidateEvidence, "before_update", _reject_anomaly_evidence_mutation)
event.listen(AnomalyCandidateEvidence, "before_delete", _reject_anomaly_evidence_mutation)
for _action in ("UPDATE", "DELETE"):
    event.listen(AnomalyCandidateEvidence.__table__, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS anomaly_candidate_evidence_no_{_action.lower()} "
        f"BEFORE {_action} ON anomaly_candidate_evidence "
        "BEGIN SELECT RAISE(ABORT, 'anomaly candidate evidence is append-only'); END"
    ).execute_if(dialect="sqlite"))
event.listen(AnomalyCandidateEvidence.__table__, "after_create", DDL(
    "CREATE TRIGGER IF NOT EXISTS anomaly_candidate_evidence_no_replace "
    "BEFORE INSERT ON anomaly_candidate_evidence WHEN EXISTS "
    "(SELECT 1 FROM anomaly_candidate_evidence WHERE id=NEW.id OR "
    "(capture_id=NEW.capture_id AND record_id=NEW.record_id)) "
    "BEGIN SELECT RAISE(ABORT, 'anomaly candidate evidence is append-only'); END"
).execute_if(dialect="sqlite"))


class PromotionPredictionRecord(Base):
    """晋级预测复盘记录"""
    __tablename__ = "promotion_prediction_record"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(20))
    target_board = Column(Integer, nullable=False, index=True)  # 1=首板, 2=二板
    prediction_trade_date = Column(Date, nullable=False, index=True)
    horizon_days = Column(Integer, default=1)

    predicted_probability = Column(Float)
    calibrated_probability = Column(Float)
    model_adjustment = Column(Float, default=0.0)
    confidence_level = Column(String(20))
    candidate_route = Column(String(40), nullable=False, default="")
    learning_bucket = Column(String(80), index=True)
    signal_status = Column(String(30))

    reason_snapshot = Column(Text)
    factors_json = Column(Text)

    # Explicit snapshot identity keeps 15:10/20:00/09:25/09:35 batches independent.
    # factors_json retains the same metadata for export/backward compatibility.
    snapshot_source = Column(String(20), nullable=False, default="legacy", index=True)
    snapshot_context = Column(String(40), nullable=False, default="legacy", index=True)
    snapshot_batch_key = Column(String(100), index=True)
    snapshot_recorded_at = Column(DateTime, index=True)
    model_version = Column(String(80), index=True)

    outcome_status = Column(String(20), default="pending", index=True)  # pending/success/failed
    outcome_trade_date = Column(Date)
    actual_limit_up_date = Column(Date)
    actual_max_change_pct = Column(Float)
    actual_close_change_pct = Column(Float)
    failure_reason = Column(Text)
    failure_tags_json = Column(Text)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "code",
            "target_board",
            "prediction_trade_date",
            "candidate_route",
            "snapshot_source",
            "snapshot_context",
            name="uq_promotion_prediction_code_target_date_route_snapshot",
        ),
        Index("ix_promotion_prediction_learning", "target_board", "candidate_route", "outcome_status"),
        Index(
            "ix_promotion_prediction_snapshot_lookup",
            "prediction_trade_date",
            "target_board",
            "snapshot_source",
            "snapshot_context",
        ),
    )
