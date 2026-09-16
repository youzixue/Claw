"""因子值、旧评估与追加式真实收益协议评估模型。"""

from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, Date, DateTime, UniqueConstraint, Index, Text, event, DDL

from app.db.session import Base


class FactorValue(Base):
    """因子值存储"""
    __tablename__ = "factor_values"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    stock_code = Column(String(10), nullable=False, index=True)
    factor_name = Column(String(30), nullable=False)
    factor_value = Column(Float)
    factor_rank = Column(Integer)               # 截面排名
    factor_pct = Column(Float)                  # 截面百分位

    __table_args__ = (
        UniqueConstraint("trade_date", "stock_code", "factor_name", name="uq_factor_date_code_name"),
        Index("ix_factor_name_date", "factor_name", "trade_date"),
    )


class FactorEvaluation(Base):
    """因子评估"""
    __tablename__ = "factor_evaluation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    factor_name = Column(String(30), nullable=False, index=True)
    eval_date = Column(Date, nullable=False, index=True)
    ic_mean = Column(Float)                     # IC均值
    ic_std = Column(Float)                      # IC标准差
    ir = Column(Float)                          # 信息比率
    win_rate = Column(Float)                    # 胜率
    is_decaying = Column(Boolean, default=False)
    decay_days = Column(Integer)                # 连续衰减天数

    __table_args__ = (
        UniqueConstraint("factor_name", "eval_date", name="uq_factor_eval_name_date"),
    )


class FactorEvaluationRun(Base):
    """新协议只追加：不覆盖旧排名自相关，也不冒充PIT因子生产证据。"""
    __tablename__ = "factor_evaluation_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    factor_name = Column(String(30), nullable=False, index=True)
    eval_date = Column(Date, nullable=False, index=True)
    as_of_at = Column(DateTime, nullable=False)
    protocol_version = Column(String(64), nullable=False)
    input_hash = Column(String(64), nullable=False)
    input_json = Column(Text, nullable=False)    # 冻结最小复算材料，不依赖后续可变日K
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    __table_args__ = (
        UniqueConstraint("factor_name", "protocol_version", "input_hash", name="uq_factor_eval_run_input"),
    )


class FactorComputationRun(Base):
    """Actual read-time calculation capture; never backdated historical PIT."""
    __tablename__ = "factor_computation_run"
    __table_args__ = (
        UniqueConstraint("capture_id", name="uq_factor_computation_capture"),
        Index("ix_factor_computation_day_time", "trade_date", "captured_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    capture_id = Column(String(40), nullable=False)
    trade_date = Column(Date, nullable=False)
    read_started_at = Column(DateTime, nullable=False)
    captured_at = Column(DateTime, nullable=False)
    protocol_version = Column(String(40), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)


def _reject_factor_computation_mutation(mapper, connection, target):
    raise ValueError("factor computation evidence is append-only")


event.listen(FactorComputationRun, "before_update", _reject_factor_computation_mutation)
event.listen(FactorComputationRun, "before_delete", _reject_factor_computation_mutation)
for _action in ("UPDATE", "DELETE"):
    event.listen(FactorComputationRun.__table__, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS factor_computation_run_no_{_action.lower()} "
        f"BEFORE {_action} ON factor_computation_run "
        "BEGIN SELECT RAISE(ABORT, 'factor computation evidence is append-only'); END"
    ).execute_if(dialect="sqlite"))
event.listen(FactorComputationRun.__table__, "after_create", DDL(
    "CREATE TRIGGER IF NOT EXISTS factor_computation_run_no_replace "
    "BEFORE INSERT ON factor_computation_run WHEN EXISTS "
    "(SELECT 1 FROM factor_computation_run WHERE id=NEW.id OR capture_id=NEW.capture_id) "
    "BEGIN SELECT RAISE(ABORT, 'factor computation evidence is append-only'); END"
).execute_if(dialect="sqlite"))
