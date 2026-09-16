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
from datetime import datetime, timedelta, time
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.session import async_session
from app.models.paper import PaperAutoTradeLog
from app.models.stock import StockTag
from app.paper.account_policy import ACCOUNT_NAMES
from app.push.channels.base import PushMessage
from app.push.channels.feishu import feishu_channel, FEISHU_CARD_BUDGET_BYTES
from app.push.scheduler import push_scheduler

SIGNAL = "buy_signal"
DELIVERY = "signal_push"
TERMINAL = {"sent", "expired"}
TEMPLATE_VERSION = "paper_buy_point_card_v4"
_dispatch_lock = asyncio.Lock()
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
    blocks = ["**📈 买点时行情**\n" + "\n".join(
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


async def record_buy_point(
    db: AsyncSession, *, account: Any, strategy_version: str, label: str,
    code: str, name: str, source: str, signal_key: str, reason: str,
    price: float, observed_at: datetime, decision_run_id: str,
    quote_round_id: str, as_of_at: datetime | None,
    code_version: str = "", queue_order: bool = False,
    market_context: dict | None = None,
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
    try:
        async with db.begin_nested():
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
        logger.error("策略买点通知记录失败 account={} code={} error={}", account.account_name, code, type(exc).__name__)
        return None


async def _audit(db: AsyncSession, item: dict, status: str, now: datetime, *, batch_id: str = "",
                 delivery_timing: dict | None = None) -> None:
    db.add(PaperAutoTradeLog(
        account_id=item["account_id"], run_id=item["run_id"], trade_date=item["trade_date"],
        created_at=now, trigger="push_worker", source="feishu", code=item["code"],
        name=item["name"], action=DELIVERY, decision=status, reason=f"飞书买点投递：{status}",
        strategy_version=item["strategy_version"], quote_round_id=item["quote_round_id"],
        stage_code="notification_delivery", reason_code=f"buy_point_push_{status}",
        candidate_json=json.dumps({"signal_log_id": item["id"], "batch_id": batch_id,
                                   **(delivery_timing or {})}, ensure_ascii=False),
    ))


def _item(row: PaperAutoTradeLog) -> dict:
    return {key: getattr(row, key) for key in (
        "id", "account_id", "run_id", "trade_date", "created_at", "as_of_at",
        "code", "name", "reason", "price", "strategy_version", "quote_round_id",
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
    blocks = ["**✅ 买点依据**\n" + "\n".join(
        f"• {part}" for part in evidence
    )] if evidence else []
    if metrics:
        blocks.append("**📊 确认时指标**\n" + "\n".join(
            " ｜ ".join(metrics[i:i + 2]) for i in range(0, len(metrics), 2)
        ))
    return blocks or ["**✅ 买点依据**\n• 未提供详细说明，请核对信号审计。"]


def build_message(items: list[dict]) -> PushMessage:
    if not items:
        raise ValueError("买点提醒不可使用空信号列表")
    badges = dict(zip(ACCOUNT_NAMES, ("A", "B", "C", "D", "E", "F",
                                     "A2", "B2", "C2", "D2", "E2", "F2")))
    state_labels = {
        "filled": "✅ 已有模拟成交记录",
        "pending": "⏳ 已提交模拟委托 · 未确认成交",
        "waiting": "⏸ 本轮未下单 · 等待条件",
        "blocked": "⛔ 本轮未下单 · 已拦截",
        "expired": "⌛ 原买点已失效 · 本轮未下单",
    }
    sections, audits, accounts = [], [], []
    for index, item in enumerate(items, 1):
        payload = item["payload"]
        badge = badges.get(payload.get("account_name"), "未知账户")
        if badge not in accounts:
            accounts.append(badge)
        role = _text(payload.get("account_role") or "账户类型待核对", 30)
        state = state_labels.get(item.get("execution_state"), "🔎 买点已确认 · 非下单或成交")
        execution_note = _text(item.get("execution_note"), 250)
        context = payload.get("market_context") or {}
        own_quote_time = _clock(context.get("source_quote_at"))
        quote_time = own_quote_time or item["as_of_at"]
        quote_label = "个股行情时点" if own_quote_time else "行情轮次时点"
        signal_time = _clock(payload.get("signal_observed_at")) or item["created_at"]
        parts = [
            f"**{index}. {_text(item['name'], 30)}（{_text(item['code'], 10)}）｜{badge} {role}**",
            f"{_text(payload.get('strategy_label'), 120)}",
            f"**{state}**" + (f"\n{execution_note}" if execution_note else ""),
            f"**信号参考价 ¥{item['price']:.2f}**（非成交价）\n"
            f"确认：{signal_time:%Y-%m-%d %H:%M:%S}（北京时间）\n"
            f"{quote_label}：{quote_time:%H:%M:%S}",
            *_buy_point_reason_blocks(item["reason"]),
            *_market_blocks(context),
            "**下单前核对**：APP最新价格与上述买点条件是否仍一致；失去确认条件时不要照消息追买。",
        ]
        if payload.get("queue_order"):
            parts.append("**⚠️ 回封排队提示**\n排队不等于成交；须由后续真实盘口撮合确认。")
        sections.append("\n\n".join(parts))
        # 保留后台身份/执行追溯，不再把技术编号和实验说明铺在卡片里。
        audits.append({"signal_id": item["id"], "run_id": item["run_id"],
                       "account": payload.get("account_name"), "strategy_version": item["strategy_version"],
                       "execution_state": item.get("execution_state", "unconfirmed"),
                       "execution_note": item.get("execution_note") or payload.get("execution_status")})
    if len(items) == 1:
        display_title = (f"策略买点｜{accounts[0]} · {_text(items[0]['name'], 30)}"
                         f"（{_text(items[0]['code'], 10)}）")
    else:
        display_title = f"策略买点｜{' / '.join(accounts)} · {len(items)}条"
    return PushMessage(
        # 保留内部标题和信号身份，避免改版改变现有限频去重语义。
        title=f"Claw 策略买点确认 · {len(items)}条 · " + "/".join(str(item["id"]) for item in items),
        content="\n\n---\n\n".join(sections),
        category="paper_buy_point", msg_type="signal", priority=8,
        # 汇总卡无单一股票；逐账户/版本/股票冷却在持久日志层执行。
        extra={"signal_ids": [item["id"] for item in items],
               "display_title": display_title, "template_version": TEMPLATE_VERSION,
               "feishu_sections": sections, "signal_audits": audits},
    )


def _fit_batch(items: list[dict]) -> list[dict]:
    """按实际中文UTF-8卡片字节预算选择前缀，未选信号留在队列而不记投递。"""
    selected = list(items[:6])
    while selected:
        card = feishu_channel._build_card(build_message(selected))
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= FEISHU_CARD_BUDGET_BYTES:
            return selected
        selected.pop()
    return []


async def dispatch_buy_points(*, now: datetime | None = None, session_factory=None) -> dict:
    """独立消费者；关闭DB读事务后才联网，发送前落attempting租约，失败可恢复。"""
    if _dispatch_lock.locked():
        return {"status": "busy"}
    async with _dispatch_lock:
        return await _dispatch(now=now or datetime.now(), session_factory=session_factory or async_session)


def _delivery_fresh(item: dict, at: datetime) -> bool:
    """发送前按每股原始时钟再验；等待DB期间不能把旧确认刷新为新信号。"""
    context = item["payload"].get("market_context") or {}
    source_at = _clock(context.get("source_quote_at")) if context else item["as_of_at"]
    clocks = (item["created_at"], item["as_of_at"], source_at)
    return bool(item["trade_date"] == at.date() and all(
        stamp is not None and stamp.tzinfo is None
        and 0 <= (at - stamp).total_seconds() <= settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC
        for stamp in clocks))


async def _dispatch(*, now: datetime, session_factory) -> dict:
    _runtime["last_poll_at"] = now.isoformat()
    _runtime["last_error"] = None
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
            if status is not None and status.decision in TERMINAL:
                continue
            item = _item(row)
            # 关闭通知期间仍保留研究分母；之后重新打开也不补发这些记录。
            if item["payload"].get("notification_allowed") is False:
                continue
            if not _delivery_fresh(item, now):
                await _audit(db, item, "expired", now)
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
                await _audit(db, item, "throttled", now)
                continue
            # 从同次决策读取已提交日志。未知状态绝不写成已成交。
            decision = await db.scalar(select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.account_id == row.account_id,
                PaperAutoTradeLog.code == row.code,
                PaperAutoTradeLog.source == row.source,
                PaperAutoTradeLog.strategy_version == row.strategy_version,
                PaperAutoTradeLog.created_at <= now,
                PaperAutoTradeLog.run_id == item["payload"].get("decision_run_id"),
                PaperAutoTradeLog.action.in_(("buy", "deferred_buy", "skip_buy", "wait_buy", "skip_terminal")),
            ).order_by(PaperAutoTradeLog.id.desc()).limit(1))
            if decision is not None:
                if decision.action == "buy" and decision.decision == "executed" and decision.executed_trade_id:
                    item["execution_state"] = "filled"
                    item["execution_note"] = "已有模拟成交记录；" + str(decision.reason or "")
                elif decision.action == "deferred_buy":
                    item["execution_state"] = "pending"
                    item["execution_note"] = "已提交模拟委托，等待撮合，未确认成交；" + str(decision.reason or "")
                else:
                    item["execution_state"] = {
                        "wait_buy": "waiting", "skip_buy": "blocked",
                        "skip_terminal": "expired",
                    }.get(decision.action, "unconfirmed")
                    item["execution_note"] = "尚未确认成交；" + str(decision.reason or decision.decision)
            # 单卡最多6个完整理由，且同账户同股不在同一批重复。
            if len(items) < 6 and not any(
                x["account_id"] == row.account_id and x["code"] == row.code
                and x["strategy_version"] == row.strategy_version for x in items
            ):
                items.append(item)
        items = _fit_batch(items)
        batch_id = hashlib.sha256("|".join(x["run_id"] for x in items).encode()).hexdigest()[:24]
        for item in items:
            await _audit(db, item, "attempting", now, batch_id=batch_id)
        await db.commit()
    # 租约提交/关闭连接可能因锁争用等待。每次等待后重验，并逐股过期，
    # 不因一只过期而丢弃同批仍有效信号；终态写入后再次检查剩余项。
    while items:
        message = build_message(items)
        send_started_at = datetime.now()
        expired = [item for item in items if not _delivery_fresh(item, send_started_at)]
        if not expired:
            break
        async with session_factory() as db:
            for item in expired:
                await _audit(db, item, "expired", send_started_at, batch_id=batch_id)
            await db.commit()
        expired_ids = {item["id"] for item in expired}
        items = [item for item in items if item["id"] not in expired_ids]
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


async def notification_status(*, session_factory=None) -> dict:
    now = datetime.now()
    async with (session_factory or async_session)() as db:
        rows = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.created_at >= datetime.combine(now.date(), time.min),
            PaperAutoTradeLog.stage_code.in_(("strategy_buy_point", "notification_delivery")),
        ).order_by(PaperAutoTradeLog.id))).all())
    last = {row.run_id: row.decision for row in rows if row.action == DELIVERY}
    counts: dict[str, int] = {}
    recent = []
    for row in rows:
        if row.action != SIGNAL:
            continue
        status = last.get(row.run_id, "pending")
        counts[status] = counts.get(status, 0) + 1
        recent.append({"signal_id": row.id, "account_name": _payload(row).get("account_name"),
                       "strategy_version": row.strategy_version, "code": row.code,
                       "name": row.name, "reason": row.reason,
                       "strategy_label": _payload(row).get("strategy_label"),
                       "quote_round_id": row.quote_round_id,
                       "status": status, "observed_at": row.created_at.isoformat()})
    return {
        "enabled": settings.PAPER_BUY_POINT_PUSH_ENABLED and settings.PUSH_ENABLED,
        "feishu_configured": await feishu_channel.is_available(),
        "accounts": list(ACCOUNT_NAMES), "counts_today": counts, "recent": recent[-50:],
        "template_version": TEMPLATE_VERSION,
        "feishu_paper_buy_points_only": settings.FEISHU_PAPER_BUY_POINTS_ONLY,
        "card_budget_bytes": FEISHU_CARD_BUDGET_BYTES,
        "max_age_sec": settings.PAPER_BUY_POINT_PUSH_MAX_AGE_SEC,
        "poll_interval_sec": settings.PAPER_BUY_POINT_PUSH_INTERVAL_SEC,
        **_runtime,
    }
