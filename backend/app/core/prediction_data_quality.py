"""Point-in-time data-quality audit and prediction gate."""

from __future__ import annotations

import json
from math import isfinite
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.data.fund_flow_clock import evidence_clock, fund_clock_status, main_fund_values_valid
from app.data.main_fund import main_fund_source_supported
from app.core.trade_calendar import is_official_closed_day
from app.models.governance import DataQualityIssue, DataQualityRun, DataWatermark
from app.models.signal import PromotionPredictionRecord
from app.models.stock import AuctionData, FundFlow, LimitUpPool, StockKline, StockSpot, StockTag


@dataclass(frozen=True, slots=True)
class QualityFinding:
    severity: str
    dataset: str
    issue_type: str
    message: str
    trade_date: date | None = None
    code: str | None = None
    evidence: dict | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"

    def payload(self) -> dict:
        result = asdict(self)
        result["trade_date"] = self.trade_date.isoformat() if self.trade_date else None
        result["evidence"] = self.evidence or {}
        result["blocking"] = self.blocking
        return result


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _status(completeness: float, *, required: bool = True) -> str:
    if completeness >= settings.DATA_COMPLETENESS_MIN:
        return "ok"
    if completeness > 0:
        return "degraded" if not required else "blocked"
    return "missing" if not required else "blocked"


PREDICTION_INTRADAY_CONTEXTS = {
    "promotion_0925",
    "promotion_0935",
    "promotion_1000",
    "promotion_1030",
    "promotion_1305",
}
PREDICTION_AUCTION_CONTEXTS = {"promotion_0925", "promotion_0935"}


# Shared producer identity and data dependency contract. Prediction output
# records must never become an input dependency of their own quality gate.
from app.promotion.route_contract import (
    REQUIRED_DATASETS_BY_ROUTE as PREDICTION_ROUTE_REQUIRED_DATASETS,
    route_contract_payload,
)


def _route_watermark_ok(item) -> bool:
    if not isinstance(item, dict) or item.get("status") != "ok":
        return False
    value = item.get("completeness")
    if isinstance(value, bool):
        return False
    try:
        ratio = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return isfinite(ratio) and float(settings.DATA_COMPLETENESS_MIN) <= ratio <= 1.0


def _parse_auction_time(value: str | None) -> time | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


class PredictionDataQualityAuditor:
    """Build a fail-closed quality snapshot without mutating source rows."""

    async def _latest_kline_date(self, db: AsyncSession) -> date | None:
        return await db.scalar(select(func.max(StockKline.trade_date)))

    async def _latest_dataset_date(
        self,
        db: AsyncSession,
        model,
        on_or_before: date,
    ) -> date | None:
        return await db.scalar(
            select(func.max(model.trade_date)).where(model.trade_date <= on_or_before)
        )

    async def _dataset_count(self, db: AsyncSession, model, trade_date_value: date) -> int:
        stmt = select(func.count()).select_from(model).where(model.trade_date == trade_date_value)
        if model is LimitUpPool:
            stmt = stmt.where(LimitUpPool.quarantined.is_(False))
        return int(await db.scalar(stmt) or 0)

    @staticmethod
    def _tradeable_tag_filters() -> tuple:
        """生产预测只交易主板，质量分母必须与实际信号宇宙一致。"""
        return (
            StockTag.board_tag == "tradeable",
            func.coalesce(StockTag.is_st, False).is_(False),
            func.coalesce(StockTag.is_suspended, False).is_(False),
            func.coalesce(StockTag.is_delisting, False).is_(False),
        )

    async def _tradeable_universe_count(self, db: AsyncSession) -> int:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(StockTag)
                .where(*self._tradeable_tag_filters())
            )
            or 0
        )

    async def _tradeable_dataset_count(
        self,
        db: AsyncSession,
        model,
        trade_date_value: date,
    ) -> int:
        stmt = (
            select(func.count(func.distinct(model.code)))
            .select_from(model)
            .join(StockTag, StockTag.code == model.code)
            .where(
                model.trade_date == trade_date_value,
                *self._tradeable_tag_filters(),
            )
        )
        if model is StockKline:
            # 盘中占位源不能冒充终场/训练日K；NULL保留为历史兼容来源。
            stmt = stmt.where(
                (StockKline.source.is_(None)) | (StockKline.source != "spot_fallback")
            )
        return int(await db.scalar(stmt) or 0)

    async def _recent_peak_count(
        self,
        db: AsyncSession,
        model,
        trade_date_value: date,
        *,
        lookback_days: int = 20,
    ) -> int:
        stmt = (
            select(model.trade_date, func.count())
            .where(
                model.trade_date <= trade_date_value,
                model.trade_date >= trade_date_value - timedelta(days=lookback_days * 2),
            )
            .group_by(model.trade_date)
        )
        if model is LimitUpPool:
            stmt = stmt.where(LimitUpPool.quarantined.is_(False))
        rows = (await db.execute(stmt)).all()
        return max((int(row[1] or 0) for row in rows), default=0)

    async def _build_watermarks(
        self,
        db: AsyncSession,
        trade_date_value: date,
        snapshot_context: str,
        *,
        as_of_at: datetime | None = None,
    ) -> list[dict]:
        decision_at = as_of_at if as_of_at is not None else datetime.now()
        normalized_context = str(snapshot_context or "").lower()
        auction_required = normalized_context in PREDICTION_AUCTION_CONTEXTS
        intraday_context = normalized_context in PREDICTION_INTRADAY_CONTEXTS
        specs = (
            ("stock_kline", StockKline, True),
            ("fund_flow", FundFlow, "2000" in normalized_context),
            ("limit_up_pool", LimitUpPool, True),
            ("auction_data", AuctionData, auction_required),
        )
        watermarks: list[dict] = []
        spot_count = int(await db.scalar(select(func.count()).select_from(StockSpot)) or 0)
        tradeable_universe_count = await self._tradeable_universe_count(db)
        for dataset, model, required in specs:
            dataset_date = trade_date_value
            if intraday_context and dataset != "auction_data":
                # 盘中写入的 StockKline 是实时行情拼成的未完成日线
                # (source=spot_fallback)，不能取代上一完整交易日的正式日K。
                # 即便某个供应商盘中返回了“日线”字段，也仍属于未来尚未收盘的
                # bar；因此这里按日期严格退到目标日前，而不是只靠 source 过滤。
                on_or_before = (
                    trade_date_value - timedelta(days=1)
                    if model is StockKline
                    else trade_date_value
                )
                dataset_date = (
                    await self._latest_dataset_date(db, model, on_or_before)
                    or trade_date_value
                )
            raw_count = await self._dataset_count(db, model, dataset_date)
            count = raw_count
            recent_peak = await self._recent_peak_count(db, model, dataset_date)
            coverage_scope = "all_rows"
            auction_health: dict | None = None
            if dataset == "auction_data":
                # 行数和时间戳不能证明竞价可交易：价格快照可能齐全，但增量竞价量、
                # 金额、量比全为0。质量闸门必须与策略D使用的最终逐股帧健康口径一致。
                from app.strategy.auction import auction_collector

                auction_health = await auction_collector.get_snapshot_health(
                    db, dataset_date, as_of_at=decision_at
                )
                latest_code_count = int(auction_health.get("latest_code_count") or 0)
                count = int(
                    auction_health.get(
                        "multi_frame_complete_count",
                        auction_health.get("feed_complete_count", 0),
                    )
                    or 0
                )
                health_universe_count = int(
                    auction_health.get("tradeable_universe_count") or 0
                )
                expected = (
                    tradeable_universe_count
                    or health_universe_count
                    or max(spot_count, latest_code_count)
                )
                field_coverage = min(count / expected, 1.0) if expected else 0.0
                timely_ratio = float(auction_health.get("verified_timely_snapshot_ratio") or 0.0)
                completeness = min(field_coverage, timely_ratio)
                status = _status(completeness, required=required)
                coverage_scope = "latest_auction_frame_tradeable_required_fields"
            elif dataset == "limit_up_pool":
                expected = None
                completeness = 1.0 if count > 0 else 0.0
                status = "ok" if count > 0 else ("blocked" if required else "missing")
            elif dataset in {"stock_kline", "fund_flow"} and tradeable_universe_count > 0:
                # 晋级预测与模拟交易在主板可交易池内运行。用全市场历史峰值做
                # 分母会把创业板/科创板等观察池缺口误判为生产阻断；这里对齐
                # StockTag 的实际可交易宇宙，同时保留 raw_count 供审计。
                count = await self._tradeable_dataset_count(db, model, dataset_date)
                expected = tradeable_universe_count
                completeness = min(count / expected, 1.0) if expected else 0.0
                status = _status(completeness, required=required)
                coverage_scope = "tradeable_stock_tags"
            else:
                expected = max(recent_peak, spot_count if dataset in {"stock_kline", "fund_flow"} else 0)
                completeness = min(count / expected, 1.0) if expected else 0.0
                status = _status(completeness, required=required)

            max_available_at = None
            details: dict = {
                "required": bool(required),
                "coverage_scope": coverage_scope,
                "raw_record_count": raw_count,
                "tradeable_universe_count": (
                    tradeable_universe_count if "tradeable" in coverage_scope else None
                ),
                "recent_peak_count": recent_peak,
                "requested_trade_date": trade_date_value.isoformat(),
                "dataset_trade_date": dataset_date.isoformat(),
            }
            if dataset == "fund_flow":
                # Raw daily rows stay intact for research. Only proven clocks can
                # count toward a route input; one fresh row cannot wash older rows.
                fund_stmt = select(FundFlow).where(FundFlow.trade_date == dataset_date)
                if tradeable_universe_count > 0:
                    fund_stmt = fund_stmt.join(StockTag, StockTag.code == FundFlow.code).where(
                        *self._tradeable_tag_filters(),
                    )
                fund_rows = list((await db.scalars(fund_stmt)).all())
                require_live = intraday_context and dataset_date == trade_date_value
                clock_counts: dict[str, int] = {}
                usable = []
                for fund in fund_rows:
                    clock_status = fund_clock_status(
                        fund.source_quote_at, fund.received_at, fund.observed_at, dataset_date,
                        decision_at, settings.FUND_FLOW_SOURCE_MAX_AGE_SEC, require_live=require_live,
                    )
                    if clock_status in {"ok", "historical_known"} and not main_fund_values_valid(
                        fund.main_net_inflow, fund.main_net_inflow_pct,
                    ):
                        clock_status = "invalid_values"
                    if (clock_status in {"ok", "historical_known"}
                            and not main_fund_source_supported(fund.source, fund.source_version)):
                        clock_status = "unsupported_source"
                    clock_counts[clock_status] = clock_counts.get(clock_status, 0) + 1
                    if clock_status in {"ok", "historical_known"}:
                        usable.append(fund)
                count = len({fund.code for fund in usable})
                completeness = min(count / expected, 1.0) if expected else 0.0
                status = _status(completeness, required=required)
                max_available_at = max((fund.observed_at for fund in usable), default=None)
                details["clock_status_counts"] = clock_counts
                details["fund_clock_policy"] = "live_source_clock" if require_live else "known_historical_source_clock"
                details["decision_as_of"] = decision_at.isoformat()
                details["source_clock_unknown_not_backfilled"] = True
            if dataset == "stock_kline":
                source_stmt = (
                    select(StockKline.source, func.count(func.distinct(StockKline.code)))
                    .where(StockKline.trade_date == dataset_date)
                    .group_by(StockKline.source)
                )
                if tradeable_universe_count > 0:
                    source_stmt = (
                        select(StockKline.source, func.count(func.distinct(StockKline.code)))
                        .select_from(StockKline)
                        .join(StockTag, StockTag.code == StockKline.code)
                        .where(
                            StockKline.trade_date == dataset_date,
                            *self._tradeable_tag_filters(),
                        )
                        .group_by(StockKline.source)
                    )
                source_rows = (await db.execute(source_stmt)).all()
                details["source_counts"] = {
                    str(source or "legacy_unknown"): int(source_count or 0)
                    for source, source_count in source_rows
                }
                details["spot_fallback_count"] = int(
                    details["source_counts"].get("spot_fallback", 0)
                )
                if intraday_context:
                    details["intraday_daily_bar_policy"] = "previous_completed_session"
            if dataset == "auction_data" and auction_health is not None:
                latest_time = str(auction_health.get("latest_snapshot_time") or "")
                # Legacy HH:MM:SS labels are not verified availability clocks.
                max_available_at = evidence_clock(auction_health.get("max_verified_observed_at"))
                details["latest_auction_time"] = latest_time or None
                details["auction_health"] = auction_health

            watermarks.append(
                {
                    "dataset": dataset,
                    "trade_date": dataset_date.isoformat(),
                    "observed_at": datetime.now().isoformat(timespec="seconds"),
                    "max_available_at": max_available_at.isoformat() if max_available_at else None,
                    "record_count": count,
                    "expected_count": expected,
                    "completeness": round(completeness, 6),
                    "status": status,
                    "details": details,
                }
            )
        return watermarks

    async def _calendar_findings(
        self,
        db: AsyncSession,
        trade_date_value: date,
        *,
        lookback_days: int,
        snapshot_context: str = "",
    ) -> list[QualityFinding]:
        start = trade_date_value - timedelta(days=max(lookback_days * 2, 60))
        limit_dates = {
            row[0]
            for row in (
                await db.execute(
                    select(LimitUpPool.trade_date)
                    .where(
                        LimitUpPool.trade_date >= start,
                        LimitUpPool.quarantined.is_(False),
                    )
                    .distinct()
                )
            ).all()
            if row[0]
        }
        kline_dates = {
            row[0]
            for row in (
                await db.execute(
                    select(StockKline.trade_date)
                    .where(StockKline.trade_date >= start)
                    .distinct()
                )
            ).all()
            if row[0]
        }
        normalized_context = str(snapshot_context or "").strip().lower()
        intraday_context = normalized_context in PREDICTION_INTRADAY_CONTEXTS

        def is_invalid_limit_pool_date(day: date) -> bool:
            if day.weekday() >= 5 or is_official_closed_day(day):
                return True
            if day in kline_dates:
                return False
            # 正式盘中快照时，当天涨停池可能已由实时行情先行写入，而日K线
            # 只有收盘后才会落库。该时点差不是脏日期；仅放行本次审计目标日，
            # 历史缺口和收盘快照仍继续失败关闭。
            return not (intraday_context and day == trade_date_value)

        invalid = sorted(day for day in limit_dates if is_invalid_limit_pool_date(day))
        if not invalid:
            return []
        counts = dict(
            (
                await db.execute(
                    select(LimitUpPool.trade_date, func.count())
                    .where(
                        LimitUpPool.trade_date.in_(invalid),
                        LimitUpPool.quarantined.is_(False),
                    )
                    .group_by(LimitUpPool.trade_date)
                )
            ).all()
        )
        return [
            QualityFinding(
                severity="blocking",
                dataset="limit_up_pool",
                issue_type="non_trading_or_unobserved_date",
                trade_date=day,
                message=f"涨停池日期 {day} 不在权威K线交易日中，共 {int(counts.get(day, 0))} 条",
                evidence={"record_count": int(counts.get(day, 0)), "has_kline": day in kline_dates},
            )
            for day in invalid
        ]

    async def _outcome_date_findings(
        self,
        db: AsyncSession,
        trade_date_value: date,
        *,
        lookback_days: int,
    ) -> list[QualityFinding]:
        start = trade_date_value - timedelta(days=max(lookback_days * 2, 120))
        market_dates = sorted(
            row[0]
            for row in (
                await db.execute(
                    select(StockKline.trade_date)
                    .where(StockKline.trade_date >= start)
                    .distinct()
                    .order_by(StockKline.trade_date)
                )
            ).all()
            if row[0] and row[0].weekday() < 5 and not is_official_closed_day(row[0])
        )
        records = list(
            (
                await db.execute(
                    select(PromotionPredictionRecord).where(
                        PromotionPredictionRecord.prediction_trade_date >= start,
                        PromotionPredictionRecord.outcome_status.in_(["success", "failed"]),
                        PromotionPredictionRecord.outcome_trade_date.is_not(None),
                    )
                )
            ).scalars().all()
        )
        invalid: list[tuple[PromotionPredictionRecord, date | None]] = []
        for record in records:
            index = bisect_right(market_dates, record.prediction_trade_date)
            horizon = max(int(record.horizon_days or 1), 1)
            expected_index = index + horizon - 1
            expected = market_dates[expected_index] if expected_index < len(market_dates) else None
            if expected and record.outcome_trade_date != expected:
                invalid.append((record, expected))
        if not invalid:
            return []
        examples = [
            {
                "id": record.id,
                "code": record.code,
                "prediction_trade_date": record.prediction_trade_date,
                "actual_outcome_date": record.outcome_trade_date,
                "expected_outcome_date": expected,
            }
            for record, expected in invalid[:20]
        ]
        return [
            QualityFinding(
                severity="blocking",
                dataset="promotion_prediction_record",
                issue_type="wrong_outcome_trade_date",
                trade_date=trade_date_value,
                message=f"发现 {len(invalid)} 条预测结果日不等于官方交易日 horizon",
                evidence={"invalid_count": len(invalid), "examples": examples},
            )
        ]

    async def _snapshot_cutoff_findings(
        self,
        db: AsyncSession,
        trade_date_value: date,
        *,
        lookback_days: int,
    ) -> list[QualityFinding]:
        start = trade_date_value - timedelta(days=max(lookback_days * 2, 60))
        records = list(
            (
                await db.execute(
                    select(PromotionPredictionRecord).where(
                        PromotionPredictionRecord.prediction_trade_date >= start,
                        PromotionPredictionRecord.snapshot_recorded_at.is_not(None),
                        PromotionPredictionRecord.snapshot_context.in_([
                            "promotion_1510",
                            "promotion_2000",
                            *sorted(PREDICTION_INTRADAY_CONTEXTS),
                        ]),
                    )
                )
            ).scalars().all()
        )
        windows = {
            "promotion_1510": (time(15, 5), time(16, 30)),
            "promotion_2000": (time(19, 50), time(21, 30)),
            # 与 official promotion_0925 语义窗口一致；09:25-09:35 的
            # 重启补跑允许到09:40，不能把合法 immutable catch-up 误报为越界。
            "promotion_0925": (time(9, 20), time(9, 40)),
            "promotion_0935": (time(9, 30), time(9, 50)),
            "promotion_1000": (time(9, 55), time(10, 15)),
            "promotion_1030": (time(10, 25), time(10, 45)),
            "promotion_1305": (time(13, 0), time(13, 20)),
        }
        invalid = []
        for record in records:
            lower, upper = windows[record.snapshot_context]
            recorded_time = record.snapshot_recorded_at.time()
            if not lower <= recorded_time <= upper:
                invalid.append(record)
        if not invalid:
            return []
        return [
            QualityFinding(
                severity="warning",
                dataset="promotion_prediction_record",
                issue_type="snapshot_outside_context_window",
                trade_date=trade_date_value,
                message=f"发现 {len(invalid)} 条快照记录时间不在声明阶段窗口内",
                evidence={
                    "invalid_count": len(invalid),
                    "examples": [
                        {
                            "id": record.id,
                            "context": record.snapshot_context,
                            "recorded_at": record.snapshot_recorded_at,
                        }
                        for record in invalid[:20]
                    ],
                },
            )
        ]

    async def _persist(
        self,
        db: AsyncSession,
        *,
        run: DataQualityRun,
        findings: list[QualityFinding],
        watermarks: list[dict],
    ) -> None:
        db.add(run)
        await db.flush()
        for finding in findings:
            db.add(
                DataQualityIssue(
                    run_id=run.id,
                    severity=finding.severity,
                    dataset=finding.dataset,
                    issue_type=finding.issue_type,
                    trade_date=finding.trade_date,
                    code=finding.code,
                    message=finding.message,
                    evidence_json=_json(finding.evidence or {}),
                )
            )
        for item in watermarks:
            trade_date_value = date.fromisoformat(item["trade_date"])
            row = await db.scalar(
                select(DataWatermark).where(
                    DataWatermark.dataset == item["dataset"],
                    DataWatermark.trade_date == trade_date_value,
                )
            )
            if row is None:
                row = DataWatermark(dataset=item["dataset"], trade_date=trade_date_value)
                db.add(row)
            row.observed_at = datetime.fromisoformat(item["observed_at"])
            row.max_available_at = (
                datetime.fromisoformat(item["max_available_at"])
                if item["max_available_at"]
                else None
            )
            row.record_count = item["record_count"]
            row.expected_count = item["expected_count"]
            row.completeness = item["completeness"]
            row.status = item["status"]
            row.details_json = _json(item["details"])
        await db.commit()

    @staticmethod
    def _build_route_gates(
        findings: list[QualityFinding],
        watermarks: list[dict],
    ) -> dict[str, dict]:
        """按路线依赖失败关闭；即使数据集全局可选，路线要求也必须为ok。"""
        known_datasets = set().union(*PREDICTION_ROUTE_REQUIRED_DATASETS.values())
        watermark_by_dataset = {
            str(item.get("dataset")): item for item in watermarks
        }
        route_gates: dict[str, dict] = {}
        for route, required_datasets in PREDICTION_ROUTE_REQUIRED_DATASETS.items():
            blockers = [
                finding
                for finding in findings
                if finding.blocking
                and (
                    finding.dataset in required_datasets
                    or finding.dataset not in known_datasets
                )
            ]
            degraded_required = [
                dataset
                for dataset in required_datasets
                if not _route_watermark_ok(watermark_by_dataset.get(dataset))
            ]
            gate_passed = not blockers and not degraded_required
            blocking_datasets = {
                item.dataset for item in blockers
            } | set(degraded_required)
            route_gates[route] = {
                "gate_passed": gate_passed,
                "status": "ok" if gate_passed else "blocked",
                "blocking_count": len(blockers) + len(degraded_required),
                "required_datasets": sorted(required_datasets),
                "blocking_datasets": sorted(blocking_datasets),
                "blocking_issue_types": sorted(
                    {item.issue_type for item in blockers}
                    | ({"route_required_watermark_not_ok"} if degraded_required else set())
                ),
            }
        return route_gates

    async def audit(
        self,
        db: AsyncSession,
        *,
        trade_date_value: date | None = None,
        snapshot_context: str = "",
        persist: bool = True,
        lookback_days: int = 120,
        as_of_at: datetime | None = None,
    ) -> dict:
        normalized_context = str(snapshot_context or "").strip().lower()
        if trade_date_value is not None:
            target_date = trade_date_value
        elif normalized_context in PREDICTION_INTRADAY_CONTEXTS:
            # 盘前当天尚无日K；治理页/手工刷新若回退到“最新日K日”，会把
            # 昨日数据误当作今日闸门。历史回放应显式传 trade_date_value。
            today = date.today()
            target_date = (
                today
                if today.weekday() < 5 and not is_official_closed_day(today)
                else await self._latest_kline_date(db) or today
            )
        else:
            target_date = await self._latest_kline_date(db) or date.today()
        started_at = datetime.now()
        watermarks = await self._build_watermarks(
            db, target_date, snapshot_context, as_of_at=as_of_at if as_of_at is not None else started_at,
        )
        findings = [
            *await self._calendar_findings(
                db,
                target_date,
                lookback_days=lookback_days,
                snapshot_context=snapshot_context,
            ),
            *await self._outcome_date_findings(db, target_date, lookback_days=lookback_days),
            *await self._snapshot_cutoff_findings(db, target_date, lookback_days=lookback_days),
        ]
        for watermark in watermarks:
            if watermark["status"] == "blocked":
                findings.append(
                    QualityFinding(
                        severity="blocking",
                        dataset=watermark["dataset"],
                        issue_type="incomplete_watermark",
                        trade_date=target_date,
                        message=(
                            f"{watermark['dataset']} 完整度 {watermark['completeness']:.1%} "
                            f"({watermark['record_count']}/{watermark['expected_count'] or '--'})"
                        ),
                        evidence=watermark,
                    )
                )
        blocking_count = sum(1 for finding in findings if finding.blocking)
        gate_passed = blocking_count == 0
        route_gates = self._build_route_gates(findings, watermarks)
        summary = {
            "trade_date": target_date.isoformat(),
            "snapshot_context": snapshot_context,
            "gate_passed": gate_passed,
            "status": "ok" if gate_passed else "blocked",
            "issue_count": len(findings),
            "blocking_count": blocking_count,
            "route_contract": route_contract_payload(),
            "route_gates": route_gates,
            "watermarks": watermarks,
            "issues": [finding.payload() for finding in findings],
            "started_at": started_at.isoformat(timespec="seconds"),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        if persist:
            run = DataQualityRun(
                run_type="prediction_gate",
                trade_date=target_date,
                snapshot_context=snapshot_context,
                status=summary["status"],
                gate_passed=gate_passed,
                issue_count=len(findings),
                blocking_count=blocking_count,
                summary_json=_json(summary),
                started_at=started_at,
                completed_at=datetime.now(),
            )
            await self._persist(db, run=run, findings=findings, watermarks=watermarks)
            summary["run_id"] = run.id
        return summary


prediction_data_quality_auditor = PredictionDataQualityAuditor()
