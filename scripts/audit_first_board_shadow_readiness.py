#!/usr/bin/env python3
"""Database-read-only readiness audit for the C3 forward shadow.

This command never invokes the scanner, never inserts historical events, and never
opens a paper account.  It writes only the requested JSON/Markdown report files.
Counts derived from the current overwrite-style spot table are diagnostics only;
they are not forward evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config.settings import settings  # noqa: E402
from app.core.stock_tagger import stock_tagger  # noqa: E402
from app.paper.strategy_iteration_shadow import (  # noqa: E402
    ROUTE_C3,
    _first_board_confirmation_frame,
    _first_board_eligibility,
    route_version_for,
)
from app.signal.anomaly_scanner import _is_causal_trade_driver_sector  # noqa: E402


DEFAULT_DB = BACKEND / "claw.db"
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "morning_review_20260903_1148"


def number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def parse_clock(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def query_rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def latest_quote_clock(row: dict[str, Any]) -> datetime | None:
    return (
        parse_clock(row.get("source_quote_at"))
        or parse_clock(row.get("received_at"))
        or parse_clock(row.get("updated_at"))
    )


def audit(connection: sqlite3.Connection, trade_day: date) -> dict[str, Any]:
    tag_rows = query_rows(
        connection,
        """
        SELECT code, name
        FROM stock_tags
        WHERE board_tag = 'tradeable'
          AND COALESCE(is_st, 0) = 0
          AND COALESCE(is_suspended, 0) = 0
          AND COALESCE(is_delisting, 0) = 0
        """,
    )
    allowed = {
        str(row["code"]): row
        for row in tag_rows
        if stock_tagger.is_tradeable(str(row["code"]))
    }
    spot_rows = query_rows(connection, "SELECT * FROM stock_spot")
    spot_by_code = {
        str(row["code"]): row
        for row in spot_rows
        if str(row["code"]) in allowed and latest_quote_clock(row) is not None
    }
    observed_at = max(
        (latest_quote_clock(row) for row in spot_by_code.values()),
        default=None,
    )
    valid_spots: dict[str, dict[str, Any]] = {}
    if observed_at is not None:
        tolerance = timedelta(seconds=max(int(settings.ANOMALY_QUOTE_ROUND_TOLERANCE_SEC), 1))
        max_age = max(int(settings.ANOMALY_QUOTE_MAX_AGE_SEC), 1)
        for code, row in spot_by_code.items():
            clock = latest_quote_clock(row)
            if (
                clock is not None
                and clock.date() == trade_day
                and clock <= observed_at + tolerance
                and (observed_at - clock).total_seconds() <= max_age
            ):
                valid_spots[code] = row

    mapping_rows = query_rows(
        connection,
        """
        SELECT code, sector_code, sector_name, sector_type, source,
               source_version, observed_at
        FROM stock_sector_mapping
        WHERE source = 'pywencai'
          AND sector_type IN ('concept', 'industry')
        """,
    )
    mappings_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mapping_rows:
        code = str(row.get("code") or "")
        mapping_clock = parse_clock(row.get("observed_at"))
        if (
            code in valid_spots
            and (observed_at is None or mapping_clock is None or mapping_clock <= observed_at)
        ):
            mappings_by_code[code].append(row)

    persistence_rows = query_rows(
        connection,
        """
        SELECT sector_code, sector_name, consecutive_days, limit_up_count,
               fund_flow, change_pct, strength_score
        FROM sector_persistence
        WHERE trade_date = ?
        """,
        (trade_day.isoformat(),),
    )
    persistence_by_sector = {
        str(row["sector_code"]): row for row in persistence_rows
    }
    contexts_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    persistence_context_codes: set[str] = set()
    causal_context_codes: set[str] = set()
    for code, mappings in mappings_by_code.items():
        for mapping in mappings:
            sector_code = str(mapping.get("sector_code") or "")
            persistence = persistence_by_sector.get(sector_code)
            if persistence is None:
                continue
            sector_name = str(
                persistence.get("sector_name") or mapping.get("sector_name") or ""
            ).strip()
            mapping_leaf = {
                "sector_code": sector_code,
                "sector_name": sector_name,
                "sector_type": str(mapping.get("sector_type") or ""),
                "source": str(mapping.get("source") or ""),
            }
            persistence_context_codes.add(code)
            if not _is_causal_trade_driver_sector(mapping_leaf):
                continue
            causal_context_codes.add(code)
            strength = number(persistence.get("strength_score"))
            sector_change = number(persistence.get("change_pct"))
            flow = number(persistence.get("fund_flow"))
            limit_ups = int(persistence.get("limit_up_count") or 0)
            if (
                strength is None
                or strength < settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_STRENGTH
                or sector_change is None
                or sector_change <= settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_CHANGE_PCT
                or flow is None
                or flow <= 0
                or limit_ups < settings.PAPER_FIRST_BOARD_SHADOW_STRUCTURAL_MIN_SECTOR_LIMIT_UP_COUNT
            ):
                continue
            contexts_by_code[code].append(
                {
                    **mapping_leaf,
                    "sector_strength": strength,
                    "sector_change_pct": sector_change,
                    "sector_fund_flow": flow,
                    "sector_limit_up_count": limit_ups,
                    "sector_consecutive_days": int(persistence.get("consecutive_days") or 0),
                }
            )

    selected_context: dict[str, dict[str, Any]] = {}
    for code, contexts in contexts_by_code.items():
        selected_context[code] = max(
            contexts,
            key=lambda item: (
                number(item.get("sector_strength"), 0.0) or 0.0,
                int(item.get("sector_limit_up_count") or 0),
                number(item.get("sector_fund_flow"), 0.0) or 0.0,
                number(item.get("sector_change_pct"), 0.0) or 0.0,
            ),
        )

    recent_dates = [
        str(row["trade_date"])
        for row in query_rows(
            connection,
            """
            SELECT DISTINCT trade_date
            FROM stock_kline
            WHERE trade_date < ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (
                trade_day.isoformat(),
                max(int(settings.PAPER_FIRST_BOARD_SHADOW_RECENT_LIMIT_LOOKBACK_SESSIONS), 1),
            ),
        )
    ]
    recent_limit_codes: set[str] = set()
    if recent_dates:
        marks = ",".join("?" for _ in recent_dates)
        recent_limit_codes = {
            str(row["code"])
            for row in query_rows(
                connection,
                f"""
                SELECT DISTINCT code
                FROM limit_up_pool
                WHERE trade_date IN ({marks})
                  AND COALESCE(quarantined, 0) = 0
                """,
                tuple(recent_dates),
            )
        }

    structural_codes = sorted(set(selected_context) - recent_limit_codes)
    eligible_codes: list[str] = []
    one_frame_confirming_codes: list[str] = []
    for code in structural_codes:
        quote = valid_spots[code]
        context = selected_context[code]
        eligible, _ = _first_board_eligibility(quote, context)
        confirming, _ = _first_board_confirmation_frame(quote, context)
        if eligible:
            eligible_codes.append(code)
        if confirming:
            one_frame_confirming_codes.append(code)

    event_rows = query_rows(
        connection,
        """
        SELECT event_type, status, COUNT(*) AS count
        FROM paper_shadow_event
        WHERE route_id = ? AND route_version = ?
        GROUP BY event_type, status
        ORDER BY event_type, status
        """,
        (ROUTE_C3, route_version_for(ROUTE_C3)),
    )
    activation_raw = str(settings.PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE or "")
    try:
        activation_day = date.fromisoformat(activation_raw)
    except ValueError:
        activation_day = None
    total_c3_events = int(
        connection.execute(
            "SELECT COUNT(*) FROM paper_shadow_event WHERE route_id = ? AND route_version = ?",
            (ROUTE_C3, route_version_for(ROUTE_C3)),
        ).fetchone()[0]
        or 0
    )
    preactivation_c3_events = (
        int(
            connection.execute(
                """
                SELECT COUNT(*) FROM paper_shadow_event
                WHERE route_id = ? AND route_version = ? AND trade_date < ?
                """,
                (ROUTE_C3, route_version_for(ROUTE_C3), activation_day.isoformat()),
            ).fetchone()[0]
            or 0
        )
        if activation_day is not None
        else None
    )
    latest_formal_kline = connection.execute(
        """
        SELECT MAX(trade_date)
        FROM stock_kline
        WHERE source IS NOT NULL AND source NOT IN ('', 'spot_fallback')
        """
    ).fetchone()[0]
    today_limit = connection.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN consecutive_days = 1 THEN 1 ELSE 0 END)
        FROM limit_up_pool
        WHERE trade_date = ? AND COALESCE(quarantined, 0) = 0
        """,
        (trade_day.isoformat(),),
    ).fetchone()

    quote_coverage = len(valid_spots) / max(len(allowed), 1)
    mapping_coverage = len(mappings_by_code) / max(len(valid_spots), 1)
    persistence_context_coverage = len(persistence_context_codes) / max(
        len(valid_spots),
        1,
    )
    sectors = {
        item["sector_code"]: item
        for item in selected_context.values()
    }
    activation_date = activation_raw
    return {
        "audit_kind": "read_only_readiness_not_forward_evidence",
        "trade_date": trade_day.isoformat(),
        "observed_at": observed_at.isoformat() if observed_at else None,
        "route_id": ROUTE_C3,
        "route_version": route_version_for(ROUTE_C3),
        "activation_date": activation_date,
        "live_only_no_historical_backfill": True,
        "execution": {
            "paper_account_created": False,
            "orders_generated_by_audit": False,
            "production_or_broker_connection": False,
        },
        "forward_session_requirements": {
            "minimum_healthy_frames": settings.PAPER_FIRST_BOARD_SHADOW_MIN_SESSION_FRAMES,
            "minimum_morning_frames": settings.PAPER_FIRST_BOARD_SHADOW_MIN_MORNING_FRAMES,
            "minimum_afternoon_frames": settings.PAPER_FIRST_BOARD_SHADOW_MIN_AFTERNOON_FRAMES,
            "first_frame_deadline": settings.PAPER_FIRST_BOARD_SHADOW_FIRST_FRAME_DEADLINE,
            "morning_last_not_before": settings.PAPER_FIRST_BOARD_SHADOW_MORNING_LAST_NOT_BEFORE,
            "afternoon_first_deadline": settings.PAPER_FIRST_BOARD_SHADOW_AFTERNOON_FIRST_DEADLINE,
            "last_frame_not_before": settings.PAPER_FIRST_BOARD_SHADOW_LAST_FRAME_NOT_BEFORE,
            "maximum_intrasegment_gap_sec": settings.PAPER_FIRST_BOARD_SHADOW_MAX_FRAME_GAP_SEC,
            "formal_close_kline_coverage": settings.PAPER_FIRST_BOARD_SHADOW_OUTCOME_MIN_KLINE_COVERAGE,
            "current_overwrite_snapshot_cannot_prove_full_session": True,
        },
        "coverage": {
            "tradeable_universe_count": len(allowed),
            "fresh_quote_count": len(valid_spots),
            "fresh_quote_coverage": round(quote_coverage, 6),
            "minimum_quote_coverage": settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE,
            "mapped_quote_count": len(mappings_by_code),
            "mapping_coverage": round(mapping_coverage, 6),
            "persistence_context_count": len(persistence_context_codes),
            "persistence_context_coverage": round(
                persistence_context_coverage,
                6,
            ),
            "causal_context_count": len(causal_context_codes),
            "recent_trade_sessions_available": recent_dates,
            "latest_formal_kline_date": latest_formal_kline,
        },
        "readiness_snapshot_only": {
            "active_structural_sector_count": len(sectors),
            "structural_denominator_count": len(structural_codes),
            "eligible_count": len(eligible_codes),
            "one_frame_confirming_count": len(one_frame_confirming_codes),
            "structural_sectors": sorted(
                sectors.values(),
                key=lambda item: (
                    -(number(item.get("sector_strength"), 0.0) or 0.0),
                    str(item.get("sector_code") or ""),
                ),
            ),
            "eligible_codes": eligible_codes,
            "one_frame_confirming_codes": one_frame_confirming_codes,
            "warning": "覆盖式实时表只能用于链路就绪审计；这些计数不会写入影子事件，也不能回填为前向样本。",
        },
        "same_day_market_context": {
            "limit_up_count_at_audit": int(today_limit[0] or 0),
            "first_board_count_at_audit": int(today_limit[1] or 0),
        },
        "existing_c3_event_counts": event_rows,
        "existing_c3_event_count": total_c3_events,
        "preactivation_c3_event_count": preactivation_c3_events,
        "gates": {
            "activation_config_valid": activation_day is not None,
            "activation_not_retroactive": (
                preactivation_c3_events == 0
                if preactivation_c3_events is not None
                else False
            ),
            "quote_coverage_ready": quote_coverage
            >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE,
            "mapping_coverage_ready": mapping_coverage
            >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE,
            "persistence_context_coverage_ready": persistence_context_coverage
            >= settings.PAPER_FIRST_BOARD_SHADOW_MIN_QUOTE_COVERAGE,
            "no_preactivation_c3_events": preactivation_c3_events == 0,
        },
    }


def markdown(payload: dict[str, Any]) -> str:
    coverage = payload["coverage"]
    snapshot = payload["readiness_snapshot_only"]
    market = payload["same_day_market_context"]
    gates = payload["gates"]
    session = payload["forward_session_requirements"]
    sectors = snapshot["structural_sectors"]
    lines = [
        "# C3 主线首板前向影子链路就绪审计",
        "",
        f"> 审计时点：{payload['observed_at'] or '--'}；数据日：{payload['trade_date']}。  ",
        "> 本报告从覆盖式实时表只读生成，仅验证链路和分母可构造性，**不是前向样本、不是历史回填、不是收益结论**。",
        "",
        "## 结论",
        "",
        f"- C3 版本：`{payload['route_version']}`，完整前向采集起始日：**{payload['activation_date']}**。",
        f"- 可交易池 {coverage['tradeable_universe_count']} 只；同一时点有效行情 {coverage['fresh_quote_count']} 只，覆盖率 **{coverage['fresh_quote_coverage']:.2%}**。",
        f"- 有同花顺行业/概念映射 {coverage['mapped_quote_count']} 只，覆盖率 **{coverage['mapping_coverage']:.2%}**。",
        f"- 可关联当日板块持续性截面 {coverage['persistence_context_count']} 只，覆盖率 **{coverage['persistence_context_coverage']:.2%}**；其中有可解释因果板块 {coverage['causal_context_count']} 只。",
        f"- 当前只读快照可构造结构分母 {snapshot['structural_denominator_count']} 只、资格候选 {snapshot['eligible_count']} 只、单帧满足确认形态 {snapshot['one_frame_confirming_count']} 只。",
        f"- 正式采证日还必须有至少 {session['minimum_healthy_frames']} 个健康全市场帧（上午≥{session['minimum_morning_frames']}、下午≥{session['minimum_afternoon_frames']}），上午首帧不晚于 {session['first_frame_deadline']} / 末帧不早于 {session['morning_last_not_before']}，下午首帧不晚于 {session['afternoon_first_deadline']} / 末帧不早于 {session['last_frame_not_before']}，各交易时段最大间断不超过 {session['maximum_intrasegment_gap_sec']} 秒；当前覆盖式快照不能替代这项证明。",
        f"- 当前涨停池 {market['limit_up_count_at_audit']} 只，其中首板 {market['first_board_count_at_audit']} 只；这些收盘前结果不参与候选选择。",
        f"- C3 当前事件行：{payload['existing_c3_event_count']}，其中激活日前事件：{payload['preactivation_c3_event_count'] if payload['preactivation_c3_event_count'] is not None else '--'}；审计脚本下单数：0；撮合账户：未创建。",
        "",
        "## 质量门禁",
        "",
        "| 门禁 | 结果 |",
        "|---|---|",
        f"| 激活日期配置合法 | {'通过' if gates['activation_config_valid'] else '未通过'} |",
        f"| 不追溯激活 | {'通过' if gates['activation_not_retroactive'] else '未通过'} |",
        f"| 行情覆盖 ≥ {coverage['minimum_quote_coverage']:.0%} | {'通过' if gates['quote_coverage_ready'] else '未通过'} |",
        f"| 板块映射覆盖 ≥ {coverage['minimum_quote_coverage']:.0%} | {'通过' if gates['mapping_coverage_ready'] else '未通过'} |",
        f"| 当日板块截面覆盖 ≥ {coverage['minimum_quote_coverage']:.0%} | {'通过' if gates['persistence_context_coverage_ready'] else '未通过'} |",
        f"| 激活前无 C3 事件 | {'通过' if gates['no_preactivation_c3_events'] else '未通过'} |",
        f"| 最近交易日历 | {', '.join(coverage['recent_trade_sessions_available']) or '--'} |",
        f"| 最新正式日 K | {coverage['latest_formal_kline_date'] or '--'} |",
        "",
        "## 当前结构板块（仅就绪诊断）",
        "",
        "| 板块 | 强度 | 涨幅 | 资金流 | 涨停数 |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in sectors:
        lines.append(
            f"| {item['sector_name']} | {item['sector_strength']:.1f} | "
            f"{item['sector_change_pct']:.2f}% | {item['sector_fund_flow']:.2f} | "
            f"{item['sector_limit_up_count']} |"
        )
    if not sectors:
        lines.append("| 无 | -- | -- | -- | -- |")
    lines.extend(
        [
            "",
            "## 因果与安全边界",
            "",
            "1. C3 生产扫描只接受服务器当天时钟；历史时间参数默认不生成 C3 事件，避免把后来更新的板块行配给旧行情。",
            "2. 从结构池、资格池、连续确认、未确认对照到收盘标签均为追加式事件；收盘是否涨停不参与对照组选择。",
            "3. 若健康全市场帧未覆盖完整交易时段，不生成未确认对照；正式收盘日 K 未覆盖结构分母 100% 时，不生成当日结果标签。",
            "4. C3 不在 Challenger 账户映射中，没有现金、持仓、NAV 或最大回撤；页面必须显示“不适用/待采证”，不能伪造 0%。",
            "5. 当前快照发生在原上午冻结点之后，因此只用于就绪审计。正式证据从激活日盘中逐帧产生，禁止将本报告计数插入证据表。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "首板C3链路就绪审计_20260903.json",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "首板C3链路就绪审计_20260903.md",
    )
    args = parser.parse_args()

    trade_day = date.fromisoformat(args.trade_date)
    with open_readonly(args.db) as connection:
        payload = audit(connection, trade_day)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output_md.write_text(markdown(payload), encoding="utf-8")
    print(json.dumps(
        {
            "output_json": str(args.output_json),
            "output_md": str(args.output_md),
            "gates": payload["gates"],
            "readiness_snapshot_only": {
                key: payload["readiness_snapshot_only"][key]
                for key in (
                    "active_structural_sector_count",
                    "structural_denominator_count",
                    "eligible_count",
                    "one_frame_confirming_count",
                )
            },
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
