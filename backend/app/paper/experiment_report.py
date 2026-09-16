"""只读持续实验统计：完整平仓轮次、净费用、入场时冻结标签，不倒推历史。"""
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
from types import SimpleNamespace

from sqlalchemy import select, func

from app.config.settings import settings
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.paper.experiment import EXPERIMENT_ACCOUNTS, experiment_status


def _object(value):
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _summary(cycles):
    pnls = [item["net_pnl"] for item in cycles]
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    n = len(pnls)
    wins = sum(value > 0 for value in pnls)
    # Wilson区间仅是样本比例描述，交易/交易日相关性仍须在报告中提示。
    if n:
        p, z = wins / n, 1.959963984540054
        center = (p + z*z/(2*n)) / (1+z*z/n)
        margin = z * ((p*(1-p)/n + z*z/(4*n*n)) ** 0.5) / (1+z*z/n)
        interval = [round(max(0, center-margin), 4), round(min(1, center+margin), 4)]
    else:
        interval = None
    return {
        "sample_count": n, "net_pnl": round(sum(pnls), 2),
        "win_rate": round(wins/n, 4) if n else None,
        "profit_factor": round(gross_profit/gross_loss, 4) if gross_loss else None,
        "win_rate_interval_95": interval,
        "entry_sessions": len({item["entry_date"] for item in cycles}),
    }


def complete_cycles(trades, evidence_by_trade, *, version, start_date, audit_cycles=None):
    """逐账户调用，不先按版本过滤流水；可选audit_cycles保留被排除清仓。

    新增元数据不改变既有eligible/net_pnl或三元统计返回合同。
    """
    state, completed = {}, []
    invalid = 0
    tainted_codes = set()
    def entry(trade_id):
        value = evidence_by_trade.get(str(trade_id), {})
        if not isinstance(value, dict):
            return {}
        return {**value, **{key: value.get(key) if isinstance(value.get(key), dict) else {}
                           for key in ("entry_sentiment", "entry_regime", "entry_bull_bear")}}
    for trade in sorted(trades, key=lambda item: (item.trade_time, item.id)):
        if trade.trade_type not in {"buy", "sell"}:
            continue
        try:
            raw_qty = float(trade.amount)
            quantity_valid = not isinstance(trade.amount, bool) and math.isfinite(raw_qty) and raw_qty > 0 and raw_qty.is_integer()
        except (TypeError, ValueError, OverflowError):
            quantity_valid = False
        if not quantity_valid or trade.code in tainted_codes:
            # Skipping a corrupt lot could invent a later clean liquidation.
            tainted_codes.add(trade.code)
            previous = state.pop(trade.code, {})
            invalid += 1
            if audit_cycles is not None:
                audit_cycles.append({"code": trade.code, "exit_trade_id": trade.id,
                    "exit_at": trade.trade_time.isoformat(), "eligible": False,
                    "trade_ids": [*previous.get("trade_ids", []), trade.id],
                    "exclusion_reasons": ["invalid_quantity_history"], "complete": False})
            continue
        qty = int(raw_qty)
        bucket = state.get(trade.code)
        if bucket is None:
            if trade.trade_type != "buy":
                invalid += 1
                if audit_cycles is not None:
                    audit_cycles.append({"code": trade.code, "exit_trade_id": trade.id,
                        "exit_at": trade.trade_time.isoformat(), "eligible": False,
                        "exclusion_reasons": ["missing_opening_buy"], "complete": False})
                continue
            evidence = entry(trade.id)
            sentiment = evidence.get("entry_sentiment") or {}
            bucket = {
                "code": trade.code, "quantity": 0, "net_pnl": 0.0,
                "first_buy_trade_id": trade.id, "entry_at": trade.trade_time.isoformat(),
                "trade_ids": [], "strategy_versions": [], "exclusion_reasons": [],
                "remaining_gross_cost": 0.0, "actual_fees": 0.0,
                "entry_date": trade.trade_time.date().isoformat(),
                "entry_regime": (evidence.get("entry_regime") or {}).get("label") or "unknown",
                "entry_bull_bear": (evidence.get("entry_bull_bear") or {}).get("label") or "unknown",
                "entry_sentiment": (sentiment.get("phase") or "unknown")
                    if sentiment.get("quality_status") == "ok" else "unknown",
                "eligible": bool(
                    trade.trade_time.date().isoformat() >= start_date
                    and trade.strategy_version == version
                    and evidence.get("active") is True
                    and evidence.get("strategy_version") == version
                ),
            }
            state[trade.code] = bucket
        bucket["eligible"] = bool(
            bucket["eligible"] and trade.strategy_version == version
            and not trade.forced_probe and not trade.excluded_from_performance
        )
        bucket["trade_ids"].append(trade.id)
        trade_version = str(trade.strategy_version or "")
        if trade_version not in bucket["strategy_versions"]:
            bucket["strategy_versions"].append(trade_version)
        reasons = bucket["exclusion_reasons"]
        for rejected, label in (
            (trade.strategy_version != version, "version_mismatch"),
            (bool(trade.forced_probe), "forced_probe"),
            (bool(trade.excluded_from_performance), "excluded_from_performance"),
            (bucket["entry_date"] < start_date, "before_protocol_start"),
        ):
            if rejected and label not in reasons:
                reasons.append(label)
        gross = float(trade.price or 0) * qty
        fees = float(trade.commission or 0) + float(trade.tax or 0)
        bucket["actual_fees"] += fees
        if trade.trade_type == "buy":
            bucket["remaining_gross_cost"] += gross
            buy_evidence = entry(trade.id)
            bucket["eligible"] = bool(bucket["eligible"]
                and buy_evidence.get("active") is True
                and buy_evidence.get("strategy_version") == version)
            if (buy_evidence.get("active") is not True or buy_evidence.get("strategy_version") != version) and "entry_evidence_missing_or_mismatched" not in reasons:
                reasons.append("entry_evidence_missing_or_mismatched")
            bucket["quantity"] += qty
            bucket["net_pnl"] -= gross + fees
        else:
            basis = bucket["remaining_gross_cost"] / bucket["quantity"] if bucket["quantity"] > 0 else None
            bucket["entry_basis_before_final_exit"] = basis
            bucket["remaining_gross_cost"] -= (basis or 0) * qty
            bucket["quantity"] -= qty
            bucket["net_pnl"] += gross - fees
            if bucket["quantity"] <= 0:
                bucket.update(exit_trade_id=trade.id, exit_at=trade.trade_time.isoformat(),
                    final_exit_price=float(trade.price or 0), final_exit_quantity=qty,
                    final_exit_fees=fees, final_exit_net_cash=gross-fees,
                    complete=bucket["quantity"] == 0)
                if bucket["quantity"] == 0:
                    bucket["net_pnl"] = round(bucket["net_pnl"], 2)
                    completed.append(bucket)
                else:
                    invalid += 1
                    bucket["exclusion_reasons"].append("oversold")
                if audit_cycles is not None:
                    audit_cycles.append({**bucket, "trade_ids": list(bucket["trade_ids"]),
                        "strategy_versions": list(bucket["strategy_versions"]),
                        "exclusion_reasons": list(bucket["exclusion_reasons"])})
                del state[trade.code]
    accepted = [item for item in completed if item["eligible"]]
    excluded = invalid + sum(not item["eligible"] for item in completed)
    pending = sum(item["eligible"] for item in state.values())
    return accepted, excluded, pending


def runtime_scan_evidence(logs, *, as_of):
    """Only explicit, clock-bounded runtime flags prove an enabled scan."""
    scans = [
        row for row in logs if row.action == "scan"
        and isinstance(row.created_at, datetime) and row.created_at <= as_of
        and _object(row.candidate_json).get("runtime_evidence_version") == "account_scan_v1"
    ]
    enabled = [row for row in scans if _object(row.candidate_json).get("buy_attempt_allowed") is True]
    return {
        "basis": "explicit_account_scan_v1" if scans else "not_recorded_for_this_version",
        "scan_started_count": len({row.run_id for row in scans}),
        "buy_attempt_allowed_scan_count": len({row.run_id for row in enabled}),
        "first_buy_attempt_allowed_scan_at": min((row.created_at for row in enabled), default=None).isoformat()
            if enabled else None,
        "last_scan_at": max((row.created_at for row in scans), default=None).isoformat() if scans else None,
        "strategy_data_and_risk_passed": None,
        "note": "只证明进入扫描及当轮允许运行买入尝试，不证明必要数据、策略、风控和撮合通过；无旧证据不倒填",
    }


async def build_experiment_report(db, *, account_name=None, now=None):
    from app.api.v1 import paper

    now = now or datetime.now()
    names = (account_name,) if account_name else EXPERIMENT_ACCOUNTS
    accounts = list((await db.scalars(select(PaperAccount).where(
        PaperAccount.account_name.in_(names), PaperAccount.status == "active",
    ).order_by(PaperAccount.id))).all())
    # 若有遗留同名账户，只有当前最新active实例计入；不创建/清空/合并账户。
    by_name = {item.account_name: item for item in accounts}
    output = []
    for name in names:
        account = by_name.get(name)
        version = paper._strategy_version(name)
        meta = paper._strategy_display_meta(account or SimpleNamespace(account_name=name))
        trades, evidence, logs = [], {}, []
        latest_day = None
        if account:
            trades = list((await db.scalars(select(PaperTradeLog).where(
                PaperTradeLog.account_id == account.id,
                PaperTradeLog.trade_time <= now,
            ).order_by(PaperTradeLog.trade_time, PaperTradeLog.id))).all())
            pairs = (await db.execute(select(TradeFill.broker_trade_id, TradeOrder.risk_json).join(
                TradeOrder, TradeOrder.order_id == TradeFill.order_id,
            ).where(
                TradeFill.broker == "paper", TradeOrder.broker == "paper",
                TradeOrder.account_id == name, TradeFill.side == "buy",
                TradeOrder.strategy_version == version, TradeFill.filled_at <= now,
            ))).all()
            evidence = {str(trade_id): _object(payload).get("experiment_entry", {})
                        for trade_id, payload in pairs if trade_id}
            latest_day = await db.scalar(select(func.max(PaperAutoTradeLog.trade_date)).where(
                PaperAutoTradeLog.account_id == account.id,
                PaperAutoTradeLog.strategy_version == version,
                PaperAutoTradeLog.action.notin_(("buy_signal", "signal_push")),
                PaperAutoTradeLog.created_at <= now,
            ))
            if latest_day:
                logs = list((await db.scalars(select(PaperAutoTradeLog).where(
                    PaperAutoTradeLog.account_id == account.id,
                    PaperAutoTradeLog.strategy_version == version,
                    PaperAutoTradeLog.trade_date == latest_day,
                    PaperAutoTradeLog.action.notin_(("buy_signal", "signal_push")),
                    PaperAutoTradeLog.created_at <= now,
                ))).all())
        cycles, excluded, pending = complete_cycles(
            trades, evidence, version=version, start_date=settings.PAPER_EXPERIMENT_START_DATE,
        )
        strata = {}
        for key in ("entry_regime", "entry_sentiment", "entry_bull_bear"):
            buckets = defaultdict(list)
            for cycle in cycles:
                buckets[cycle[key]].append(cycle)
            strata["by_" + key] = [{"label": label, **_summary(values)}
                                  for label, values in sorted(buckets.items())]
        counter = Counter((str(log.reason_code or "unknown"), str(log.reason or ""))
                          for log in logs if log.decision in {"blocked", "skipped", "wait", "dry_run"}
                          and log.action != "scan")
        summary = _summary(cycles)
        heartbeat_runs = {log.run_id for log in logs if log.action == "scan"}
        scan_runs = heartbeat_runs or {log.run_id for log in logs}
        output.append({
            "account_id": account.id if account else None,
            "account_name": name, "strategy_label": meta["label"], "strategy_version": version,
            "configured": True, "account_created": account is not None,
            "active": experiment_status(name, at=now)["active"],
            "auto_buy_enabled": bool(settings.PAPER_AUTO_TRADE_ENABLED
                                     and paper._strategy_auto_order_enabled(name, now=now)),
            "experiment": experiment_status(name, at=now),
            "closed_round_trips": len(cycles), "open_round_trips": pending,
            "excluded_round_trips": excluded, **summary, **strata,
            "latest_activity": {
                "trade_date": latest_day.isoformat() if latest_day else None,
                "scan_count": len(scan_runs),
                "scan_count_basis": "scan_heartbeats" if heartbeat_runs else "decision_runs",
                "decision_count": sum(log.action != "scan" for log in logs),
                "runtime": runtime_scan_evidence(logs, as_of=now),
                "fill_count": sum(trade.trade_time.date() == latest_day and trade.strategy_version == version
                                  for trade in trades),
                "blocked_count": sum(log.decision == "blocked" for log in logs),
                "top_reasons": [{"reason_code": code, "reason": reason, "count": count}
                                for (code, reason), count in counter.most_common(8)],
            },
        })
    return {
        "generated_at": now.isoformat(),
        "mode": "continuous_paper" if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED else "standard",
        "protocol_version": settings.PAPER_EXPERIMENT_VERSION,
        "start_date": settings.PAPER_EXPERIMENT_START_DATE,
        "activation_at": settings.PAPER_EXPERIMENT_ACTIVATION_AT,
        "first_session_scope": "afternoon_only",
        "scope": "current_protocol_closed_round_trips",
        "methodology": {
            "pnl": "从首次买入至持仓归零的实际模拟现金流，扣除全部买卖佣金及卖出税费",
            "sample_unit": "完整平仓轮次；未平仓、跨版本/强制/控制样本不计胜率",
            "regime": "首次入场时已知市场风格与情绪冻结；未知单独分组；不以事后收盘重贴标签",
            "bull_bear": "入场前上一完整交易日沪深双指数MA20/60及5日斜率趋势代理：牛态/熊态/震荡/未知；不是长期牛熊定论",
            "confidence": "Wilson95%区间仅供描述；交易日/股票相关且样本不足时不能证明策略优势",
            "first_session": "授权边界不等于运行时实际放行或完整午后扫描；以逐账户当前版本心跳证明实际运行区间，旧版缺证据不倒填，不回填上午信号或成交",
            "real_order_connected": False,
        },
        "accounts": output,
    }
