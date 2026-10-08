"""不可变行情轮次元数据、Parquet 分时归档与同日状态恢复。"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.trade_calendar import TRADE_SESSIONS
from app.data.fund_flow_clock import local_clock
from app.models.stock import QuoteRound, StockSpot


_COMPACT_COLUMNS = (
    "quote_round_id",
    "code",
    "name",
    "price",
    "prev_close",
    "open",
    "high",
    "low",
    "change_pct",
    "volume",
    "amount",
    "avg_price",  # 来源明确的当日累计VWAP；不可用缺失量额反算伪造
    "limit_up",   # 前向归档真实限价；旧档缺列不回填
    "limit_down",
    "source_quote_at",
    "received_at",
    "updated_at",
    "committed_at",
)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def quote_config_version() -> str:
    """只哈希行情/撮合相关公开配置，绝不把密钥写入审计记录。"""
    payload = {
        "app_version": settings.APP_VERSION,
        "quote_min_coverage": settings.QUOTE_ROUND_MIN_COVERAGE,
        "source_time_min_coverage": settings.QUOTE_ROUND_MIN_SOURCE_TIME_COVERAGE,
        "source_skew_sec": settings.QUOTE_ROUND_MAX_SOURCE_SKEW_SEC,
        "quote_max_age_sec": settings.PAPER_EXECUTION_QUOTE_MAX_AGE_SEC,
        "source_quality_policy": "fresh_coverage_with_per_stock_clock_guard_v1",
        "main_fund_policy": "tencent_quote_not_a_main_fund_source_v1",
        "limit_pool_policy": "tencent_limit_state_v1+wencai_dated_limit_details_v1",
        "limit_pool_max_age_sec": settings.LIMIT_POOL_SOURCE_MAX_AGE_SEC,
        "limit_pool_detail_max_age_sec": settings.LIMIT_POOL_WENCAI_INTERVAL_SEC,
        "slippage_pct": settings.PAPER_EXECUTION_SLIPPAGE_PCT,
        "commission_rate": settings.PAPER_COMMISSION_RATE,
        "minimum_commission": settings.PAPER_MIN_COMMISSION,
        "stamp_tax_rate": settings.PAPER_STAMP_TAX_RATE,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"quote-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def quote_code_version() -> str:
    configured = str(settings.QUOTE_ROUND_CODE_VERSION or "").strip()
    return configured or f"claw-{settings.APP_VERSION}"


def quote_round_continuity(
    previous_committed_at: datetime | None,
    committed_at: datetime,
) -> dict[str, Any]:
    """审计已提交轮次间的交易时段缺口，不把单轮新鲜度当作路径连续性。

    只扣除日历已定义的计划休市区间；不得填补缺失报价或放宽策略门槛。
    本证据描述轮次，不替代各股票源时钟及各策略自己的连续窗口检查。
    """
    previous = local_clock(previous_committed_at)
    current = local_clock(committed_at)
    evidence = {
        "policy": "active_session_intercommit_v1",
        "scope": "committed_rounds_only",
        "status": "unknown",
        "previous_committed_at": previous.isoformat() if previous else None,
        "committed_at": current.isoformat() if current else None,
        "wall_gap_sec": None,
        "active_gap_sec": None,
        # 2026-09-18：本值是**通用**报价轮次连续性阈值，不再复用 momentum 专用阈值
        "max_gap_sec": int(settings.QUOTE_ROUND_CONTINUITY_MAX_GAP_SEC),
        "threshold_consumer": "quote_round_continuity",
        "per_stock_clock_check_required": True,
    }
    if current is None or previous is None:
        return evidence
    if previous >= current:
        evidence["status"] = "invalid_clock"
        return evidence
    if previous.date() != current.date():
        evidence["status"] = "new_trade_date"
        return evidence
    active_seconds = 0.0
    for name in ("pre_auction", "morning", "afternoon"):
        start, end = TRADE_SESSIONS[name]
        left = max(previous, datetime.combine(current.date(), start))
        right = min(current, datetime.combine(current.date(), end))
        active_seconds += max(0.0, (right - left).total_seconds())
    evidence.update({
        "status": "gap" if active_seconds > evidence["max_gap_sec"] else "continuous",
        "wall_gap_sec": round((current - previous).total_seconds(), 3),
        "active_gap_sec": round(active_seconds, 3),
    })
    return evidence


def build_quote_round_record(
    records: list[dict],
    *,
    expected_count: int,
    committed_at: datetime,
    component_watermarks: dict[str, Any] | None = None,
    previous_committed_at: datetime | None = None,
) -> dict[str, Any]:
    """由一批已收到、即将同事务提交的报价生成不可变清单。"""
    round_id = f"qr-{committed_at:%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:8]}"
    source_times = [
        value
        for value in (_as_datetime(item.get("source_quote_at")) for item in records)
        if value is not None
    ]
    received_times = [
        value
        for value in (_as_datetime(item.get("received_at")) for item in records)
        if value is not None
    ]
    received_count = len(records)
    expected = max(int(expected_count or 0), 0)
    coverage = received_count / expected if expected else 0.0
    source_coverage = len(source_times) / received_count if received_count else 0.0
    # 全市场包含ST研究/零成交报价，少量09:00旧时钟不能拖停其他99%股票。
    # 轮次只认证新鲜覆盖比例；原始min/max完整保留，逐股执行仍检查源时钟。
    max_age = max(0, int(settings.PAPER_EXECUTION_QUOTE_MAX_AGE_SEC))
    fresh_times = [value for value in source_times
                   if value.date() == committed_at.date()
                   and -10 <= (committed_at - value).total_seconds() <= max_age]
    fresh_coverage = len(fresh_times) / expected if expected else 0.0
    source_min = min(source_times) if source_times else None
    source_max = max(source_times) if source_times else None
    received_min = min(received_times) if received_times else None
    received_max = max(received_times) if received_times else None
    # as_of是本轮新鲜报价的最新源时点；不能被孤立未来/旧日异常时钟推移。
    # 没有新鲜报价时仍保留观测值，但该轮一定quality=degraded。
    as_of_at = max(fresh_times) if fresh_times else source_max or received_max or committed_at
    skew_sec = ((max(fresh_times) - min(fresh_times)).total_seconds()
                if fresh_times else None)
    reasons: list[str] = []
    if coverage < float(settings.QUOTE_ROUND_MIN_COVERAGE):
        reasons.append(
            f"coverage={coverage:.4f}<{float(settings.QUOTE_ROUND_MIN_COVERAGE):.4f}"
        )
    if source_coverage < float(settings.QUOTE_ROUND_MIN_SOURCE_TIME_COVERAGE):
        reasons.append(
            "source_time_coverage="
            f"{source_coverage:.4f}<{float(settings.QUOTE_ROUND_MIN_SOURCE_TIME_COVERAGE):.4f}"
        )
    if fresh_coverage < float(settings.QUOTE_ROUND_MIN_SOURCE_TIME_COVERAGE):
        reasons.append(f"fresh_source_coverage={fresh_coverage:.4f}<{float(settings.QUOTE_ROUND_MIN_SOURCE_TIME_COVERAGE):.4f}")
    if skew_sec is None:
        reasons.append("fresh_source_time_missing")
    elif skew_sec > int(settings.QUOTE_ROUND_MAX_SOURCE_SKEW_SEC):
        reasons.append(
            f"source_time_skew={skew_sec:.1f}s>{int(settings.QUOTE_ROUND_MAX_SOURCE_SKEW_SEC)}s"
        )
    if as_of_at.date() != committed_at.date():
        reasons.append(
            f"as_of_trade_date={as_of_at.date().isoformat()}!=commit_date={committed_at.date().isoformat()}"
        )

    for item in records:
        item["quote_round_id"] = round_id
        item["updated_at"] = committed_at

    return {
        "round_id": round_id,
        "trade_date": committed_at.date(),
        "source": "tencent",
        "source_min_at": source_min,
        "source_max_at": source_max,
        "received_min_at": received_min,
        "received_max_at": received_max,
        "committed_at": committed_at,
        "as_of_at": as_of_at,
        "expected_count": expected,
        "received_count": received_count,
        "source_time_count": len(source_times),
        "coverage": round(coverage, 6),
        "source_time_coverage": round(source_coverage, 6),
        "quality_status": "ok" if not reasons else "degraded",
        "quality_reason": ";".join(reasons),
        "component_watermarks_json": json.dumps(
            {**(component_watermarks or {}),
             "source_quote_continuity": quote_round_continuity(previous_committed_at, committed_at),
             "source_quote_quality": {
                "policy": "fresh_coverage_with_per_stock_clock_guard_v1",
                "fresh_count": len(fresh_times), "stale_or_future_count": len(source_times) - len(fresh_times),
                "missing_time_count": received_count - len(source_times),
                "fresh_coverage": round(fresh_coverage, 6), "max_age_sec": max_age,
                "source_skew_scope": "fresh_quotes_only",
            }}, ensure_ascii=False, default=str, sort_keys=True
        ),
        "config_version": quote_config_version(),
        "code_version": quote_code_version(),
        "archive_status": "pending" if settings.QUOTE_ROUND_ARCHIVE_ENABLED else "disabled",
        "focus_count": 0,
    }


async def load_latest_healthy_quote_payload(
    db: AsyncSession,
    *,
    trade_day: date,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """重建最近已提交轮次的 owned payload；绝不把不同 round 的 latest 行拼在一起。"""
    observed_at = now or datetime.now()
    round_row = await db.scalar(
        select(QuoteRound)
        .where(
            QuoteRound.trade_date == trade_day,
            QuoteRound.quality_status == "ok",
        )
        .order_by(desc(QuoteRound.committed_at))
        .limit(1)
    )
    if round_row is None:
        return None
    age = (observed_at - round_row.committed_at).total_seconds()
    if age < -10 or age > max(
        60,
        int(settings.PAPER_EXECUTION_QUOTE_MAX_AGE_SEC),
    ):
        return None
    rows = list(
        (
            await db.scalars(
                select(StockSpot).where(
                    StockSpot.quote_round_id == round_row.round_id
                )
            )
        ).all()
    )
    required_count = max(1, int(round_row.received_count or 0))
    if len(rows) != required_count:
        logger.warning(
            "不可变轮次明细不完整，拒绝重建: "
            f"round={round_row.round_id} rows={len(rows)} "
            f"expected={round_row.received_count}"
        )
        return None
    columns = [column.name for column in StockSpot.__table__.columns]
    records = [
        # Mask the legacy mis-mapped spot field in this owned projection only;
        # neither stored historical rows nor immutable archives are rewritten.
        {column: (None if column == "main_net_inflow" else getattr(row, column))
         for column in columns}
        for row in rows
    ]
    return {
        "round_id": round_row.round_id,
        "trade_date": round_row.trade_date,
        "source": round_row.source,
        "source_min_at": round_row.source_min_at,
        "source_max_at": round_row.source_max_at,
        "received_min_at": round_row.received_min_at,
        "received_max_at": round_row.received_max_at,
        "committed_at": round_row.committed_at,
        "as_of_at": round_row.as_of_at,
        "expected_count": round_row.expected_count,
        "received_count": round_row.received_count,
        "source_time_count": round_row.source_time_count,
        "coverage": round_row.coverage,
        "source_time_coverage": round_row.source_time_coverage,
        "quality_status": round_row.quality_status,
        "quality_reason": round_row.quality_reason,
        "component_watermarks_json": round_row.component_watermarks_json,
        "config_version": round_row.config_version,
        "code_version": round_row.code_version,
        "records": records,
        "records_by_code": {
            str(item["code"]): item
            for item in records
            if item.get("code")
        },
    }


class QuoteRoundArchive:
    """文件归档器；调用方应放在线程中，避免阻塞30秒采集事件循环。"""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or settings.QUOTE_ROUND_ARCHIVE_DIR)
        self._last_retention_date: date | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _frame(records: Iterable[dict], columns: Iterable[str] | None = None) -> pd.DataFrame:
        rows = list(records)
        frame = pd.DataFrame(rows)
        if columns is not None:
            for column in columns:
                if column not in frame.columns:
                    frame[column] = None
            frame = frame[list(columns)]
        for column in ("source_quote_at", "received_at", "updated_at", "committed_at"):
            if column in frame.columns:
                frame[column] = frame[column].map(
                    lambda value: value.isoformat() if isinstance(value, datetime) else value
                )
                # 缺失必须保留 ISO 字符串形态，分钟缓冲判定才不会被 None 拖崩。
                frame[column] = frame[column].fillna("")
        return frame

    def _minute_path(self, minute_key: tuple[date, str]) -> Path:
        trade_day, minute = minute_key
        return (
            self.root
            / "minute"
            / f"trade_date={trade_day.isoformat()}"
            / f"minute={minute}.parquet"
        )

    @staticmethod
    def _write_frame(frame: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        frame.to_parquet(temp, engine="pyarrow", compression="zstd", index=False)
        temp.replace(path)

    def _write_minute(self, committed_at: datetime, compact: pd.DataFrame) -> Path:
        minute_key = (committed_at.date(), committed_at.strftime("%H%M"))
        partition = self.root / "compact" / f"trade_date={committed_at.date().isoformat()}"
        # round_id由build_quote_round_record编码提交分钟。每次从该分钟已落盘的
        # 不可变轮次重建，通常仅2轮；乱序、重启、重复任务不会丢失早先样本。
        # 读取异常直接报告归档失败，不能把残缺片段覆盖成“完整”分钟。
        paths = sorted(partition.glob(f"qr-{committed_at:%Y%m%dT%H%M}*.parquet"))
        frames = [pd.read_parquet(path, engine="pyarrow") for path in paths]
        combined = pd.concat([*frames, compact], ignore_index=True)
        combined = combined.drop_duplicates(["quote_round_id", "code"], keep="last")
        combined["_observed"] = pd.to_datetime(combined["updated_at"], errors="coerce")
        combined = combined.sort_values(["code", "_observed", "quote_round_id"])
        grouped = combined.groupby("code", as_index=False).agg(
            open=("price", "first"), high=("price", "max"), low=("price", "min"),
            price_samples=("price", "count"), first_observed_at=("updated_at", "first"),
        )
        # GroupBy.last会跳过null，导致把上一轮VWAP冒充最新值；必须取完整末行，
        # 保留该轮缺失字段。volume/amount仍为全日累计值，非本分钟增量。
        last_columns = ["code", "name", "price", "prev_close", "change_pct", "volume",
                        "amount", "avg_price", "source_quote_at", "received_at",
                        "updated_at", "quote_round_id"]
        latest = combined.drop_duplicates("code", keep="last")[last_columns].rename(
            columns={"price": "close"})
        grouped = grouped.merge(latest, on="code", how="left")
        grouped["minute"] = committed_at.replace(second=0, microsecond=0).isoformat()
        grouped["price_basis"] = "sampled_quote_prices"
        grouped["volume_basis"] = "cumulative_session"
        path = self._minute_path(minute_key)
        self._write_frame(grouped, path)
        return path

    def _enforce_retention(self, current_date: date) -> None:
        if self._last_retention_date == current_date:
            return
        self._last_retention_date = current_date
        for family, keep in (
            ("compact", int(settings.QUOTE_ROUND_ARCHIVE_RETENTION_TRADING_DAYS)),
            ("focus", int(settings.QUOTE_ROUND_ARCHIVE_RETENTION_TRADING_DAYS)),
            ("minute", int(settings.QUOTE_ROUND_MINUTE_RETENTION_TRADING_DAYS)),
        ):
            base = self.root / family
            if not base.exists():
                continue
            partitions = sorted(
                path for path in base.glob("trade_date=*") if path.is_dir()
            )
            for expired in partitions[:-max(keep, 1)]:
                shutil.rmtree(expired, ignore_errors=True)

    def write_round(
        self,
        round_record: dict[str, Any],
        records: list[dict],
        *,
        focus_codes: set[str] | None = None,
    ) -> dict[str, Any]:
        # 调度器会把多个30秒轮次放入工作线程；分钟聚合缓冲和原子替换必须串行。
        with self._lock:
            return self._write_round_unlocked(
                round_record,
                records,
                focus_codes=focus_codes,
            )

    def _write_round_unlocked(
        self,
        round_record: dict[str, Any],
        records: list[dict],
        *,
        focus_codes: set[str] | None = None,
    ) -> dict[str, Any]:
        committed_at = round_record["committed_at"]
        trade_day = round_record["trade_date"]
        round_id = str(round_record["round_id"])
        compact = self._frame(records, _COMPACT_COLUMNS)
        compact["committed_at"] = committed_at.isoformat()
        compact_path = (
            self.root
            / "compact"
            / f"trade_date={trade_day.isoformat()}"
            / f"{round_id}.parquet"
        )
        self._write_frame(compact, compact_path)

        focus_set = {str(code) for code in (focus_codes or set()) if code}
        focus_rows = [item for item in records if str(item.get("code") or "") in focus_set]
        focus_path: Path | None = None
        if focus_rows:
            focus_path = (
                self.root
                / "focus"
                / f"trade_date={trade_day.isoformat()}"
                / f"{round_id}.parquet"
            )
            self._write_frame(self._frame(focus_rows), focus_path)
        minute_path = self._write_minute(committed_at, compact)
        self._enforce_retention(trade_day)
        return {
            "archive_status": "ready",
            "archive_path": str(compact_path),
            "minute_archive_path": str(minute_path),
            "focus_path": str(focus_path) if focus_path else None,
            "focus_count": len(focus_rows),
        }

    def restore_recent_batches(
        self,
        trade_day: date,
        *,
        minutes: int | None = None,
    ) -> list[list[dict]]:
        """读取同日最近N分钟批次；文件内容仍按原 round 分组。"""
        with self._lock:
            return self._restore_recent_batches_unlocked(
                trade_day,
                minutes=minutes,
            )

    def _restore_recent_batches_unlocked(
        self,
        trade_day: date,
        *,
        minutes: int | None = None,
    ) -> list[list[dict]]:
        partition = self.root / "compact" / f"trade_date={trade_day.isoformat()}"
        if not partition.exists():
            return []
        keep_minutes = max(int(minutes or settings.QUOTE_ROUND_RESTORE_MINUTES), 1)
        paths = sorted(partition.glob("qr-*.parquet"))[-(keep_minutes * 3 + 2):]
        frames: list[tuple[datetime, list[dict]]] = []
        for path in paths:
            try:
                frame = pd.read_parquet(path, engine="pyarrow")
            except Exception as exc:
                logger.warning(f"跳过损坏的行情轮次归档 {path}: {exc}")
                continue
            records = frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records")
            if not records:
                continue
            observed_values = [
                value
                for value in (
                    _as_datetime(item.get("updated_at")) for item in records
                )
                if value is not None
            ]
            observed = max(observed_values) if observed_values else None
            if observed is not None:
                frames.append((observed, records))
        if not frames:
            return []
        latest = max(item[0] for item in frames)
        cutoff = latest.timestamp() - keep_minutes * 60
        return [records for observed, records in sorted(frames) if observed.timestamp() >= cutoff]


quote_round_archive = QuoteRoundArchive()
