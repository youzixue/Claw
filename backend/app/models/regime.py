"""Immutable point-in-time market-regime snapshots."""

from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, Index, Integer, String, Text, event

from app.db.session import Base


class MarketRegimeSnapshot(Base):
    """Explainable market-style classification known at a declared as-of time."""

    __tablename__ = "market_regime_snapshot"
    __table_args__ = (
        Index(
            "ix_market_regime_snapshot_lookup",
            "trade_date",
            "snapshot_context",
            "created_at",
        ),
        Index(
            "ix_market_regime_snapshot_regime",
            "primary_regime",
            "trade_date",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_key = Column(String(64), nullable=False, unique=True, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    as_of_at = Column(DateTime, nullable=False)
    snapshot_context = Column(String(30), nullable=False, default="postmarket")
    regime_version = Column(String(80), nullable=False)
    data_version = Column(String(80), nullable=False)
    primary_regime = Column(String(40), nullable=False, index=True)
    secondary_regime = Column(String(40))
    previous_regime = Column(String(40))
    transition_type = Column(String(30), nullable=False, default="initial")
    confidence = Column(Float, nullable=False, default=0.0)
    quality_status = Column(String(20), nullable=False, default="partial")
    input_coverage = Column(Float, nullable=False, default=0.0)
    universe_count = Column(Integer, nullable=False, default=0)
    scores_json = Column(Text, nullable=False, default="{}")
    features_json = Column(Text, nullable=False, default="{}")
    evidence_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.now)


def _reject_regime_mutation(_mapper, _connection, _target) -> None:
    raise RuntimeError(
        "MarketRegimeSnapshot is append-only; create a new versioned snapshot instead"
    )


event.listen(MarketRegimeSnapshot, "before_update", _reject_regime_mutation)
event.listen(MarketRegimeSnapshot, "before_delete", _reject_regime_mutation)
