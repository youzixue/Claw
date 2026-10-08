from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.trade_calendar import is_official_closed_day
from app.db.session import Base
from app.models.promotion import (PromotionModelArtifact, PromotionTrainingRun,
                                  PromotionPredictionRun, PromotionPredictionSnapshot)
from app.config.settings import settings
from app.promotion.modeling.ledger_dataset import build_ledger_training_dataset
from app.models.regime import MarketRegimeSnapshot
from app.models.signal import PromotionPredictionRecord
from app.models.stock import LimitUpPool, StockKline
from app.promotion.modeling.acceptance import evaluate_challenger_acceptance
from app.promotion.modeling.dataset import build_training_dataset
from app.promotion.labels import PROMOTION_LABEL_VERSION, promotion_event_label
from app.promotion.modeling.historical_dataset import (
    _Bar,
    _is_ambiguous_pre_reform_st_move,
    _quick_prefilter_scores,
    build_historical_panel_dataset,
)
from app.promotion.modeling.features import (
    FeatureRow,
    FeatureVectorizer,
    extract_point_in_time_features,
    validate_feature_contract,
)
from app.promotion.modeling.logistic import LogisticBinaryClassifier, PlattCalibrator
from app.promotion.modeling.metrics import average_precision, roc_auc
from app.promotion.modeling.training import (
    _historical_universe_rank_metrics,
    _write_artifact_atomic,
    train_promotion_challenger,
)
from app.promotion.modeling.walk_forward import walk_forward_evaluate


@pytest_asyncio.fixture
async def modeling_session(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'promotion_modeling.db'}", future=True
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        yield session, tmp_path
    await engine.dispose()


def _trade_days(start: date, count: int) -> list[date]:
    days = []
    current = start
    while len(days) < count:
        if current.weekday() < 5 and not is_official_closed_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


async def _seed_training_history(session: AsyncSession, *, trade_day_count: int = 45) -> None:
    days = _trade_days(date(2026, 4, 7), trade_day_count)
    for trade_day in days:
        session.add(
            StockKline(
                code="999999",
                trade_date=trade_day,
                open=10,
                high=10.2,
                low=9.8,
                close=10.1,
                volume=1000,
            )
        )
    for trade_day in days:
        session.add(LimitUpPool(code="999999", trade_date=trade_day,
                               consecutive_days=1, source="fixture"))
        for candidate_index in range(16):
            session.add(StockKline(code=f"{candidate_index:06d}", trade_date=trade_day,
                                  open=10, high=10.2, low=9.8, close=10.1,
                                  volume=1000, source="fixture"))
    for day_index, prediction_day in enumerate(days[:-1]):
        recorded_at = datetime.combine(prediction_day, datetime.min.time()).replace(hour=20)
        session.add(PromotionPredictionRun(
            id=day_index + 1, run_key=f"fixture-run-{day_index}",
            snapshot_batch_key=f"batch:{prediction_day}", reference_trade_date=prediction_day,
            as_of_at=recorded_at, created_at=recorded_at, completed_at=recorded_at + timedelta(seconds=1),
            snapshot_source="schedule", snapshot_context="promotion_2000",
            model_version="synthetic_champion", feature_version="fixture_features",
            data_version="fixture_data", status="completed", gate_passed=True,
            candidate_count=16, ranked_count=0, actionable_count=0,
            payload_hash=f"fixture-{day_index}", metadata_json="{}",
        ))
        outcome_day = days[day_index + 1]
        positive_indexes = {
            (day_index * 2) % 16,
            (day_index * 2 + 1) % 16,
        }
        for candidate_index in range(16):
            # Rotate first-board events so a stock is not already limit-up on the
            # prediction day; consecutive hits belong to the T2 lane instead.
            positive = candidate_index in positive_indexes
            code = f"{candidate_index:06d}"
            memory_score = 90.0 - candidate_index if positive else 15.0 + candidate_index
            factors = {
                "prediction_snapshot_source": "schedule",
                "prediction_snapshot_context": "promotion_2000",
                "prediction_snapshot_recorded_at": datetime.combine(
                    prediction_day, datetime.min.time()
                ).replace(hour=20).isoformat(),
                "prediction_snapshot_batch_key": f"batch:{prediction_day}",
                "memory_score": memory_score,
                "route_score": 85.0 if positive else 25.0,
                "kline_confirmation_score": 0.9 if positive else 0.1,
                "strict_ready": positive,
            }
            session.add(
                PromotionPredictionRecord(
                    code=code,
                    name=f"样本{candidate_index}",
                    target_board=1,
                    prediction_trade_date=prediction_day,
                    horizon_days=1,
                    predicted_probability=0.08,
                    calibrated_probability=0.08,
                    candidate_route=(
                        "fresh_mainline_start" if positive else "quiet_setup"
                    ),
                    learning_bucket="synthetic",
                    snapshot_source="schedule",
                    snapshot_context="promotion_2000",
                    snapshot_batch_key=f"batch:{prediction_day}",
                    snapshot_recorded_at=datetime.combine(
                        prediction_day, datetime.min.time()
                    ).replace(hour=20),
                    model_version="synthetic_champion",
                    outcome_status=(
                        "success"
                        if positive and not (day_index == 0 and candidate_index == 0)
                        else "failed"
                    ),
                    outcome_trade_date=outcome_day,
                    actual_limit_up_date=outcome_day if positive else None,
                    factors_json=__import__("json").dumps(factors),
                )
            )
            session.add(PromotionPredictionSnapshot(
                run_id=day_index + 1, record_key=f"{day_index}:{code}",
                code=code, name=f"样本{candidate_index}", target_board=1,
                prediction_trade_date=prediction_day, horizon_days=1,
                calibrated_probability=0.08, raw_probability=0.08,
                candidate_route="fresh_mainline_start" if positive else "quiet_setup",
                rank_scope="pool_unranked", trade_gate_passed=True,
                actionable=False, watch_only=False,
                features_json=__import__("json").dumps(factors), reason_json="{}",
                created_at=recorded_at,
            ))
            if positive:
                session.add(
                    LimitUpPool(
                        code=code,
                        name=f"样本{candidate_index}",
                        trade_date=outcome_day,
                        consecutive_days=1,
                        source="synthetic",
                    )
                )
    await session.commit()
    from test_promotion_ledger_dataset import seal_fixture, calendar_quality_fixture
    from sqlalchemy import text
    await session.execute(text("UPDATE stock_kline SET source=\'ths\', prev_close=close"))
    await session.commit()
    await seal_fixture(session)
    await calendar_quality_fixture(session, days)


async def _seed_historical_panel(session: AsyncSession) -> None:
    days = _trade_days(date(2026, 4, 7), 50)
    for code_index in range(12):
        code = f"000{code_index:03d}"
        previous_close = 10.0 + code_index * 0.1
        for day_index, trade_day in enumerate(days):
            # Deterministic regular-main-board limit events; 5% ambiguous ST
            # moves are intentionally absent from this fixture.
            change = 10.0 if (day_index + code_index) % 13 == 0 else ((day_index + code_index) % 5 - 2) * 0.7
            close = previous_close * (1.0 + change / 100.0)
            session.add(
                StockKline(
                    code=code,
                    trade_date=trade_day,
                    open=previous_close,
                    high=max(previous_close, close) * 1.01,
                    low=min(previous_close, close) * 0.99,
                    close=close,
                    prev_close=previous_close,
                    change_pct=change,
                    volume=100_000 + day_index * 1000 + code_index * 100,
                    amount=20_000_000 + day_index * 100_000,
                    turnover=3.0 + code_index * 0.2,
                    source="ths",
                )
            )
            previous_close = close
    await session.commit()


def test_canonical_promotion_labels_separate_new_first_from_continuation():
    assert PROMOTION_LABEL_VERSION.endswith("_v2")
    assert promotion_event_label(
        target_board=1,
        outcome_limit_up=True,
        prediction_day_limit_up=False,
    ) == 1
    assert promotion_event_label(
        target_board=1,
        outcome_limit_up=True,
        prediction_day_limit_up=True,
    ) == 0
    assert promotion_event_label(
        target_board=2,
        outcome_limit_up=True,
        prediction_day_limit_up=True,
    ) == 1
    assert promotion_event_label(
        target_board=2,
        outcome_limit_up=True,
        prediction_day_limit_up=False,
    ) == 0


def test_feature_contract_excludes_label_and_future_fields():
    validate_feature_contract()
    values = extract_point_in_time_features(
        {
            "memory_score": 70,
            "strict_ready": True,
            "actual_max_change_pct": 10,
            "outcome_status": "success",
            "learning_success_rate": 0.9,
        }
    )
    assert values["memory_score"] == 70
    assert values["strict_ready"] == 1.0
    assert "actual_max_change_pct" not in values
    assert "outcome_status" not in values
    assert "learning_success_rate" not in values


def test_artifact_writer_never_overwrites_registered_model_bytes(tmp_path: Path):
    first_payload = {
        "schema_version": "promotion_model_artifact_v1",
        "model_version": "immutable_test_v1",
        "created_at": "2026-08-29T10:00:00",
        "model": {"coefficient": [1.0]},
    }
    destination = _write_artifact_atomic(
        tmp_path, "immutable_test_v1", first_payload
    )
    original_bytes = destination.read_bytes()

    repeated_payload = {
        **first_payload,
        "created_at": "2026-08-29T10:05:00",
    }
    assert (
        _write_artifact_atomic(tmp_path, "immutable_test_v1", repeated_payload)
        == destination
    )
    assert destination.read_bytes() == original_bytes

    with pytest.raises(ValueError, match="immutable artifact collision"):
        _write_artifact_atomic(
            tmp_path,
            "immutable_test_v1",
            {**repeated_payload, "model": {"coefficient": [2.0]}},
        )
    assert destination.read_bytes() == original_bytes
    assert not list(tmp_path.glob("*.tmp"))


def test_numpy_logistic_classifier_learns_separable_signal():
    matrix = np.asarray([[-2.0], [-1.0], [-0.5], [0.5], [1.0], [2.0]])
    labels = np.asarray([0, 0, 0, 1, 1, 1], dtype=float)
    model = LogisticBinaryClassifier(max_iter=300).fit(matrix, labels)
    probabilities = model.predict_proba(matrix)
    assert probabilities[0] < probabilities[-1]
    assert np.mean(probabilities[:3]) < 0.35
    assert np.mean(probabilities[3:]) > 0.65
    sample_weights = np.where(labels > 0.5, model.positive_weight, 1.0)
    sample_weights = sample_weights / np.mean(sample_weights)
    clipped = np.clip(probabilities, 1e-7, 1.0 - 1e-7)
    expected_loss = np.mean(
        sample_weights
        * -(labels * np.log(clipped) + (1.0 - labels) * np.log(1.0 - clipped))
    ) + 0.5 * model.l2 * float(model.coefficients @ model.coefficients)
    assert model.final_loss == pytest.approx(expected_loss)


def test_platt_calibrator_corrects_prior_shift_without_changing_rank():
    raw_probabilities = np.repeat(np.linspace(0.05, 0.50, 10), 100)
    labels = np.concatenate(
        [
            np.concatenate(
                [
                    np.ones(round(raw_probability / 3 * 100)),
                    np.zeros(100 - round(raw_probability / 3 * 100)),
                ]
            )
            for raw_probability in np.linspace(0.05, 0.50, 10)
        ]
    )

    calibrated = PlattCalibrator().fit(raw_probabilities, labels).transform(
        raw_probabilities
    )

    assert abs(float(np.mean(calibrated)) - float(np.mean(labels))) < 0.01
    assert np.mean((calibrated - labels) ** 2) < np.mean(
        (raw_probabilities - labels) ** 2
    )
    assert np.all(np.diff(calibrated) >= 0)


def test_platt_calibrator_handles_constant_scores_with_imbalanced_labels():
    raw_probabilities = np.full(200, 0.50)
    labels = np.concatenate([np.ones(10), np.zeros(190)])

    calibrated = PlattCalibrator().fit(raw_probabilities, labels).transform(
        raw_probabilities
    )

    assert np.all(np.isfinite(calibrated))
    assert np.mean(calibrated) == pytest.approx(0.05, abs=1e-6)


def test_ranking_metrics_treat_probability_ties_without_order_bias():
    labels = np.asarray([1, 0, 0, 0], dtype=float)
    probabilities = np.asarray([0.25, 0.25, 0.25, 0.25], dtype=float)
    assert average_precision(labels, probabilities) == pytest.approx(0.25)
    assert roc_auc(labels, probabilities) == pytest.approx(0.5)


def test_historical_prefilter_proximity_uses_ma20_not_twenty_day_lag_close():
    start = date(2026, 1, 1)
    bars = [
        _Bar(
            code="000001",
            trade_date=start + timedelta(days=index),
            open=5.0 if index == 0 else 10.0,
            high=5.0 if index == 0 else 10.0,
            low=5.0 if index == 0 else 10.0,
            close=5.0 if index == 0 else 10.0,
            volume=1000.0,
            amount=10_000.0,
            turnover=4.0,
            change_pct=0.0,
            prev_close=5.0 if index <= 1 else 10.0,
        )
        for index in range(21)
    ]

    _momentum, proximity, _reversal, _liquidity = _quick_prefilter_scores(
        bars, len(bars) - 1
    )

    assert proximity == pytest.approx(1.0)


def test_historical_st_like_moves_are_ambiguous_only_before_rule_reform():
    def bar(trade_day: date) -> _Bar:
        return _Bar(
            code="600001",
            trade_date=trade_day,
            open=10.0,
            high=10.5,
            low=10.0,
            close=10.5,
            volume=1000.0,
            amount=10_000.0,
            turnover=2.0,
            change_pct=5.0,
            prev_close=10.0,
        )

    assert _is_ambiguous_pre_reform_st_move(bar(date(2026, 7, 3))) is True
    assert _is_ambiguous_pre_reform_st_move(bar(date(2026, 7, 6))) is False


def test_walk_forward_uses_only_prior_dates_and_reports_champion_comparison():
    rows = []
    for day in range(40):
        trade_date = f"2026-05-{day + 1:02d}"
        for index in range(8):
            label = 1 if index == 0 else 0
            rows.append(
                FeatureRow(
                    code=f"{index:06d}",
                    trade_date=trade_date,
                    target_board=1,
                    label=label,
                    baseline_probability=0.1,
                    candidate_route="strong" if label else "weak",
                    values=extract_point_in_time_features(
                        {"memory_score": 90 if label else 10, "strict_ready": bool(label)}
                    ),
                )
            )
    result = walk_forward_evaluate(
        rows,
        initial_train_days=15,
        validation_days=5,
        step_days=5,
        calibration_days=3,
    )
    assert result["completed_fold_count"] >= 3
    assert result["challenger_metrics"]["average_precision"] > result["champion_metrics"]["average_precision"]
    for fold in result["folds"]:
        if fold["status"] == "completed":
            assert fold["train_end_date"] < fold["validation_start_date"]
            assert fold["fit_contract"]["calibration_end_date"] <= fold["train_end_date"]
            assert fold["fit_contract"]["calibrator"]["slope"] > 0


def test_walk_forward_rejects_overlapping_validation_windows():
    rows = [
        FeatureRow(
            code=f"{index % 4:06d}",
            trade_date=f"2026-06-{index // 4 + 1:02d}",
            target_board=1,
            label=1 if index % 4 == 0 else 0,
            baseline_probability=0.25,
            candidate_route="synthetic",
            values={"memory_score": float(index % 4)},
        )
        for index in range(80)
    ]

    with pytest.raises(ValueError, match="not counted more than once"):
        walk_forward_evaluate(
            rows,
            initial_train_days=12,
            validation_days=5,
            step_days=2,
            calibration_days=3,
        )


def test_acceptance_requires_all_metrics_not_only_precision():
    metrics = {
        "sample_count": 100,
        "positive_count": 30,
        "average_precision": 0.30,
        "brier_score": 0.20,
        "expected_calibration_error": 0.10,
        "daily_rank": {
            "12": {"precision": 0.20},
            "30": {"recall": 0.80},
        },
    }
    baseline = {
        **metrics,
        "average_precision": 0.20,
        "brier_score": 0.10,
        "expected_calibration_error": 0.03,
    }
    result = evaluate_challenger_acceptance(
        {
            "completed_fold_count": 4,
            "validation_trade_day_count": 20,
            "challenger_metrics": metrics,
            "champion_metrics": baseline,
        }
    )
    assert result["passed"] is False
    assert {check["name"] for check in result["checks"] if not check["passed"]} >= {
        "brier_score",
        "calibration_error",
    }


def test_historical_rank_report_never_uses_prefilter_positives_as_universe():
    rank = {
        "5": {
            "selected_count": 10,
            "hit_count": 4,
            "precision": 0.4,
            "recall": 0.5,
        }
    }
    result = _historical_universe_rank_metrics(
        {
            "trade_dates": ["2026-06-01", "2026-06-02"],
            "challenger_metrics": {"daily_rank": rank},
            "champion_metrics": {"daily_rank": rank},
        },
        {
            "daily": [
                {
                    "trade_date": "2026-06-01",
                    "universe_positive_count": 10,
                    "candidate_positive_count": 4,
                },
                {
                    "trade_date": "2026-06-02",
                    "universe_positive_count": 10,
                    "candidate_positive_count": 4,
                },
                {
                    "trade_date": "2026-06-03",
                    "universe_positive_count": 999,
                    "candidate_positive_count": 999,
                },
            ]
        },
    )

    assert result["candidate_prefilter_recall"] == pytest.approx(0.4)
    assert result["challenger"]["5"]["candidate_pool_recall"] == pytest.approx(0.5)
    assert result["challenger"]["5"]["full_universe_recall"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_historical_panel_reconstructs_point_in_time_features(modeling_session):
    session, _tmp_path = modeling_session
    await _seed_historical_panel(session)

    dataset = await build_historical_panel_dataset(
        session,
        target_board=1,
        lookback_trade_days=80,
        history_days=20,
        candidate_limit_per_day=50,
        minimum_universe_count=1,
    )

    assert len(dataset.rows) > 100
    assert dataset.diagnostics["dataset_source"] == "historical_kline_panel"
    assert dataset.diagnostics["trade_day_count"] >= 20
    assert 0 <= dataset.diagnostics["prefilter_recall"] <= 1
    assert 0 <= dataset.diagnostics["ambiguous_st_exclusion_rate"] <= 1
    assert dataset.diagnostics["clock_aligned_sample_count"] >= len(dataset.rows)
    assert "hist_return_20d" in dataset.rows[0].values
    assert dataset.rows[0].trade_date <= dataset.end_date.isoformat()
    assert dataset.data_version.startswith("historical_panel_v3_")

    daily = dataset.diagnostics["daily"]
    assert daily[0]["reference_prior_sample_count"] == 0
    assert daily[0]["reference_base_rate"] == pytest.approx(0.04)
    first_day_rows = [
        row for row in dataset.rows if row.trade_date == daily[0]["trade_date"]
    ]
    assert np.mean([row.baseline_probability for row in first_day_rows]) == pytest.approx(
        0.04
    )
    assert daily[1]["reference_prior_sample_count"] == daily[0]["candidate_count"]
    assert daily[1]["reference_base_rate"] == pytest.approx(
        (2 + daily[0]["candidate_positive_count"])
        / (50 + daily[0]["candidate_count"])
    )


@pytest.mark.asyncio
async def test_dataset_regime_fallback_never_reads_post_cutoff_snapshot(
    modeling_session,
):
    session, _tmp_path = modeling_session
    prediction_day = date(2026, 4, 20)
    outcome_day = date(2026, 4, 21)
    cutoff = datetime(2026, 4, 20, 20, 0)
    session.add_all(
        [
            PromotionPredictionRecord(
                code="000777",
                name="时点样本",
                target_board=1,
                prediction_trade_date=prediction_day,
                horizon_days=1,
                predicted_probability=0.1,
                calibrated_probability=0.1,
                candidate_route="quiet_setup",
                learning_bucket="T1:quiet_setup",
                snapshot_source="schedule",
                snapshot_context="promotion_2000",
                snapshot_batch_key="cutoff-test",
                snapshot_recorded_at=cutoff,
                factors_json=__import__("json").dumps(
                    {
                        "prediction_snapshot_source": "schedule",
                        "prediction_snapshot_context": "promotion_2000",
                        "prediction_snapshot_recorded_at": cutoff.isoformat(),
                        "route_score": 20,
                    }
                ),
            ),
            StockKline(
                code="000777",
                trade_date=outcome_day,
                open=10,
                high=11,
                low=10,
                close=11,
                prev_close=10,
                change_pct=10,
                volume=1000,
            ),
            LimitUpPool(
                code="000777",
                name="时点样本",
                trade_date=outcome_day,
                consecutive_days=1,
                source="test",
            ),
            MarketRegimeSnapshot(
                snapshot_key="regime-before-cutoff",
                trade_date=prediction_day,
                as_of_at=datetime(2026, 4, 20, 19, 0),
                snapshot_context="postmarket",
                regime_version="test-v1",
                data_version="before-v1",
                primary_regime="sector_rotation",
                confidence=0.8,
                quality_status="good",
                input_coverage=1.0,
                universe_count=5000,
                created_at=datetime(2026, 4, 20, 19, 1),
            ),
            MarketRegimeSnapshot(
                snapshot_key="regime-after-cutoff",
                trade_date=prediction_day,
                as_of_at=datetime(2026, 4, 20, 21, 0),
                snapshot_context="postmarket",
                regime_version="test-v1",
                data_version="after-v1",
                primary_regime="risk_off",
                confidence=0.8,
                quality_status="good",
                input_coverage=1.0,
                universe_count=5000,
                created_at=datetime(2026, 4, 20, 21, 1),
            ),
        ]
    )
    await session.commit()

    dataset = await build_training_dataset(session, target_board=1)

    assert len(dataset.rows) == 1
    assert dataset.rows[0].market_regime == "sector_rotation"


@pytest.mark.asyncio
async def test_snapshot_dataset_never_uses_quarantined_limit_events_as_truth(
    modeling_session,
):
    session, _tmp_path = modeling_session
    prediction_day = date(2026, 4, 20)
    outcome_day = date(2026, 4, 21)
    session.add_all(
        [
            StockKline(
                code="999999",
                trade_date=prediction_day,
                open=10,
                high=10.2,
                low=9.8,
                close=10,
                prev_close=10,
                change_pct=0,
                volume=1000,
            ),
            StockKline(
                code="999999",
                trade_date=outcome_day,
                open=10,
                high=11,
                low=10,
                close=11,
                prev_close=10,
                change_pct=10,
                volume=1000,
            ),
            PromotionPredictionRecord(
                code="000888",
                name="隔离真值样本",
                target_board=1,
                prediction_trade_date=prediction_day,
                horizon_days=1,
                predicted_probability=0.2,
                calibrated_probability=0.2,
                candidate_route="quiet_setup",
                learning_bucket="T1:quiet_setup",
                snapshot_source="schedule",
                snapshot_context="promotion_2000",
                snapshot_batch_key="quarantine-test",
                snapshot_recorded_at=datetime(2026, 4, 20, 20, 0),
                model_version="synthetic_champion",
                outcome_status="success",
                outcome_trade_date=outcome_day,
                factors_json=__import__("json").dumps(
                    {
                        "prediction_snapshot_source": "schedule",
                        "prediction_snapshot_context": "promotion_2000",
                        "prediction_snapshot_recorded_at": "2026-04-20T20:00:00",
                        "route_score": 80,
                    }
                ),
            ),
            LimitUpPool(
                code="000888",
                name="隔离真值样本",
                trade_date=outcome_day,
                consecutive_days=1,
                source="synthetic",
                quarantined=True,
            ),
        ]
    )
    await session.commit()

    dataset = await build_training_dataset(session, target_board=1)

    assert len(dataset.rows) == 1
    assert dataset.rows[0].label == 0
    assert dataset.diagnostics["outcome_label_disagreement"] == 1


@pytest.mark.asyncio
async def test_dataset_and_training_pipeline_persist_reproducible_artifact(modeling_session, monkeypatch):
    monkeypatch.setattr(settings, "PROMOTION_SHADOW_MIN_KLINE_ROWS", 1)
    session, tmp_path = modeling_session
    await _seed_training_history(session)
    # Training mechanics fixture only: the real identity gate still validates a
    # synthetic prepared view. This is not production archival/source evidence.
    from app.promotion import identity_evidence
    from test_promotion_identity_evidence import fixture_index
    async def synthetic_identity_index(*, known_cutoff, **kwargs):
        return fixture_index(known_cutoff=known_cutoff,
                             codes=tuple(f"{i:06d}" for i in range(16)))
    monkeypatch.setattr(identity_evidence, "prepare_identity_index", synthetic_identity_index)
    from app.promotion import outcome_materials
    from test_promotion_paired_material import load_material_fixture, outcome_ready_fixture
    async def synthetic_outcome_index(*, known_cutoff, **kwargs):
        return await load_material_fixture(session)
    monkeypatch.setattr(outcome_materials, "prepare_outcome_index", synthetic_outcome_index)
    monkeypatch.setattr(outcome_materials, "outcome_pair_gate", outcome_ready_fixture)

    dataset = await build_training_dataset(session, target_board=1)
    assert len(dataset.rows) == 44 * 16
    assert dataset.diagnostics["positive_count"] == 44 * 2
    assert dataset.diagnostics["excluded_incomplete_horizon"] == 0
    assert dataset.diagnostics["label_source"] == "limit_up_pool_on_stock_kline_session_clock"
    assert dataset.diagnostics["outcome_label_disagreement"] == 1
    assert dataset.diagnostics["immutable_ledger_evidence"] is False
    frozen = await build_ledger_training_dataset(session, target_board=1, as_of_at=datetime(2026, 8, 31, 21))
    assert len(frozen.rows) == 44 * 16
    assert frozen.diagnostics["trade_day_count"] == 44

    response = await train_promotion_challenger(
        session,
        target_board=1,
        initial_train_days=20,
        validation_days=5,
        step_days=5,
        calibration_days=3,
        minimum_samples=100,
        minimum_trade_days=25,
        minimum_positives=20,
        persist=True,
        artifact_dir=tmp_path / "artifacts",
        as_of_at=datetime(2026, 8, 31, 21),
    )

    assert response["status"] == "completed"
    assert response["production_unchanged"] is True
    assert response["model_version"].startswith("promotion_t1_numpy_lr_")
    artifact_path = Path(response["artifact_uri"])
    assert artifact_path.is_file()
    original_bytes = artifact_path.read_bytes()
    repeated = await train_promotion_challenger(
        session,
        target_board=1,
        initial_train_days=20,
        validation_days=5,
        step_days=5,
        calibration_days=3,
        minimum_samples=100,
        minimum_trade_days=25,
        minimum_positives=20,
        persist=True,
        artifact_dir=tmp_path / "artifacts",
        as_of_at=datetime(2026, 8, 31, 21),
    )
    assert repeated["model_version"] == response["model_version"]
    assert artifact_path.read_bytes() == original_bytes
    assert await session.scalar(select(func.count()).select_from(PromotionTrainingRun)) == 2
    assert await session.scalar(select(func.count()).select_from(PromotionModelArtifact)) == 1
