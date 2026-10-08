"""十二账户买点通知：追加式执行审计作为持久队列，不在交易事务中联网。

buy_signal 是策略入场条件确认，不是账户风控许可、加仓许可或成交。
signal_push 只追加投递状态；同账户/版本/交易日/信号去重，不回写交易。
机器人无幂等收据：发送成功但本地落账前崩溃，重试可能重复（至少一次）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import nullcontext
from datetime import datetime, timedelta, time
from typing import Any
from types import SimpleNamespace

from loguru import logger
from sqlalchemy import select, cast, String, literal, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.session import async_session
from app.models.paper import PaperAutoTradeLog, PaperAccount
from app.models.stock import StockTag
from app.paper.account_policy import ACCOUNT_NAMES
from app.push.channels.base import PushMessage
from app.push.channels.feishu import feishu_channel, FEISHU_CARD_BUDGET_BYTES
from app.push.scheduler import push_scheduler

SIGNAL = "buy_signal"
DELIVERY = "signal_push"
TERMINAL = {"sent", "expired"}
TEMPLATE_VERSION = "paper_buy_point_card_v6_execution_snapshot"
_dispatch_lock = asyncio.Lock()
_ingress_lock = asyncio.Lock()
_FAILED_INGRESS = "paper_buy_point_failed_ingress"
_runtime: dict[str, Any] = {"last_poll_at": None, "last_error": None}


def _payload(row: PaperAutoTradeLog) -> dict:
    try:
        value = json.loads(row.candidate_json or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _text(value: Any, limit: int = 800) -> str:
    # 数据文本不能形成飞书@所有人、链接或卡片控制标签。
    return str(value or "").replace("<", "＜").replace(">", "＞").replace(
        "[", "［"
    ).replace("]", "］")[:limit]


def _metric(value: Any, label: str, suffix: str = "") -> str:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return ""
    return f"{label}{number:.2f}{suffix}" if math.isfinite(number) else ""


def _clock(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo is None else None
    except (ValueError, TypeError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _freeze_market_context(context: dict, *, price: float, observed_at: datetime,
                           quote_round_id: str) -> dict | None:
    """只复制本次个股报价叶子；不可拿发送时行情/轮次总时间替代个股时间。"""
    if not isinstance(context, dict):
        return None
    source_at = _clock(context.get("source_quote_at"))
    context_price = _number(context.get("price"))
    if (source_at is None or context.get("quote_round_id") != quote_round_id
            or context_price is None or abs(context_price - price) > 0.000001
            or not 0 <= (observed_at - source_at).total_seconds() <= settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC):
        return None
    frozen = {"source_quote_at": source_at.isoformat(), "quote_round_id": quote_round_id,
              "schema": "signal_market_context_v1", "amount_unit": "CNY",
              "circ_market_cap_unit": "100M_CNY"}
    signed = {"change_pct", "orderbook_imbalance"}
    nonnegative = {"volume_ratio", "turnover_rate", "amount", "bid_ask_spread",
                   "confirmation_sample_count", "confirmation_persistence_sec"}
    for key in ("price", "prev_close", "open", "high", "low", "change_pct", "avg_price",
                "volume_ratio", "turnover_rate", "amount", "circ_market_cap", "limit_up",
                "limit_down", "orderbook_imbalance", "bid_ask_spread", "stop_loss_price",
                "confirmation_sample_count", "confirmation_persistence_sec"):
        value = _number(context.get(key))
        if value is not None and (key in signed or value > 0 or (key in nonnegative and value == 0)):
            frozen[key] = value
    return frozen


def _market_blocks(context: dict) -> list[str]:
    if not context:
        return []
    metrics = []
    for key, label, suffix in (
        ("change_pct", "涨跌幅", "%"), ("avg_price", "分时均价(VWAP) ¥", ""),
        ("volume_ratio", "量比 ", "倍"), ("turnover_rate", "换手率 ", "%"),
    ):
        value = _number(context.get(key))
        if value is not None:
            metrics.append(f"{label}**{value:.2f}{suffix}**")
    amount = _number(context.get("amount"))
    if amount is not None:
        metrics.append(f"成交额 **{amount / 100000000:.2f}亿元**")
    high, low = _number(context.get("high")), _number(context.get("low"))
    if high is not None and low is not None and high >= low > 0:
        metrics.append(f"当日区间 **¥{low:.2f}–{high:.2f}**")
    blocks = ["**📈 原确认时行情**\n" + "\n".join(
        " ｜ ".join(metrics[i:i + 2]) for i in range(0, len(metrics), 2)
    )] if metrics else []
    levels = []
    stop = _number(context.get("stop_loss_price"))
    if stop is not None and stop > 0:
        levels.append(f"策略风控参考价 **¥{stop:.2f}**（不是保证成交的止损价）")
    ceiling, price = _number(context.get("limit_up")), _number(context.get("price"))
    if ceiling is not None and price is not None and ceiling >= price > 0:
        levels.append(f"涨停价 **¥{ceiling:.2f}**，较信号价空间 **{(ceiling / price - 1) * 100:.2f}%**（不是盈利目标）")
    if levels:
        blocks.append("**📍 关键价位**\n" + "\n".join("• " + x for x in levels))
    return blocks


def candidate_reason(candidate: dict, fallback: str) -> str:
    """仅格式化交易链路已经验证的证据，不另造选股规则/概率。"""
    source = str(candidate.get("_source") or "")
    parts = []
    if source.startswith("promotion_") or source in {"tenbagger_midline", "reversal_pullback"}:
        parts.extend([candidate.get("strategy_label"), candidate.get("entry_condition")])
    else:
        parts.append(fallback)
    parts.extend([
        _metric(candidate.get("confirmation_sample_count"), "连续确认帧数"),
        _metric(candidate.get("confirmation_persistence_sec"), "持续", "秒"),
        _metric(candidate.get("change_pct"), "当时涨幅", "%"),
        _metric(candidate.get("volume_ratio"), "当时量比"),
        _metric(candidate.get("avg_price"), "VWAP"),
        _metric(candidate.get("pullback_from_high_pct"), "高点回撤", "%"),
    ])
    return "；".join(str(part) for part in parts if part)


def event_reason(hypothesis: str, event: Any, spot: Any) -> str:
    parts = [
        hypothesis,
        f"不可变confirmed事件{event.observed_at:%H:%M:%S}，执行轮次再次校验通过",
        "当前站上成交均价线、量比/盘口及本路线价格区间达标",
        _metric(getattr(spot, "price", None), "现价"),
        _metric(getattr(spot, "avg_price", None), "VWAP"),
        _metric(getattr(spot, "volume_ratio", None), "量比"),
        _metric(getattr(spot, "orderbook_imbalance", None), "盘口失衡"),
    ]
    return "；".join(part for part in parts if part)


def _hold_ingress(db: AsyncSession, run_id: str, snapshot: dict, issue: str) -> None:
    """仅事务间handoff，既不刷外层待写对象也不冒充已持久化。"""
    db.info.setdefault(_FAILED_INGRESS, {})[run_id] = {
        "args": snapshot, "error_type": issue,
        "notification_allowed": bool(settings.PAPER_BUY_POINT_PUSH_ENABLED and settings.PUSH_ENABLED),
    }
    _runtime["last_ingress_failure"] = {
        "run_id": run_id, "event_key": snapshot["signal_key"], "account_name": snapshot["account_name"],
        "code": snapshot["code"], "error_type": issue, "durable": False,
    }
    log = logger.warning if issue == "OuterWritesDeferred" else logger.error
    log("策略买点入口待独立持久化 account={} code={} event_key={} run_id={} issue={}",
        snapshot["account_name"], snapshot["code"], snapshot["signal_key"], run_id, issue)


async def record_buy_point(
    db: AsyncSession, *, account: Any, strategy_version: str, label: str,
    code: str, name: str, source: str, signal_key: str, reason: str,
    price: float, observed_at: datetime, decision_run_id: str,
    quote_round_id: str, as_of_at: datetime | None,
    code_version: str = "", queue_order: bool = False,
    market_context: dict | None = None,
    _use_savepoint: bool = True,
) -> PaperAutoTradeLog | None:
    """由完整策略过滤之后、资金/持仓过滤之前调用。只写本轮新证据。

    主循环按当日同类入场形态去重；子路线使用不可变event_key。
    调用者持有账户/行情轮次锁；独立savepoint保护通知附加写入。只表示已经
    确认的技术信号，即使后续委托失败也不撤销该信号，更不将其视为成交。
    """
    from app.paper.experiment import experiment_active

    capture_research = experiment_active(account.account_name, at=observed_at)
    if not settings.PAPER_BUY_POINT_PUSH_ENABLED and not capture_research:
        return None
    wall_now = datetime.now()
    if (
        account.account_name not in ACCOUNT_NAMES or account.status != "active"
        or not quote_round_id or not strategy_version or not reason or not signal_key
        or observed_at.date() != wall_now.date()
        or not 0 <= (wall_now - observed_at).total_seconds() <= settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC
        or not isinstance(as_of_at, datetime)
        or not 0 <= (observed_at - as_of_at).total_seconds() <= settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC
        or not math.isfinite(price) or price <= 0
    ):
        return None
    frozen_market = {}
    if market_context is not None:
        frozen_market = _freeze_market_context(market_context, price=price,
                                               observed_at=observed_at, quote_round_id=quote_round_id)
        if frozen_market is None:
            # 附加通知失败不能阻断交易，但也不能向用户展示串轮/陈旧的个股买点。
            return None
    identity = "|".join((str(account.id), strategy_version, observed_at.date().isoformat(), code, source, signal_key))
    run_id = "bp-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    # 只持有已验证的标量快照，不保留失效/rollback后的ORM引用；不是持久队列。
    snapshot = dict(
        account_id=account.id, account_name=account.account_name,
        strategy_version=strategy_version, label=label, code=code, name=name,
        source=source, signal_key=signal_key, reason=reason, price=price,
        observed_at=observed_at, decision_run_id=decision_run_id,
        quote_round_id=quote_round_id, as_of_at=as_of_at, code_version=code_version,
        queue_order=queue_order, market_context=dict(frozen_market) if market_context is not None else None,
    )
    # begin_nested无条件preflush，no_autoflush也不能阻止。通知不能替交易
    # 刷新待写账务，更不能因preflush错误使外层事务失效；交给事务结束hook。
    if _use_savepoint and (db.new or db.dirty or db.deleted):
        _hold_ingress(db, run_id, snapshot, "OuterWritesDeferred")
        return None
    try:
        async with (db.begin_nested() if _use_savepoint else nullcontext()):
            tag = await db.scalar(select(StockTag).where(StockTag.code == code).limit(1))
            if (tag is None or tag.board_tag != "tradeable" or tag.is_st
                    or tag.is_suspended or tag.is_delisting or tag.is_ipo_recent):
                return None
            previous = await db.scalar(select(PaperAutoTradeLog.id).where(
                PaperAutoTradeLog.run_id == run_id, PaperAutoTradeLog.action == SIGNAL,
            ).limit(1))
            if previous is not None:
                return None
            signal_labels = {}
            if capture_research:
                from app.paper.signal_research import freeze_signal_labels
                signal_labels = await freeze_signal_labels(
                    db, account.account_name, strategy_version, at=observed_at,
                )
            payload = {
                "research_capture_schema": "all_confirmed_v1" if capture_research else "notification_only",
                "notification_allowed": bool(settings.PAPER_BUY_POINT_PUSH_ENABLED and settings.PUSH_ENABLED),
                "signal_labels": signal_labels,
                "notification_schema": "paper_buy_point_v2",
                "signal_observed_at": observed_at.isoformat(),
                "market_context": frozen_market,
                "account_name": account.account_name, "strategy_label": _text(label, 120),
                "account_role": "次账户" if account.account_name.startswith("challenger_") else "主账户",
                "signal_key": signal_key, "decision_run_id": decision_run_id,
                "queue_order": bool(queue_order), "real_order_connected": False,
                "execution_status": "策略入场信号确认；账户资金/持仓/风控及加仓条件另行审核，非下单或成交",
            }
            row = PaperAutoTradeLog(
                account_id=account.id, run_id=run_id, trade_date=observed_at.date(),
                created_at=wall_now, trigger="strategy_scan", source=source[:30],
                code=code, name=name, action=SIGNAL, decision="confirmed",
                reason=_text(reason), price=price, strategy_version=strategy_version,
                quote_round_id=quote_round_id, as_of_at=as_of_at,
                stage_code="strategy_buy_point", reason_code="strategy_entry_confirmed",
                code_version=code_version or None,
                candidate_json=json.dumps(payload, ensure_ascii=False),
            )
            db.add(row)
            await db.flush()
        return row
    except Exception as exc:
        # 通知附加写入失败不改买卖决策；不输出含凭据/候选全文的异常。
        _hold_ingress(db, run_id, snapshot, type(exc).__name__)
        return None


async def retry_failed_buy_points(db: AsyncSession, *, session_factory=None) -> dict:
    """原事务commit/rollback释放写锁后调用；仅补本次失败入口，不扫描历史confirmed。

    session.info只是短暂移交，进程崩溃或持续DB不可写仍会丢失。独立提交现有
    outbox后才算recovered；失败保留原快照供调用者重试，不联网、不改交易。
    """
    result = {"recovered": 0, "expired": 0, "rejected": 0, "failed": 0}
    pending = db.info.get(_FAILED_INGRESS, {})
    if not pending:
        return result
    if db.in_transaction():
        return {**result, "status": "transaction_active", "failed": len(pending)}
    async with _ingress_lock:
        for run_id, failure in list(pending.items()):
            args = failure["args"]
            try:
                async with (session_factory or async_session)() as retry_db:
                    previous = await retry_db.scalar(select(PaperAutoTradeLog).where(
                        PaperAutoTradeLog.run_id == run_id,
                        PaperAutoTradeLog.action.in_((SIGNAL, "signal_ingress")),
                    ).order_by(PaperAutoTradeLog.id.desc()).limit(1))
                    if previous is not None:
                        status = "recovered" if previous.action == SIGNAL else previous.decision
                    else:
                        now = datetime.now()
                        item = {"trade_date": args["observed_at"].date(),
                                "created_at": args["observed_at"], "as_of_at": args["as_of_at"],
                                "payload": {"market_context": args["market_context"] or {}}}
                        status = "expired" if not _delivery_fresh(item, now) else "rejected"
                        account = await retry_db.get(PaperAccount, args["account_id"])
                        if status != "expired" and account is not None and account.account_name == args["account_name"]:
                            replay = {k: v for k, v in args.items() if k not in {"account_id", "account_name"}}
                            row = await record_buy_point(retry_db, account=account, **replay,
                                                         _use_savepoint=False)
                            if retry_db.info.get(_FAILED_INGRESS):
                                raise RuntimeError("buy_point_ingress_retry_failed")
                            if row is not None:
                                payload = _payload(row)
                                # The failed attempt did not durably freeze research
                                # labels. Never certify labels recomputed later as PIT.
                                payload["signal_labels"] = {
                                    "schema": "signal_labels_v1", "status": "unknown",
                                    "observed_at": args["observed_at"].isoformat(),
                                    "strategy_version": args["strategy_version"],
                                    "regime": "unknown", "bull_bear": "unknown", "sentiment": "unknown",
                                    "reason": "ingress_recovered_original_labels_unavailable",
                                }
                                payload["ingress_recovery"] = {
                                    "schema": "paper_buy_point_ingress_recovery_v1",
                                    "original_signal_observed_at": args["observed_at"].isoformat(),
                                    "recovered_at": now.isoformat(),
                                    "decision_run_id": args["decision_run_id"],
                                    "original_error_type": failure["error_type"],
                                }
                                # 不能把原来关闭通知时捕获的研究证据补发。
                                payload["notification_allowed"] = bool(
                                    failure["notification_allowed"] and payload["notification_allowed"])
                                row.candidate_json = json.dumps(payload, ensure_ascii=False)
                                status = "recovered"
                        retry_db.add(PaperAutoTradeLog(
                            account_id=args["account_id"], run_id=run_id,
                            trade_date=args["observed_at"].date(), created_at=now,
                            trigger="push_worker", source=args["source"][:30], code=args["code"],
                            name=args["name"], action="signal_ingress", decision=status,
                            stage_code="notification_ingress", reason_code=f"buy_point_ingress_{status}",
                            reason="通知入口独立补偿；过期证据仅审计，不补推",
                            strategy_version=args["strategy_version"], quote_round_id=args["quote_round_id"],
                            as_of_at=args["as_of_at"],
                            candidate_json=json.dumps({
                                "event_key": args["signal_key"], "account_name": args["account_name"],
                                "decision_run_id": args["decision_run_id"],
                                "signal_observed_at": args["observed_at"].isoformat(),
                                "original_error_type": failure["error_type"],
                            }, ensure_ascii=False),
                        ))
                    await retry_db.commit()
                pending.pop(run_id, None)
                result[status] += 1
            except Exception as exc:
                result["failed"] += 1
                logger.error("策略买点独立补偿未持久化 event_key={} run_id={} error={}",
                             args["signal_key"], run_id, type(exc).__name__)
        _runtime["last_ingress_retry"] = {**result, "at": datetime.now().isoformat(),
                                         "volatile_pending": len(pending)}
    return result


async def _audit(db: AsyncSession, item: dict, status: str, now: datetime, *, batch_id: str = "",
                 delivery_timing: dict | None = None) -> None:
    research = item["payload"].get("research_only") is True
    db.add(PaperAutoTradeLog(
        account_id=item["account_id"], run_id=item["run_id"], trade_date=item["trade_date"],
        created_at=now, trigger="push_worker", source=C3_ROUTE if research else "feishu", code=item["code"],
        name=item["name"], action="research_push" if research else DELIVERY, decision=status,
        reason=f"飞书研究观察投递：{status}" if research else f"飞书买点投递：{status}",
        strategy_version=item["strategy_version"], quote_round_id=item["quote_round_id"],
        stage_code="c3_research_delivery" if research else "notification_delivery",
        reason_code=f"c3_research_{status}" if research else f"buy_point_push_{status}",
        candidate_json=json.dumps({**(item["payload"] if research else {}),
                                   "signal_log_id": item["id"], "batch_id": batch_id,
                                   **({"execution_snapshot": {
                                       "state": item.get("execution_state", "unconfirmed"),
                                       "checked_at": item["execution_checked_at"],
                                       "decision_log_id": item.get("execution_log_id"),
                                       "market_revalidated": False,
                                   }} if item.get("execution_checked_at") else {}),
                                   **(delivery_timing or {})}, ensure_ascii=False),
    ))


def _item(row: PaperAutoTradeLog) -> dict:
    return {key: getattr(row, key) for key in (
        "id", "account_id", "run_id", "trade_date", "created_at", "as_of_at",
        "code", "name", "reason", "price", "source", "strategy_version", "quote_round_id",
    )} | {"payload": _payload(row)}


def _buy_point_reason_blocks(reason: str) -> list[str]:
    """只把原始确认依据分区展示；不生成交易预案或推算缺失指标。"""
    evidence, metrics = [], []
    labels = ("连续确认帧数", "当时涨幅", "当时量比", "高点回撤", "盘口失衡",
              "VWAP", "现价", "量比", "持续")
    for part in str(reason or "")[:800].replace("\n", "；").split("；"):
        part = part.strip()
        if not part:
            continue
        label = next((label for label in labels if part.startswith(label)), None)
        value = part[len(label):].strip() if label else ""
        # 仅识别本模块生成的数值前缀；像“VWAP上方”这样的依据仍按原文展示。
        if value and value[0] in "+-0123456789":
            label = {"VWAP": "分时均价(VWAP)", "盘口失衡": "买卖盘强弱指标"}.get(label, label)
            metrics.append(f"{label} **{_text(value, 100)}**")
        else:
            evidence.append(_text(part.replace("不可变confirmed事件", "信号确认于"), 800))
    blocks = ["**📋 原确认依据**\n" + "\n".join(
        f"• {part}" for part in evidence
    )] if evidence else []
    if metrics:
        blocks.append("**📊 确认时指标**\n" + "\n".join(
            " ｜ ".join(metrics[i:i + 2]) for i in range(0, len(metrics), 2)
        ))
    return blocks or ["**📋 原确认依据**\n• 未提供详细说明，请核对信号审计。"]


def build_message(items: list[dict]) -> PushMessage:
    if not items:
        raise ValueError("买点提醒不可使用空信号列表")
    badges = dict(zip(ACCOUNT_NAMES, ("A", "B", "C", "D", "E", "F",
                                     "A2", "B2", "C2", "D2", "E2", "F2")))
    state_labels = {
        "partial": "⏳ 已有部分模拟成交记录",
        "filled": "✅ 已有模拟成交记录",
        "pending": "⏳ 已提交模拟委托 · 未确认成交",
        "waiting": "⏸ 本轮未下单 · 等待条件",
        "blocked": "⛔ 本轮执行已拦截 · 历史成交以账本为准",
        "expired": "⌛ 原买点已失效 · 本轮未下单",
        "canceled": "⏹ 模拟委托已撤销余量 · 成交情况以账本为准",
    }
    sections, audits, accounts = [], [], []
    for index, item in enumerate(items, 1):
        payload = item["payload"]
        shared = payload.get("portfolio_notification_schema") == "portfolio_buy_point_ingress_v1"
        badge = "组合实验" if shared else badges.get(payload.get("account_name"), "未知账户")
        if badge not in accounts:
            accounts.append(badge)
        role = "独立账本" if shared else _text(payload.get("account_role") or "账户类型待核对", 30)
        strategy_label = (
            f"来源策略：{badges.get(payload.get('origin_account'), '未知账户')}；不合并原账户资金或成交"
            if shared else _text(payload.get("strategy_label"), 120)
        )
        state = state_labels.get(item.get("execution_state"), "🔎 原条件曾确认 · 执行未核实 · 非下单或成交")
        execution_note = _text(item.get("execution_note"), 250)
        reason = item["reason"]
        if shared:
            # Presentation only: keep raw payload, identities and receipt text in audits.
            execution_note = execution_note.replace("共享组合", "本组合").replace("共享模拟", "模拟").replace("共享账本", "本账本").replace("共享分配", "分配结果")
            reason = str(reason or "").replace("原策略已提交共享组合确认", "原策略确认已入组合队列")
        context = payload.get("market_context") or {}
        own_quote_time = _clock(context.get("source_quote_at"))
        quote_time = own_quote_time or item["as_of_at"]
        quote_label = "个股行情时点" if own_quote_time else "行情轮次时点"
        signal_time = _clock(payload.get("signal_observed_at")) or item["created_at"]
        parts = [
            f"**{index}. {_text(item['name'], 30)}（{_text(item['code'], 10)}）｜{badge} {role}**",
            strategy_label,
            f"**{state}**" + (f"\n{execution_note}" if execution_note else ""),
            f"**{'原委托参考价' if shared else '信号参考价'} ¥{item['price']:.2f}**（非成交价、非当前报价）\n"
            f"确认：{signal_time:%Y-%m-%d %H:%M:%S}（北京时间）\n"
            f"{quote_label}：{quote_time:%H:%M:%S}",
            *_buy_point_reason_blocks(reason),
            *_market_blocks(context),
            ("**用途**：仅报告独立组合实验，不是原账户的新增买入指令。"
             if shared else "**下单前核对**：本卡报告原条件与执行记录，不是当前买入或加仓指令；当前买入条件未重新认证，不得据此追买。"),
        ]
        if item.get("execution_checked_at"):
            parts.insert(3, f"执行记录核对截至：{_text(item['execution_checked_at'])}（北京时间；非行情复验）")
        if payload.get("queue_order"):
            parts.append("**⚠️ 回封排队提示**\n排队不等于成交；须由后续真实盘口撮合确认。")
        sections.append("\n\n".join(parts))
        # 保留后台身份/执行追溯，不再把技术编号和实验说明铺在卡片里。
        audits.append({"signal_id": item["id"], "run_id": item["run_id"],
                       "account": payload.get("account_name"), "strategy_version": item["strategy_version"],
                       "execution_state": item.get("execution_state", "unconfirmed"),
                       "execution_note": item.get("execution_note") or payload.get("execution_status"),
                       "execution_checked_at": item.get("execution_checked_at"),
                       "execution_log_id": item.get("execution_log_id"),
                       "market_revalidated": False})
    if len(items) == 1:
        prefix = "组合实验通知" if accounts == ["组合实验"] else {
            "filled": "模拟成交回执", "partial": "模拟部分成交",
            "pending": "模拟委托·待成交", "waiting": "执行等待",
            "blocked": "执行已拦截", "expired": "原信号已失效",
            "canceled": "模拟委托·余量已撤",
        }.get(items[0].get("execution_state"), "条件确认·执行未核实")
        display_title = (f"{prefix}｜{accounts[0]} · {_text(items[0]['name'], 30)}"
                         f"（{_text(items[0]['code'], 10)}）")
    else:
        prefix = "策略与组合通知" if "组合实验" in accounts else "策略信号与执行状态"
        display_title = f"{prefix}｜{' / '.join(accounts)} · {len(items)}条"
    return PushMessage(
        # 保留内部标题和信号身份，避免改版改变现有限频去重语义。
        title=f"Claw 策略买点确认 · {len(items)}条 · " + "/".join(str(item["id"]) for item in items),
        content="\n\n---\n\n".join(sections),
        category="paper_buy_point", msg_type="signal", priority=8,
        # 汇总卡无单一股票；逐账户/版本/股票冷却在持久日志层执行。
        extra={"signal_ids": [item["id"] for item in items],
               "display_title": display_title, "template_version": (
                    "paper_buy_point_card_v5_portfolio_scope" if "组合实验" in accounts else TEMPLATE_VERSION),
               "feishu_sections": sections, "signal_audits": audits},
    )


def _fit_batch(items: list[dict]) -> list[dict]:
    """按实际UTF-8预算装箱，过大头部不阻塞后续小卡；未选项仍留队。"""
    selected = []
    for item in items:
        if len(selected) >= 6:
            break
        if any(x["account_id"] == item["account_id"] and x["code"] == item["code"]
               and x["strategy_version"] == item["strategy_version"] for x in selected):
            continue
        candidate = selected + [item]
        card = feishu_channel._build_card(build_message(candidate))
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= FEISHU_CARD_BUDGET_BYTES:
            selected = candidate
    return selected


async def reconcile_portfolio_buy_points(*, now: datetime, session_factory, limit: int = 32) -> dict:
    """Committed immutable source -> existing outbox; never own a trading session.

    No volatile retry spool: a failed commit leaves the original signal eligible.
    Recent work and old audit cleanup each have a bounded quota. Single-process
    ingress lock, not a claim of cross-process exactly-once delivery.
    """
    from app.models.paper import PaperPortfolioSignal
    from app.paper.account_policy import ROUTE_ACCOUNT_NAMES
    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT, entry_version, make_signal_key
    result = {"recovered": 0, "expired": 0, "rejected": 0, "disabled": 0, "failed": 0}
    if not settings.PAPER_PORTFOLIO_ENABLED:
        return {**result, "status": "disabled"}
    limit = max(1, min(128, limit))
    async with _ingress_lock:
        try:
            async with session_factory() as db:
                wallets = (await db.scalars(select(PaperAccount).where(
                    PaperAccount.account_name == PORTFOLIO_ACCOUNT).limit(2))).all()
                if len(wallets) > 1:
                    _runtime["portfolio_ingress"] = {**result, "status": "wallet_ambiguous"}
                    return {**result, "status": "wallet_ambiguous"}
                wallet = wallets[0] if wallets else None
                if wallet is None or wallet.status != "active":
                    return {**result, "status": "wallet_unavailable"}
                wallet_id = wallet.id
                done = select(PaperAutoTradeLog.id).where(
                    PaperAutoTradeLog.run_id == literal("psb-") + cast(PaperPortfolioSignal.id, String),
                    PaperAutoTradeLog.action.in_((SIGNAL, "signal_ingress")),
                ).exists()
                cutoff = now - timedelta(seconds=settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC)
                ids = []
                # Future/malformed source clocks get audited, never refreshed.
                for recent in (True, False):
                    query = select(PaperPortfolioSignal.id).where(~done,
                        PaperPortfolioSignal.observed_at >= cutoff if recent else
                        PaperPortfolioSignal.observed_at < cutoff)
                    ids.extend((await db.scalars(query.order_by(
                        PaperPortfolioSignal.observed_at, PaperPortfolioSignal.id).limit(limit))).all())
            for signal_id in ids:
                await asyncio.sleep(0)
                try:
                    async with session_factory() as db:
                        signal = await db.get(PaperPortfolioSignal, signal_id)
                        run_id = f"psb-{signal_id}"
                        if await db.scalar(select(PaperAutoTradeLog.id).where(
                                PaperAutoTradeLog.run_id == run_id,
                                PaperAutoTradeLog.action.in_((SIGNAL, "signal_ingress"))).limit(1)):
                            continue
                        status, cause, price, version, expires = "rejected", "invalid_contract", None, None, None
                        try:
                            policy = json.loads(signal.entry_policy_json)
                            envelope = policy.get("notification")
                            if not isinstance(envelope, dict) or envelope.get("schema") != "portfolio_buy_point_ingress_v1":
                                cause = "legacy_or_missing_notification_schema"
                            elif (type(envelope.get("notification_allowed")) is not bool
                                  or envelope.get("price_basis") != "original_order_reference"):
                                cause = "invalid_notification_envelope"
                            elif envelope["notification_allowed"] is False:
                                status, cause = "disabled", "disabled_at_capture"
                            else:
                                price = _number(policy.get("price"))
                                ttl = _number(policy.get("max_execution_delay_sec"))
                                portfolio_ttl = _number(settings.PAPER_PORTFOLIO_SIGNAL_TTL_SECONDS)
                                clocks = [_clock(str(x)) for x in
                                          (signal.confirmed_at, signal.observed_at, signal.as_of_at)]
                                confirmed, observed, asof = clocks
                                valid = (signal.origin_account in ACCOUNT_NAMES and signal.origin_version
                                    and signal.portfolio_version and signal.source and signal.source_signal_id
                                    and signal.decision_round_id and price is not None and price > 0
                                    and ttl is not None and ttl > 0
                                    and portfolio_ttl is not None and 0 < portfolio_ttl <= 120 and all(clocks)
                                    and confirmed.date() == observed.date() == asof.date()
                                    and confirmed <= observed and asof <= observed)
                                route = next((r for r, account in ROUTE_ACCOUNT_NAMES.items()
                                              if account == signal.origin_account), None)
                                valid = valid and ((route == signal.source and bool(signal.shadow_event_key))
                                                   if route else not signal.shadow_event_key)
                                if valid and not signal.shadow_event_key:
                                    valid = asof <= confirmed
                                if valid:
                                    expected = make_signal_key(source=signal.source,
                                        origin_account=signal.origin_account, origin_version=signal.origin_version,
                                        decision_round_id=signal.decision_round_id,
                                        source_signal_id=signal.source_signal_id,
                                        shadow_event_key=signal.shadow_event_key,
                                        origin_account_id=signal.origin_account_id,
                                        portfolio_version=signal.portfolio_version)
                                    valid = expected == signal.signal_key
                                if valid:
                                    version = entry_version(signal.origin_version, policy_version=signal.portfolio_version)
                                    expires = min(confirmed + timedelta(seconds=min(ttl,
                                                  portfolio_ttl,
                                                  settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC)),
                                                  observed + timedelta(seconds=settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC),
                                                  asof + timedelta(seconds=settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC))
                                    if any(x > now for x in clocks):
                                        cause = "future_clock"
                                    elif observed.date() != now.date() or now > expires:
                                        status, cause = "expired", "original_signal_ttl"
                                    else:
                                        origin_account = await db.get(PaperAccount, signal.origin_account_id)
                                        tag = await db.scalar(select(StockTag).where(StockTag.code == signal.code).limit(1))
                                        if (origin_account is not None and origin_account.account_name == signal.origin_account
                                                and origin_account.status == "active" and tag is not None and tag.board_tag == "tradeable" and not any(
                                                (tag.is_st, tag.is_suspended, tag.is_delisting, tag.is_ipo_recent))):
                                            status, cause = "recovered", "committed_portfolio_signal"
                                        else:
                                            cause = "stock_tag_not_tradeable"
                        except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                            cause = "invalid_contract"
                        payload = {
                            "notification_schema": "paper_buy_point_v2",
                            "portfolio_notification_schema": "portfolio_buy_point_ingress_v1",
                            "portfolio_signal_id": signal.id, "signal_key": signal.signal_key,
                            "account_name": PORTFOLIO_ACCOUNT, "account_role": "共享5万元组合",
                            "origin_account": signal.origin_account, "origin_version": signal.origin_version,
                            "portfolio_version": signal.portfolio_version,
                            "strategy_label": f"共享组合 · 来源 {signal.origin_account}",
                            "signal_observed_at": str(signal.confirmed_at),
                            "source_observed_at": str(signal.observed_at),
                            "notification_expires_at": expires.isoformat() if expires else None,
                            "price_basis": "original_order_reference", "market_context": {},
                            "notification_allowed": status == "recovered",
                            "real_order_connected": False, "cause": cause,
                        }
                        db.add(PaperAutoTradeLog(
                            account_id=wallet_id, run_id=run_id, trade_date=signal.observed_at.date(),
                            created_at=now, trigger="push_worker", source=signal.source[:30],
                            code=signal.code, name=signal.name, strategy_version=version or signal.origin_version,
                            quote_round_id=signal.decision_round_id, as_of_at=signal.as_of_at,
                            action=SIGNAL if status == "recovered" else "signal_ingress",
                            decision="confirmed" if status == "recovered" else status,
                            price=price, stage_code="strategy_buy_point" if status == "recovered" else "notification_ingress",
                            reason_code="portfolio_notification_" + status,
                            reason="原策略已提交共享组合确认；资金分配与下一轮模拟撮合另行审核，非成交",
                            candidate_json=json.dumps(payload, ensure_ascii=False)))
                        await db.commit()
                    result[status] += 1
                except Exception as exc:
                    result["failed"] += 1
                    logger.error("共享买点入口未持久化 signal_id={} error={}", signal_id, type(exc).__name__)
        except Exception as exc:
            result["failed"] += 1
            logger.error("共享买点对账失败 error={}", type(exc).__name__)
    _runtime["portfolio_ingress"] = {**result, "at": now.isoformat(), "limit_per_age_group": limit}
    return result


async def _portfolio_execution(db, item: dict, now: datetime) -> None:
    """Read exact shared identity only. Never use the source wallet's fills."""
    from app.models.paper import PaperPortfolioSignal, PaperPortfolioDecision, PaperTradeLog
    from app.models.trading import TradeOrder, TradeFill
    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT, entry_version
    from app.paper.portfolio_wallet import order_key
    item.pop("execution_state", None)
    item["execution_note"] = "共享组合执行状态待核对；不代表成交"
    payload = item["payload"]
    try:
        wallets = (await db.scalars(select(PaperAccount.id).where(
            PaperAccount.account_name == PORTFOLIO_ACCOUNT).limit(2))).all()
        if wallets != [item["account_id"]]:
            return
        signal = await db.get(PaperPortfolioSignal, payload.get("portfolio_signal_id"))
        if (signal is None or signal.signal_key != payload.get("signal_key")
                or signal.origin_version != payload.get("origin_version")
                or signal.portfolio_version != payload.get("portfolio_version")
                or signal.code != item["code"] or signal.observed_at > now):
            return
        version = entry_version(signal.origin_version, policy_version=signal.portfolio_version)
        if version != item["strategy_version"]:
            return
        order = await db.scalar(select(TradeOrder).where(
            TradeOrder.idempotency_key == order_key(signal.signal_key),
            TradeOrder.account_id == PORTFOLIO_ACCOUNT, TradeOrder.broker == "paper",
            TradeOrder.created_at <= now))
        if order is None:
            decision = await db.scalar(select(PaperPortfolioDecision).where(
                PaperPortfolioDecision.signal_id == signal.id,
                PaperPortfolioDecision.account_id == item["account_id"],
                PaperPortfolioDecision.portfolio_version == signal.portfolio_version,
                PaperPortfolioDecision.observed_at <= now,
            ).order_by(PaperPortfolioDecision.id.desc()).limit(1))
            if decision is not None and not decision.order_id:
                state = {"source_wait": "waiting", "confirmation_wait": "waiting",
                         "budget_skipped": "blocked", "rejected": "blocked",
                         "expired": "expired", "superseded": "expired"}.get(decision.decision)
                if state:
                    item["execution_state"] = state
                    item["execution_note"] = "共享分配：" + str(decision.reason_code)
            return
        frozen = json.loads(order.risk_json or "{}").get("paper_portfolio_origin") or {}
        if (frozen.get("schema") != "paper_portfolio_origin_v1"
                or order.side != "buy" or order.code != signal.code or order.source != signal.source
                or order.signal_id != signal.source_signal_id or order.strategy_version != version
                or frozen.get("portfolio_signal_key") != signal.signal_key
                or any(frozen.get(k) != getattr(signal, k) for k in (
                    "origin_account", "origin_account_id", "origin_version", "portfolio_version"))
                or frozen.get("entry_version") != version):
            return
        if order.status in ("pending", "submitted") and order.filled_quantity == 0:
            item["execution_state"], item["execution_note"] = "pending", "共享模拟委托已提交，等待下一轮撮合"
        elif order.status in ("filled", "partial"):
            if not order.decision_round_id:
                return
            fills = (await db.execute(select(TradeFill.quantity, PaperTradeLog.amount, PaperTradeLog.id).join(
                PaperTradeLog, cast(PaperTradeLog.id, String) == TradeFill.broker_trade_id).where(
                TradeFill.order_id == order.order_id, TradeFill.broker == "paper",
                TradeFill.code == signal.code, TradeFill.side == "buy", TradeFill.quantity > 0,
                TradeFill.filled_at <= now, PaperTradeLog.account_id == item["account_id"],
                PaperTradeLog.code == signal.code, PaperTradeLog.trade_type == "buy",
                PaperTradeLog.strategy_version == version, PaperTradeLog.trade_time <= now,
                # The order retains source_signal_id; each broker slice has its
                # own request signal_id. Receipt ID plus round/time bind the ledger.
                TradeFill.decision_round_id == order.decision_round_id,
                PaperTradeLog.decision_round_id == TradeFill.decision_round_id,
                TradeFill.fill_round_id.is_not(None), TradeFill.fill_round_id != "",
                TradeFill.fill_round_id != order.decision_round_id,
                PaperTradeLog.fill_round_id == TradeFill.fill_round_id,
                PaperTradeLog.trade_time == TradeFill.filled_at,
                TradeFill.filled_at >= order.created_at,
                TradeFill.trade_date == order.trade_date,
                func.date(PaperTradeLog.trade_time) == order.trade_date,
                PaperTradeLog.price == TradeFill.price, TradeFill.price > 0,
            ))).all()
            receipt_count = await db.scalar(select(func.count(TradeFill.id)).where(
                TradeFill.order_id == order.order_id))
            quantity = sum(q for q, amount, _ in fills if q == amount)
            if (fills and receipt_count == len(fills)
                    and len({identity for _, _, identity in fills}) == len(fills)
                    and all(q == a for q, a, _ in fills) and quantity == order.filled_quantity
                    and 0 < quantity <= order.quantity
                    and (order.status != "filled" or quantity == order.quantity)):
                item["execution_state"] = "filled" if order.status == "filled" else "partial"
                item["execution_note"] = f"共享账本已绑定模拟成交 {quantity} 股；非来源账户成交"
    except (ValueError, TypeError, AttributeError, KeyError):
        return


async def dispatch_buy_points(*, now: datetime | None = None, session_factory=None) -> dict:
    """独立消费者；关闭DB读事务后才联网，发送前落attempting租约，失败可恢复。"""
    if _dispatch_lock.locked():
        return {"status": "busy"}
    async with _dispatch_lock:
        return await _dispatch(now=now or datetime.now(), session_factory=session_factory or async_session)


def _portfolio_delivery_disabled(item: dict) -> bool:
    """The lifecycle switch also stops already-queued portfolio notifications."""
    payload = item["payload"]
    return bool(not settings.PAPER_PORTFOLIO_ENABLED and (
        payload.get("portfolio_notification_schema") == "portfolio_buy_point_ingress_v1"
        or payload.get("account_name") == "shared_50k"))


def _delivery_fresh(item: dict, at: datetime) -> bool:
    """发送前按每股原始时钟再验；等待DB期间不能把旧确认刷新为新信号。"""
    if item["payload"].get("portfolio_notification_schema") == "portfolio_buy_point_ingress_v1":
        expires = _clock(item["payload"].get("notification_expires_at"))
        observed = _clock(item["payload"].get("source_observed_at"))
        if expires is None or observed is None or not observed <= at <= expires:
            return False
    context = item["payload"].get("market_context") or {}
    source_at = _clock(context.get("source_quote_at")) if context else item["as_of_at"]
    observed_at = _clock(item["payload"].get("signal_observed_at")) or item["created_at"]
    clocks = (item["created_at"], observed_at, item["as_of_at"], source_at)
    return bool(item["trade_date"] == at.date() and all(
        stamp is not None and stamp.tzinfo is None
        and 0 <= (at - stamp).total_seconds() <= settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC
        for stamp in clocks))


def _execution_log_order_link(row: PaperAutoTradeLog) -> tuple[str, str | None]:
    """Distinguish legacy missing links from malformed or conflicting evidence."""
    try:
        payload = json.loads(row.candidate_json or "{}")
    except (ValueError, TypeError):
        return "invalid", None
    if not isinstance(payload, dict):
        return "invalid", None
    observation = payload.get("deferred_order_observation")
    if observation is not None and not isinstance(observation, dict):
        return "invalid", None
    response = observation.get("order_response") if isinstance(observation, dict) else None
    if response is not None and not isinstance(response, dict):
        return "invalid", None
    values = [payload.get("pending_order_id"), payload.get("queue_order_id")]
    if isinstance(response, dict):
        values.append(response.get("order_id"))
    present = [value for value in values if value is not None]
    if any(not isinstance(value, str) or not value.strip() or value != value.strip()
           for value in present):
        return "invalid", None
    identities = set(present)
    if len(identities) > 1:
        return "conflicting", None
    return ("linked", next(iter(identities))) if identities else ("missing", None)


async def _buy_point_execution_log(db, row, *, decision_run_id, now):
    """Follow the original decision's exact order across later audit run IDs.

    Never use the latest same-stock decision: accounts, versions, sources, dates
    and order identities remain isolated. A bounded/incomplete read is unknown,
    not evidence that the original pending state is still current.
    """
    if not isinstance(decision_run_id, str) or not decision_run_id:
        return None
    scope = (
        PaperAutoTradeLog.account_id == row.account_id,
        PaperAutoTradeLog.code == row.code,
        PaperAutoTradeLog.source == row.source,
        PaperAutoTradeLog.strategy_version == row.strategy_version,
        PaperAutoTradeLog.trade_date == row.trade_date,
        PaperAutoTradeLog.created_at <= now,
        PaperAutoTradeLog.action.in_((
            "buy", "deferred_buy", "queue_buy", "skip_buy", "wait_buy", "skip_terminal")),
    )
    original = await db.scalar(select(PaperAutoTradeLog).where(
        *scope, PaperAutoTradeLog.run_id == decision_run_id,
    ).order_by(PaperAutoTradeLog.id.desc()).limit(1))
    if original is None:
        return None
    link_status, order_id = _execution_log_order_link(original)
    if link_status != "linked":
        # Presentation rejects invalid/conflicting evidence; only truly missing
        # legacy links may use the separately verified broker/ledger identity.
        return original
    # The producer already stores order_response.order_id for deferred outcomes
    # and queue_order_id for highboard outcomes. No order/ledger writes here.
    later = list((await db.scalars(select(PaperAutoTradeLog).where(
        *scope, PaperAutoTradeLog.id > original.id,
        PaperAutoTradeLog.created_at >= original.created_at,
    ).order_by(PaperAutoTradeLog.created_at.desc(), PaperAutoTradeLog.id.desc())
      .limit(129))).all())
    for receipt in later[:128]:
        link_status, receipt_order_id = _execution_log_order_link(receipt)
        if link_status in {"invalid", "conflicting"}:
            # Same-scope corrupt evidence cannot certify a different order or
            # justify falling back to an older, possibly stale pending receipt.
            return receipt
        if receipt_order_id == order_id:
            return receipt
    return None if len(later) > 128 else original


async def _verified_buy_fill_state(db, item: dict, decision, *, now) -> str | None:
    """A nonempty audit ID is not a fill: verify owned, visible broker+ledger facts.

    This is a bounded read-side check only. Database errors propagate to the
    existing delivery retry path; no fake success or repair writes are produced.
    """
    from app.models.paper import PaperTradeLog
    from app.models.trading import TradeOrder, TradeFill

    link_status, linked_order_id = _execution_log_order_link(decision)
    if link_status in {"invalid", "conflicting"}:
        return None
    query = select(TradeOrder).join(
        TradeFill, TradeFill.order_id == TradeOrder.order_id).join(
        PaperTradeLog, cast(PaperTradeLog.id, String) == TradeFill.broker_trade_id).where(
        PaperTradeLog.id == decision.executed_trade_id,
        PaperTradeLog.account_id == item["account_id"],
        PaperTradeLog.code == item["code"], PaperTradeLog.trade_type == "buy",
        PaperTradeLog.strategy_version == item["strategy_version"],
        TradeOrder.broker == "paper", TradeOrder.side == "buy",
        TradeOrder.account_id == item["payload"].get("account_name"),
        TradeOrder.code == item["code"], TradeOrder.source == decision.source,
        TradeOrder.strategy_version == item["strategy_version"],
        TradeOrder.trade_date == item["trade_date"], TradeOrder.created_at <= now,
        TradeOrder.decision_at <= now, TradeOrder.decision_round_id == item["quote_round_id"],
    )
    if linked_order_id:
        query = query.where(TradeOrder.order_id == linked_order_id)
    orders = list((await db.scalars(query.limit(2))).all())
    if len(orders) != 1:
        return None
    order = orders[0]
    if (order.status not in {"filled", "partial"} or not order.decision_round_id
            or not 0 < int(order.filled_quantity or 0) <= int(order.quantity or 0)):
        return None
    receipts = (await db.execute(select(
        TradeFill.quantity, PaperTradeLog.amount, PaperTradeLog.id, TradeFill.price,
    ).join(PaperTradeLog, cast(PaperTradeLog.id, String) == TradeFill.broker_trade_id).where(
        TradeFill.order_id == order.order_id, TradeFill.broker == "paper",
        TradeFill.side == "buy", TradeFill.code == item["code"],
        TradeFill.quantity > 0, TradeFill.price > 0,
        TradeFill.filled_at <= now, TradeFill.filled_at >= order.decision_at,
        TradeFill.trade_date == item["trade_date"],
        TradeFill.decision_round_id == order.decision_round_id,
        TradeFill.fill_round_id.is_not(None), TradeFill.fill_round_id != "",
        PaperTradeLog.account_id == item["account_id"],
        PaperTradeLog.code == item["code"], PaperTradeLog.trade_type == "buy",
        PaperTradeLog.strategy_version == item["strategy_version"],
        PaperTradeLog.trade_time == TradeFill.filled_at,
        func.date(PaperTradeLog.trade_time) == item["trade_date"],
        PaperTradeLog.price == TradeFill.price,
        PaperTradeLog.decision_round_id == TradeFill.decision_round_id,
        PaperTradeLog.fill_round_id == TradeFill.fill_round_id,
    ).limit(129))).all()
    count = await db.scalar(select(func.count(TradeFill.id)).where(TradeFill.order_id == order.order_id))
    if (not receipts or len(receipts) > 128 or count != len(receipts)
            or len({row.id for row in receipts}) != len(receipts)
            or decision.executed_trade_id not in {row.id for row in receipts}
            or any(not math.isfinite(row.price) or row.price <= 0
                   or row.quantity != row.amount or row.quantity % 100 for row in receipts)
            or sum(row.quantity for row in receipts) != order.filled_quantity):
        return None
    if order.status == "filled" and order.filled_quantity == order.quantity:
        return "filled"
    if order.status == "partial" and order.filled_quantity < order.quantity:
        return "partial"
    return None


def _apply_execution_log(item: dict, decision: PaperAutoTradeLog) -> None:
    """Presentation only: order rejection, signal expiry and delivery differ."""
    link_status, _ = _execution_log_order_link(decision)
    if link_status in {"invalid", "conflicting"}:
        item["execution_state"] = "unconfirmed"
        item["execution_note"] = "订单关联证据无效或冲突，尚不能核实执行状态；"
        return
    payload = _payload(decision)
    observation = payload.get("deferred_order_observation")
    response = observation.get("order_response") if isinstance(observation, dict) else None
    status = response.get("status") if isinstance(response, dict) else None
    status = status or payload.get("submission_order_status")
    # A cancellation can follow partial fills; never claim the whole order was
    # unfilled merely because this receipt cancels the remainder.
    if status == "canceled" or decision.reason_code == "deferred_canceled":
        item["execution_state"] = "canceled"
    elif status in {"risk_blocked", "rejected"}:
        item["execution_state"] = "blocked"
    elif decision.action == "buy" and decision.decision == "executed" and decision.executed_trade_id:
        item["execution_state"] = "partial" if status == "partial" else "filled"
    elif status in {"filled", "partial"}:  # A status alone is not an execution receipt.
        item["execution_state"] = "unconfirmed"
    elif decision.action in {"deferred_buy", "queue_buy"} and decision.decision == "wait":
        item["execution_state"] = "pending"
    else:
        item["execution_state"] = {
            # Legacy signal-terminal logs retain their original invalidation label;
            # explicit submission outcomes above must never inherit that label.
            "wait_buy": "waiting", "skip_buy": "blocked", "skip_terminal": "expired",
        }.get(decision.action, "unconfirmed")
    prefix = {
        "filled": "已有模拟成交记录；", "partial": "已有部分模拟成交记录；",
        "pending": "已提交模拟委托，等待撮合，未确认成交；",
        "canceled": "模拟委托余量已撤销；不否认此前部分成交；",
    }.get(item["execution_state"], "尚未确认成交；")
    item["execution_note"] = prefix + str(decision.reason or decision.decision)


async def _refresh_execution_snapshot(db, item: dict, *, now: datetime) -> None:
    """Read the same decision/order only; no market certification or order writes."""
    # A fresh read that is incomplete must not retain an earlier optimistic state.
    item["execution_state"] = "unconfirmed"
    item["execution_note"] = "尚未核实本信号的账户执行结果；缺少回执不等于未扫描或未下单。"
    item["execution_checked_at"] = now.isoformat()
    item["execution_log_id"] = None
    scope = SimpleNamespace(**{key: item[key] for key in (
        "account_id", "code", "source", "strategy_version", "trade_date",
    )})
    decision = await _buy_point_execution_log(
        db, scope, decision_run_id=item["payload"].get("decision_run_id"), now=now)
    if decision is None:
        return
    item["execution_log_id"] = decision.id
    _apply_execution_log(item, decision)
    if item.get("execution_state") in {"filled", "partial"}:
        verified = await _verified_buy_fill_state(db, item, decision, now=now)
        item["execution_state"] = verified or "unconfirmed"
        prefix = {"filled": "已有模拟成交记录；", "partial": "已有部分模拟成交记录；"}.get(
            verified, "模拟成交回执尚不可核实；")
        item["execution_note"] = prefix + str(decision.reason or "")


async def _dispatch(*, now: datetime, session_factory) -> dict:
    _runtime["last_poll_at"] = now.isoformat()
    _runtime["last_error"] = None
    try:
        # One bounded reconciliation slice must not starve the original queue.
        await asyncio.wait_for(reconcile_portfolio_buy_points(now=now, session_factory=session_factory), timeout=2.0)
    except asyncio.TimeoutError:
        _runtime["portfolio_ingress"] = {"status": "timeout", "at": now.isoformat(),
                                          "durable_source_retryable": True}
    if not settings.PAPER_BUY_POINT_PUSH_ENABLED:
        return {"status": "disabled"}
    async with session_factory() as db:
        # 跨日未完成通知只补记过期，绝不补发昨日买点。
        start = datetime.combine(now.date(), time.min) - timedelta(days=1)
        rows = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.created_at >= start,
            PaperAutoTradeLog.created_at <= now,
            PaperAutoTradeLog.stage_code.in_(("strategy_buy_point", "notification_delivery")),
        ).order_by(PaperAutoTradeLog.id))).all())
        last = {row.run_id: row for row in rows if row.action == DELIVERY}
        sent = [row for row in rows if row.action == DELIVERY and row.decision == "sent"]
        batches = {_payload(row).get("batch_id") or row.run_id for row in sent
                   if (now - row.created_at).total_seconds() < 3600}
        items = []
        for row in rows:
            if row.action != SIGNAL or row.decision != "confirmed":
                continue
            status = last.get(row.run_id)
            if status is not None and (status.decision in TERMINAL or (
                    status.decision == "disabled" and _payload(status).get("cause") == "portfolio_disabled")):
                continue
            item = _item(row)
            if _portfolio_delivery_disabled(item):
                await _audit(db, item, "disabled", now, delivery_timing={
                    "cause": "portfolio_disabled",
                    "previous_status": status.decision if status else "pending",
                    "event_key": item["payload"].get("signal_key"),
                })
                continue
            # 关闭通知期间仍保留研究分母；之后重新打开也不补发这些记录。
            if item["payload"].get("notification_allowed") is False:
                continue
            if not _delivery_fresh(item, now):
                await _audit(db, item, "expired", now, delivery_timing={
                    "cause": "signal_ttl_expired", "previous_status": status.decision if status else "pending",
                    "previous_cause": _payload(status).get("cause") if status else None,
                    "event_key": item["payload"].get("signal_key"),
                })
                continue
            if status is not None:
                retry = max(30, settings.PAPER_BUY_POINT_PUSH_RETRY_SEC) if status.decision == "attempting" else settings.PAPER_BUY_POINT_PUSH_RETRY_SEC
                if (now - status.created_at).total_seconds() < retry:
                    continue
            cooldown = any(
                delivery.account_id == row.account_id and delivery.code == row.code
                and delivery.strategy_version == row.strategy_version
                and (now - delivery.created_at).total_seconds() < settings.PUSH_STOCK_COOLDOWN
                for delivery in sent
            )
            if cooldown or len(batches) >= settings.PUSH_HOURLY_LIMIT:
                await _audit(db, item, "throttled", now, delivery_timing={
                    "cause": "stock_cooldown" if cooldown else "hourly_batch_limit",
                    "hourly_batches": len(batches), "hourly_limit": settings.PUSH_HOURLY_LIMIT,
                    "event_key": item["payload"].get("signal_key"),
                })
                continue
            if item["payload"].get("portfolio_notification_schema") == "portfolio_buy_point_ingress_v1":
                await _portfolio_execution(db, item, now)
                items.append(item)
                continue
            await _refresh_execution_snapshot(db, item, now=now)
            # 让字节预算选择器看到后续短卡，避免前6条长卡占位浪费批次。
            items.append(item)
        items = _fit_batch(items)
        # 额度受压时只借用一个原poll周期合并碎片；不能牺牲即将过期的源行情。
        merge_window = max(0, settings.PAPER_BUY_POINT_PUSH_INTERVAL_SEC)
        if (items and len(items) < 6 and len(batches) >= max(1, settings.PUSH_HOURLY_LIMIT // 2)
                and merge_window > 0
                and all(0 <= (now - (_clock(x["payload"].get("signal_observed_at"))
                                     or x["created_at"])).total_seconds() < merge_window
                        and _delivery_fresh(x, now + timedelta(seconds=merge_window))
                        for x in items)):
            await db.commit()
            return {"status": "coalescing", "count": 0, "pending_count": len(items),
                    "max_merge_wait_sec": merge_window}
        batch_id = hashlib.sha256("|".join(x["run_id"] for x in items).encode()).hexdigest()[:24]
        for item in items:
            await _audit(db, item, "attempting", now, batch_id=batch_id)
        await db.commit()
    # 租约提交/关闭连接可能因锁争用等待。每次等待后重验，并逐股过期，
    # 不因一只过期而丢弃同批仍有效信号；终态写入后再次检查剩余项。
    while items:
        # The attempting commit can wait while a consumer cancels/rejects/fills
        # the exact order. Never send the execution snapshot read before that wait.
        # Reopen a read-only session, then close it before card construction/HTTP.
        async with session_factory() as db:
            checked_at = datetime.now()
            for item in items:
                if (_portfolio_delivery_disabled(item) or not _delivery_fresh(item, checked_at)
                        or item["payload"].get("portfolio_notification_schema") == "portfolio_buy_point_ingress_v1"):
                    continue
                await _refresh_execution_snapshot(db, item, now=checked_at)
        # Remove expired/disabled entries before byte packing, or a stale long
        # card can displace a fresh one until its retry lease has also expired.
        send_started_at = datetime.now()
        disabled = [item for item in items if _portfolio_delivery_disabled(item)]
        disabled_ids = {item["id"] for item in disabled}
        expired = [item for item in items if item["id"] not in disabled_ids
                   and not _delivery_fresh(item, send_started_at)]
        if not expired and not disabled:
            fitted = _fit_batch(items)
            if not fitted:
                items = []
                break
            message = build_message(fitted)
            # Packing/building can itself outlast the original source TTL.
            # Retain the full candidate set until this check passes; on a wait
            # caused by expiry auditing, re-read every remaining execution.
            send_started_at = datetime.now()
            disabled = [item for item in items if _portfolio_delivery_disabled(item)]
            disabled_ids = {item["id"] for item in disabled}
            expired = [item for item in items if item["id"] not in disabled_ids
                       and not _delivery_fresh(item, send_started_at)]
            if not expired and not disabled:
                items = fitted
                break
        async with session_factory() as db:
            for item in disabled:
                await _audit(db, item, "disabled", send_started_at, batch_id=batch_id,
                             delivery_timing={"cause": "portfolio_disabled", "previous_status": "attempting",
                                              "event_key": item["payload"].get("signal_key")})
            for item in expired:
                await _audit(db, item, "expired", send_started_at, batch_id=batch_id,
                             delivery_timing={"cause": "expired_before_transport", "previous_status": "attempting",
                                              "event_key": item["payload"].get("signal_key")})
            await db.commit()
        removed_ids = disabled_ids | {item["id"] for item in expired}
        items = [item for item in items if item["id"] not in removed_ids]
    if not items:
        return {"status": "idle", "count": 0}
    try:
        result = await push_scheduler.push_to_channels(message, [feishu_channel], use_throttle=True)
        status = "sent" if result.get("channels", {}).get("feishu") is True else str(result.get("status") or "failed")
        if status not in {"sent", "failed", "disabled", "skipped", "throttled"}:
            status = "failed"
    except Exception as exc:
        status = "failed"
        _runtime["last_error"] = type(exc).__name__
        logger.error("飞书买点投递异常：{}", type(exc).__name__)
    send_completed_at = datetime.now()
    delivery_timing = {
        "delivery_clock_schema": "paper_push_transport_v1",
        "dispatch_started_at": now.isoformat(),
        "send_started_at": send_started_at.isoformat(),
        "send_completed_at": send_completed_at.isoformat(),
        "transport_clock_status": ("ok" if now <= send_started_at <= send_completed_at else "invalid"),
        # HTTP成功仅为渠道返回，不证明用户收到/阅读或按信号价成交。
        "user_received_at": None,
    }
    async with session_factory() as db:
        for item in items:
            await _audit(db, item, status, send_completed_at, batch_id=batch_id,
                         delivery_timing=delivery_timing)
        await db.commit()
    return {"status": status, "count": len(items)}



C3_ROUTE = "c3_mainline_first_board"
_c3_lock = asyncio.Lock()
def _c3_session(at: datetime) -> bool:
    from app.core.trade_calendar import is_official_closed_day, TRADE_SESSIONS
    return (at.tzinfo is None and at.weekday() < 5 and not is_official_closed_day(at.date())
            and any(start <= at.time() <= end for start, end in (
                TRADE_SESSIONS["morning"], TRADE_SESSIONS["afternoon"])))


def _c3_original_check(event, now: datetime) -> tuple[str | None, dict]:
    from app.paper.strategy_iteration_shadow import route_version_for
    try:
        snapshot = json.loads(event.snapshot_json)
        quote = snapshot["quote"]
        rules = snapshot["rule_snapshot"]
        source_at = _clock(quote.get("source_quote_at"))
        version = route_version_for(C3_ROUTE)
        expected = f"{C3_ROUTE}:{version}:{event.trade_date.isoformat()}:{event.code}:confirmed"
        if (event.route_id != C3_ROUTE or event.route_version != version
                or event.event_type != "confirmed" or event.status != "confirmed"
                or event.event_key != expected or snapshot.get("route_version") != version
                or snapshot.get("route_id") != C3_ROUTE
                or snapshot.get("point_in_time_only") is not True
                or snapshot.get("analysis_trade_date") != event.trade_date.isoformat()
                or _clock(snapshot.get("as_of_at")) != event.observed_at
                or rules.get("evidence_only_without_execution_account") is not True
                or any(rules.get(k) is not False for k in (
                    "real_order_connected", "champion_order_connected", "challenger_paper_account_connected"))):
            return "invalid_research_contract", {}
        clocks = (event.observed_at, source_at, event.created_at)
        if any(t is None or t.tzinfo is not None for t in clocks):
            return "missing_original_clock", {}
        if any(t > now for t in clocks) or source_at > event.observed_at:
            return "future_clock", {}
        if (event.trade_date != now.date() or any(t.date() != now.date() for t in clocks)
                or any((now - t).total_seconds() > settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC for t in clocks)):
            return "original_signal_expired", {}
        if not _c3_session(now) or not _c3_session(event.observed_at) or not _c3_session(source_at):
            return "outside_trading_session", {}
        price = _number(event.price)
        if price is None or price <= 0 or _number(quote.get("price")) != price or quote.get("code") != event.code:
            return "invalid_original_quote", {}
        return None, {"source_quote_at": source_at.isoformat(), "price": price}
    except (ValueError, TypeError, KeyError, AttributeError):
        return "missing_research_evidence", {}


async def _c3_current_check(db, code: str, now: datetime, *, pool: dict | None = None) -> tuple[str | None, dict]:
    from app.paper.strategy_iteration_shadow import build_first_board_current_pool, route_version_for
    from app.core.stock_tagger import stock_tagger
    tag = await db.scalar(select(StockTag).where(StockTag.code == code).limit(1))
    if tag is None:
        return "temporary_data_wait", {"data_wait_reason": "stock_identity_missing"}
    # 002 uses the project's existing "sme" taxonomy, which is tradeable.
    # Reuse canonical classification rather than inventing a second board list;
    # the original notification prefix scope and all identity/risk flags remain.
    if (tag.board_tag != "tradeable" or not stock_tagger.is_tradeable(code)
            or tag.board_type != stock_tagger.get_board_type(code)
            or not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))
            or any(getattr(tag, k) is not False for k in ("is_st", "is_suspended", "is_delisting", "is_ipo_recent"))):
        return "stock_identity_not_tradeable", {}
    if pool is None:
        pool = await build_first_board_current_pool(db, now=now)
    asof = _clock(pool.get("as_of"))
    member = next((x for x in pool.get("members", []) if x.get("code") == code), {})
    latest = pool.get("latest_quote_round") or {}
    latest_at = _clock(latest.get("as_of"))
    expires_at = _clock(pool.get("expires_at"))
    if (pool.get("valid") is not True or pool.get("history_complete") is not True
            or pool.get("route_version") != route_version_for(C3_ROUTE)
            or asof is None or asof.date() != now.date()
            or not 0 <= (now - asof).total_seconds() <= settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC
            or latest.get("quality_status") != "ok"
            or latest_at is None or latest_at.date() != now.date()
            or not 0 <= (now - latest_at).total_seconds() <= settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC
            or expires_at is None or now > expires_at
            or latest.get("round_id") not in pool.get("quote_round_ids", [])):
        return "temporary_data_wait", {"data_wait_reason": "current_confirmation_unavailable"}
    # Only a healthy complete pool can prove a stock-specific invalidation.
    # Missing/unfinished continuity is unknown, not a terminal strategy rejection.
    if member.get("eligible") is False or (not member and any(
            x.get("code") == code for x in pool.get("invalidated", []))):
        return "current_stock_invalidated", {}
    if member.get("eligible") is not True or member.get("currently_confirmed") is not True:
        return "temporary_data_wait", {"data_wait_reason": "current_continuity_unproven"}
    return None, {"checked_at": now.isoformat(), "current_pool_as_of": asof.isoformat(),
                  "current_pool_expires_at": expires_at.isoformat(),
                  "quote_round_id": latest["round_id"]}


def build_c3_research_message(item: dict) -> PushMessage:
    p = item["payload"]
    content = (
        f"**{_text(item['name'], 30)}（{_text(item['code'], 10)}）**\n"
        "**仅研究观察 · 未下单 · 无执行账户 · 不连接交易**\n"
        f"原始确认时间：{_text(p['signal_observed_at'])}（北京时间）\n"
        f"原始个股行情：{_text(p.get('source_quote_at'))}\n"
        f"通知当前复验时点：{_text(p.get('checked_at'))}\n"
        f"当前池证据时点：{_text(p.get('current_pool_as_of'))}\n"
        f"原确认参考价：¥{item['price']:.2f}（非成交价、非当前报价）\n"
        "原始多帧确认与通知复验分别保留；不代表可交易许可或收益承诺。\n"
        "失去确认条件、行情过期或资格变化即停止通知；勿据此直接追买。"
    )
    return PushMessage(title=f"C3研究观察信号·未下单/{item['run_id']}",
        content=content, category="c3_research_signal", msg_type="signal", priority=6,
        extra={"display_title": "C3研究观察信号·未下单", "feishu_sections": [content],
               "research_only": True, "execution_connected": False})


async def dispatch_c3_research(*, now: datetime | None = None, session_factory=None) -> dict:
    """Independent append-only notification queue; no shadow mutation or execution calls."""
    if _c3_lock.locked():
        return {"status": "busy"}
    if not settings.C3_RESEARCH_PUSH_ENABLED or not settings.PUSH_ENABLED:
        _runtime["c3_research"] = {"status": "disabled"}
        return {"status": "disabled"}
    async with _c3_lock:
        at = now or datetime.now()
        _runtime["c3_research"] = {"status": "running", "started_at": at.isoformat()}
        try:
            result = await _dispatch_c3(now=at, session_factory=session_factory or async_session)
            _runtime["c3_research"] = {**result, "started_at": at.isoformat()}
            return result
        except asyncio.CancelledError:
            _runtime["c3_research"] = {"status": "interrupted", "started_at": at.isoformat()}
            raise  # The owning scheduler supervises lifecycle, not an inner timeout.
        except Exception as exc:
            _runtime["c3_research"] = {"status": "retryable_error", "error": type(exc).__name__,
                                      "started_at": at.isoformat()}
            raise


async def _c3_delivery_rows(db: AsyncSession, now: datetime) -> list[PaperAutoTradeLog]:
    """Keep the complete queue; avoid a broad account_id=NULL index scan.

    Stage/time projection selects the same immutable log IDs before fetching
    payloads. Do not LIMIT this set: older sent receipts still enforce cooldown
    and hourly limits, and every pending signal needs its terminal audit.
    """
    metadata = (await db.execute(select(
        PaperAutoTradeLog.id, PaperAutoTradeLog.source, PaperAutoTradeLog.account_id,
    ).where(
        PaperAutoTradeLog.stage_code.in_(("c3_research_signal", "c3_research_delivery")),
        PaperAutoTradeLog.trade_date >= now.date() - timedelta(days=1),
        PaperAutoTradeLog.created_at <= now,
    ).order_by(PaperAutoTradeLog.id))).all()
    ids = [row.id for row in metadata if row.source == C3_ROUTE and row.account_id is None]
    rows = []
    # Remain below legacy SQLite bind limits; no per-page state-machine execution.
    for offset in range(0, len(ids), 400):
        rows.extend((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.id.in_(ids[offset:offset + 400]),
        ).order_by(PaperAutoTradeLog.id))).all())
    return rows


async def _dispatch_c3(*, now: datetime, session_factory) -> dict:
    from app.models.paper import PaperShadowEvent
    from app.paper.strategy_iteration_shadow import route_version_for
    # Only fresh current-day source rows are ingress candidates. No historical backfill.
    async with session_factory() as db:
        events = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == C3_ROUTE,
            PaperShadowEvent.route_version == route_version_for(C3_ROUTE),
            PaperShadowEvent.event_type == "confirmed", PaperShadowEvent.status == "confirmed",
            PaperShadowEvent.trade_date == now.date(),
            PaperShadowEvent.observed_at >= now - timedelta(seconds=settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC),
            PaperShadowEvent.observed_at <= now,
            # One immutable confirmed per route/version/day/code. Exclude durable
            # ingress before LIMIT so a busy first page cannot starve later codes.
            ~select(PaperAutoTradeLog.id).where(
                PaperAutoTradeLog.source == C3_ROUTE,
                PaperAutoTradeLog.account_id.is_(None),
                PaperAutoTradeLog.action == "research_signal",
                PaperAutoTradeLog.stage_code == "c3_research_signal",
                PaperAutoTradeLog.strategy_version == PaperShadowEvent.route_version,
                PaperAutoTradeLog.trade_date == PaperShadowEvent.trade_date,
                PaperAutoTradeLog.code == PaperShadowEvent.code,
            ).exists(),
        ).order_by(PaperShadowEvent.id).limit(100))).all())
        for event in events:
            run_id = "c3notify:" + hashlib.sha256(event.event_key.encode()).hexdigest()[:30]
            exists = await db.scalar(select(PaperAutoTradeLog.id).where(
                PaperAutoTradeLog.run_id == run_id, PaperAutoTradeLog.action == "research_signal"))
            if exists:
                continue
            cause, quote = _c3_original_check(event, now)
            payload = {"notification_schema": "c3_research_notification_v1",
                       "shadow_event_key": event.event_key, "shadow_event_id": event.id,
                       "signal_observed_at": event.observed_at.isoformat(),
                       "route_version": event.route_version, "research_only": True,
                       "execution_connected": False, **quote}
            row = PaperAutoTradeLog(account_id=None, run_id=run_id, trade_date=event.trade_date,
                created_at=now, trigger="push_worker", source=C3_ROUTE, code=event.code,
                name=event.name, price=event.price, strategy_version=event.route_version,
                as_of_at=_clock(quote.get("source_quote_at")), action="research_signal",
                decision="confirmed", stage_code="c3_research_signal",
                reason="C3不可变确认的研究通知入口；无执行连接",
                candidate_json=json.dumps(payload, ensure_ascii=False))
            db.add(row)
            await db.flush()
            if cause:
                await _audit(db, _item(row), "rejected", now, delivery_timing={"cause": cause})
        await db.commit()
    async with session_factory() as db:
        rows = await _c3_delivery_rows(db, now)
        last = {r.run_id: r for r in rows if r.action == "research_push"}
        sent = [r for r in rows if r.action == "research_push" and r.decision == "sent"]
        chosen = None
        pool_cache = None
        checked_count = 0
        for row in rows:
            if row.action != "research_signal":
                continue
            status = last.get(row.run_id)
            if status and status.decision in {"sent", "expired", "rejected", "disabled"}:
                continue
            item = _item(row)
            if not _delivery_fresh(item, now) or not _c3_session(now):
                await _audit(db, item, "expired", now, delivery_timing={
                    "cause": "original_signal_expired",
                    "previous_status": status.decision if status else "pending",
                    "previous_cause": _payload(status).get("cause") if status else None})
                continue
            if status and (now - status.created_at).total_seconds() < (
                    max(30, settings.PAPER_BUY_POINT_PUSH_RETRY_SEC) if status.decision == "attempting"
                    else settings.PAPER_BUY_POINT_PUSH_RETRY_SEC):
                continue
            cooldown = any(r.code == row.code and
                (now - r.created_at).total_seconds() < settings.PUSH_STOCK_COOLDOWN for r in sent)
            hourly = sum((now - r.created_at).total_seconds() < 3600 for r in sent)
            if cooldown or hourly >= settings.PUSH_HOURLY_LIMIT:
                await _audit(db, item, "throttled", now, delivery_timing={
                    "cause": "stock_cooldown" if cooldown else "hourly_batch_limit"})
                continue
            if checked_count >= 100:
                break  # Commit bounded work; durable unprocessed rows remain pending.
            checked_count += 1
            event = await db.get(PaperShadowEvent, item["payload"].get("shadow_event_id"))
            cause, _ = _c3_original_check(event, now)
            if not cause and (event.event_key != item["payload"].get("shadow_event_key")
                              or event.code != row.code):
                cause = "event_identity_mismatch"
            checked = {}
            if not cause:
                if pool_cache is None:
                    from app.paper.strategy_iteration_shadow import build_first_board_current_pool
                    pool_cache = await build_first_board_current_pool(db, now=now)
                cause, checked = await _c3_current_check(db, row.code, now, pool=pool_cache)
            item["payload"].update(checked)
            if cause:
                await _audit(db, item, "waiting" if cause == "temporary_data_wait" else "rejected",
                             now, delivery_timing={"cause": cause, "checked_at": now.isoformat()})
                continue
            await _audit(db, item, "attempting", now)
            chosen = item
            break  # Bounded independent slice; original queue retains priority.
        await db.commit()
    if chosen is None:
        return {"status": "idle", "count": 0}
    # The attempting commit may have waited while newer invalidation evidence
    # arrived. Use a new read session, never the selection cache/identity map.
    # This closes that wait window, not the market-to-HTTP atomicity gap.
    recheck_started = datetime.now()
    checked = {}
    cause = None
    if _delivery_fresh(chosen, recheck_started) and _c3_session(recheck_started):
        async with session_factory() as db:
            cause, checked = await _c3_current_check(db, chosen["code"], recheck_started)
    recheck_completed = datetime.now()
    # Release even the read transaction before card construction and transport.
    # Query failures/cancellation propagate with the durable attempting lease.
    # Card and receipt describe the same final proof, without overwriting the
    # immutable signal round, reference price or original observation clocks.
    card_item = {**chosen, "payload": {
        **chosen["payload"],
        **{key: checked.get(key) for key in (
            "checked_at", "current_pool_as_of", "current_pool_expires_at")},
        "recheck_quote_round_id": checked.get("quote_round_id"),
    }}
    message = build_c3_research_message(card_item) if cause is None else None
    final_checked = datetime.now()
    current_expiry = _clock(checked.get("current_pool_expires_at"))
    checked_at = _clock(checked.get("checked_at"))
    data_wait_reason = checked.get("data_wait_reason")
    started = None
    if not _delivery_fresh(chosen, final_checked) or not _c3_session(final_checked):
        status, cause = "expired", "expired_before_transport"
    elif cause is not None:
        status = "waiting" if cause == "temporary_data_wait" else "rejected"
    elif (current_expiry is None or checked_at is None
            or not recheck_started <= recheck_completed <= final_checked
            or not checked_at <= final_checked <= current_expiry):
        # Expiry of current proof is unknown, not expiry of the original signal.
        # Retry only within the unchanged original TTL and existing queue cadence.
        status, cause = "waiting", "temporary_data_wait"
        data_wait_reason = "current_confirmation_unavailable"
    else:
        started = final_checked
        try:
            result = await push_scheduler.push_to_channels(
                message, [feishu_channel], use_throttle=True)
            status = "sent" if result.get("channels", {}).get("feishu") is True else result.get("status", "failed")
            if status not in {"sent", "failed", "throttled", "disabled", "skipped"}:
                status = "failed"
        except Exception:
            status = "failed"
    completed = datetime.now()
    async with session_factory() as db:
        await _audit(db, chosen, status, completed, delivery_timing={
            "cause": cause, "data_wait_reason": data_wait_reason,
            "recheck_started_at": recheck_started.isoformat(),
            "recheck_completed_at": recheck_completed.isoformat(),
            "final_checked_at": final_checked.isoformat(),
            "checked_at": checked.get("checked_at"),
            "current_pool_as_of": checked.get("current_pool_as_of"),
            "current_pool_expires_at": checked.get("current_pool_expires_at"),
            "recheck_quote_round_id": checked.get("quote_round_id"),
            "send_started_at": started.isoformat() if started else None,
            "send_completed_at": completed.isoformat() if started else None,
            "user_received_at": None})
        await db.commit()
    return {"status": status, "count": 1}


async def c3_research_notification_receipts(db: AsyncSession, *, event_keys: list[str],
                                          now: datetime | None = None) -> dict[str, dict]:
    """Public read-only keyed receipts. Missing key means not queued, never unconfirmed."""
    if not event_keys:
        return {}
    now = now or datetime.now()
    run_ids = ["c3notify:" + hashlib.sha256(key.encode()).hexdigest()[:30] for key in event_keys]
    with db.no_autoflush:
        rows = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.run_id.in_(run_ids), PaperAutoTradeLog.source == C3_ROUTE,
            PaperAutoTradeLog.account_id.is_(None), PaperAutoTradeLog.created_at <= now,
            PaperAutoTradeLog.stage_code.in_(("c3_research_signal", "c3_research_delivery")),
        ).order_by(PaperAutoTradeLog.id))).all())
    result = {}
    for row in rows:
        p = _payload(row)
        key = p.get("shadow_event_key")
        if key not in event_keys or row.action not in {"research_signal", "research_push"}:
            continue
        result[key] = {
            **{k: p.get(k) for k in ("shadow_event_id", "route_version", "signal_observed_at",
                "checked_at", "cause", "data_wait_reason", "previous_status", "previous_cause",
                "send_started_at", "send_completed_at", "recheck_started_at",
                "recheck_completed_at", "final_checked_at", "recheck_quote_round_id")},
            "signal_log_id": row.id if row.action == "research_signal" else p.get("signal_log_id"),
            "delivery_log_id": row.id if row.action == "research_push" else None,
            "status": row.decision if row.action == "research_push" else "pending",
            "user_received_at": None, "research_only": True, "execution_connected": False,
        }
    return result


async def notification_status(*, session_factory=None) -> dict:
    now = datetime.now()
    async with (session_factory or async_session)() as db:
        rows = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.created_at >= datetime.combine(now.date(), time.min),
            PaperAutoTradeLog.stage_code.in_(("strategy_buy_point", "notification_delivery", "notification_ingress")),
        ).order_by(PaperAutoTradeLog.id))).all())
    last = {row.run_id: row for row in rows if row.action == DELIVERY}
    ingress_counts: dict[str, int] = {}
    ingress_recent = []
    for row in rows:
        if row.action == "signal_ingress":
            ingress_counts[row.decision] = ingress_counts.get(row.decision, 0) + 1
            ingress_recent.append({"run_id": row.run_id, "code": row.code,
                                   "status": row.decision, **_payload(row)})
    counts: dict[str, int] = {}
    recent = []
    for row in rows:
        if row.action != SIGNAL:
            continue
        delivery = last.get(row.run_id)
        status = delivery.decision if delivery else "pending"
        counts[status] = counts.get(status, 0) + 1
        recent.append({"signal_id": row.id, "account_name": _payload(row).get("account_name"),
                       "strategy_version": row.strategy_version, "code": row.code,
                       "name": row.name, "reason": row.reason,
                       "strategy_label": _payload(row).get("strategy_label"),
                       "quote_round_id": row.quote_round_id,
                       "status": status, "observed_at": row.created_at.isoformat(),
                       "event_key": _payload(row).get("signal_key"),
                       "delivery_cause": _payload(delivery).get("cause") if delivery else None,
                       "previous_cause": _payload(delivery).get("previous_cause") if delivery else None})
    return {
        "enabled": settings.PAPER_BUY_POINT_PUSH_ENABLED and settings.PUSH_ENABLED,
        "feishu_configured": await feishu_channel.is_available(),
        "accounts": list(ACCOUNT_NAMES), "counts_today": counts, "recent": recent[-50:],
        "portfolio_accounts": ["shared_50k"],
        "ingress_counts_today": ingress_counts, "ingress_recent": ingress_recent[-50:],
        "ingress_recovery_contract": "session_handoff_not_durable_until_independent_commit",
        "template_version": TEMPLATE_VERSION,
        "feishu_paper_buy_points_only": settings.FEISHU_PAPER_BUY_POINTS_ONLY,
        "card_budget_bytes": FEISHU_CARD_BUDGET_BYTES,
        "max_age_sec": settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC,
        "poll_interval_sec": settings.PAPER_BUY_POINT_PUSH_INTERVAL_SEC,
        **_runtime,
    }
