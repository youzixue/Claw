"""持续模拟实验的可信账户边界；不改变实盘及策略自身的必要数据条件。"""

from datetime import date, datetime, time
import hashlib
import json

from app.config.settings import settings
from sqlalchemy import select, desc
from app.models.regime import MarketRegimeSnapshot
from app.paper.account_policy import account_parameter_snapshot

EXPERIMENT_ACCOUNTS = (
    "default", "promotion", "mainline", "auction", "tenbagger", "reversal",
    "challenger_a", "challenger_b", "challenger_c", "challenger_d",
    "challenger_e", "challenger_f2",
)
# 这些路线仅以历史形态、真实报价、量价路径/盘口为入场证据。
# A/B/C/D主账户仍依赖资金或情绪证据，不允许用昨日资金或缺失值放行。
PRICE_PATH_ACCOUNTS = frozenset({
    "tenbagger", "reversal", "challenger_a", "challenger_b", "challenger_c",
    "challenger_d", "challenger_e", "challenger_f2",
})


def experiment_activation_at() -> datetime | None:
    """返回本地前向实验的精确启用边界；配置异常时失败关闭。"""
    try:
        start = date.fromisoformat(settings.PAPER_EXPERIMENT_START_DATE)
    except (TypeError, ValueError):
        return None
    raw = str(getattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "") or "").strip()
    if not raw:
        return datetime.combine(start, time.min)
    try:
        activation = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return activation if activation.date() == start else None


def experiment_active(account_name: str, *, broker: str = "paper", at=None) -> bool:
    if broker != "paper" or account_name not in EXPERIMENT_ACCOUNTS:
        return False
    if not settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED:
        return False
    observed = at or datetime.now()
    day = observed.date() if isinstance(observed, datetime) else observed
    try:
        start = date.fromisoformat(settings.PAPER_EXPERIMENT_START_DATE)
    except (TypeError, ValueError):
        return False
    if not isinstance(day, date) or day < start:
        return False
    if isinstance(observed, datetime):
        activation = experiment_activation_at()
        if activation is None:
            return False
        try:
            return observed >= activation
        except TypeError:
            return False
    # 仅日期调用决定当日策略扫描路径；真实下单还会携带datetime再次校验。
    return True


def experiment_order_override(account_name: str, *, at=None) -> bool | None:
    """None沿用普通模式；实验启动前仅管理退出，不补单。"""
    if account_name not in EXPERIMENT_ACCOUNTS or not settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED:
        return None
    return experiment_active(account_name, at=at)


def sentiment_is_required(account_name: str) -> bool:
    return account_name not in PRICE_PATH_ACCOUNTS


def sentiment_quality_at(state, *, at: datetime, strict: bool) -> tuple[str, str]:
    quality = str(state.quality_status or "missing")
    reason = str(state.quality_reason or "")
    if not strict:
        return quality, reason
    observed = getattr(state, "observed_at", None)
    if not isinstance(observed, datetime):
        return "missing", f"{reason}；情绪快照缺少观测时钟"
    age = (at - observed).total_seconds()
    if observed.date() != at.date() or age < 0 or age > settings.PAPER_EXPERIMENT_SENTIMENT_MAX_AGE_SEC:
        return "stale", f"{reason}；情绪快照时点{observed.isoformat()}不在当前决策有效窗口"
    return quality, reason


# Execution semantics version, distinct from the backward-readable v1 JSON schema.
PENDING_BUY_VALIDITY_VERSION = "pending_buy_validity_v2"
# Shared entry/pending severity semantics, scoped only to route-connected accounts.
LIVE_ROUTE_CONFIRMATION_CONTRACT_VERSION = "route_confirmation_nonbool_v2"
# Primary pending quotes evaluate known-invalid predicates before missing inputs.
# Only A-F and E2 consume this contract; connected shadow routes keep theirs.
PRIMARY_BUY_CONFIRMATION_CONTRACT_VERSION = "primary_source_quote_v4"
# B/C/D alone consume the formal snapshot visibility and finite-price contract.
PROMOTION_CANDIDATE_CONTRACT_VERSION = "promotion_candidate_clock_numeric_v1"
REVERSAL_PENDING_EVIDENCE_CONTRACT_VERSION = "reversal_pending_evidence_v1"
A_ZERO_LOT_RISK_CAP_CONTRACT_VERSION = "a_zero_lot_risk_cap_v1"
# A consumes staged layers; the five event accounts consume daily-name counting.
BUY_ORDER_COUNT_CONTRACT_VERSION = "paper_buy_order_count_v1"
# Primary daily/sector quotas only; do not rotate the five connected accounts.
PRIMARY_NEW_BUY_QUOTA_CONTRACT_VERSION = "primary_new_buy_order_quota_v1"
# Pending T reductions may yield only to a newly validated full protective exit.
EXIT_UPGRADE_CONTRACT_VERSION = "paper_exit_upgrade_v1"
# All twelve routes evaluate holding age at the decision calendar, not NAV cache.
EXIT_HOLD_CLOCK_CONTRACT_VERSION = "paper_exit_hold_clock_v1"
# Only the seven short-exit accounts consume the opening weak-gate scope.
SHORT_EXIT_WEAK_GATE_CONTRACT_VERSION = "short_exit_weak_gate_scope_v1"
# Only E/E2 consume the explicit non-degenerate high-board entry modes.
HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION = "highboard_entry_mode_v1"
# Only C consumes independent live-sector visibility across all rank scopes.
MAINLINE_SECTOR_CLOCK_CONTRACT_VERSION = "mainline_sector_clock_v1"
# Only E/E2 consume mode-terminal precedence and explicit queue quantity evidence.
HIGHBOARD_PENDING_MODE_CONTRACT_VERSION = "highboard_pending_mode_terminal_v1"
LIMIT_QUEUE_EVIDENCE_CONTRACT_VERSION = "limit_queue_evidence_v1"


def execution_signal_identity(account_name: str) -> dict:
    """Bind execution to this account's live signal semantics and effective TTL.

    Resolve the evidence module lazily: account_policy is also consumed by the
    evidence module, so importing it there at module scope would create a cycle.
    No evidence is generated and no database or order is touched here.
    """
    from app.paper.account_policy import ROUTE_ACCOUNT_NAMES, challenger_execution_policy

    route_id = next(
        (route for route, name in ROUTE_ACCOUNT_NAMES.items() if name == account_name),
        None,
    )
    if route_id is not None:
        ttl = challenger_execution_policy(route_id)["max_execution_delay_sec"]
        if account_name == "challenger_a":
            route_version = str(settings.PAPER_MOMENTUM_RETEST_SHADOW_VERSION)
        else:
            from app.paper.strategy_iteration_shadow import route_version_for
            route_version = route_version_for(route_id)
    else:
        if account_name not in EXPERIMENT_ACCOUNTS:
            raise KeyError(f"unknown paper account: {account_name}")
        ttl = settings.PAPER_PENDING_BUY_MAX_AGE_SEC
        route_version = None
    identity = {
        "route_id": route_id,
        "route_version": route_version,
        "pending_buy_guard_version": PENDING_BUY_VALIDITY_VERSION,
        "pending_buy_max_age_sec": ttl,
        "exit_upgrade_contract": EXIT_UPGRADE_CONTRACT_VERSION,
        "exit_hold_clock_contract": EXIT_HOLD_CLOCK_CONTRACT_VERSION,
    }
    if account_name in {"default", "promotion", "mainline", "auction",
                        "challenger_b", "challenger_c", "challenger_d"}:
        identity["short_exit_weak_gate_contract"] = SHORT_EXIT_WEAK_GATE_CONTRACT_VERSION
    if route_id is not None:
        identity["live_route_confirmation_contract"] = LIVE_ROUTE_CONFIRMATION_CONTRACT_VERSION
    else:
        identity["primary_buy_confirmation_contract"] = PRIMARY_BUY_CONFIRMATION_CONTRACT_VERSION
    if account_name in {"promotion", "mainline", "auction"}:
        identity["promotion_candidate_contract"] = PROMOTION_CANDIDATE_CONTRACT_VERSION
    if account_name == "reversal":
        identity["reversal_pending_evidence_contract"] = REVERSAL_PENDING_EVIDENCE_CONTRACT_VERSION
    if account_name == "mainline":
        identity["mainline_sector_clock_contract"] = MAINLINE_SECTOR_CLOCK_CONTRACT_VERSION
    if account_name in {"tenbagger", "challenger_e"}:
        identity["highboard_entry_mode_contract"] = HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION
        identity["highboard_pending_mode_contract"] = HIGHBOARD_PENDING_MODE_CONTRACT_VERSION
        identity["limit_queue_evidence_contract"] = LIMIT_QUEUE_EVIDENCE_CONTRACT_VERSION
    if account_name == "default":
        identity["zero_lot_risk_cap_contract"] = A_ZERO_LOT_RISK_CAP_CONTRACT_VERSION
    if account_name == "default" or route_id is not None:
        identity["buy_order_count_contract"] = BUY_ORDER_COUNT_CONTRACT_VERSION
    if account_name in {
        "default", "promotion", "mainline", "auction", "tenbagger", "reversal", "challenger_e",
    }:
        identity["primary_new_buy_quota_contract"] = PRIMARY_NEW_BUY_QUOTA_CONTRACT_VERSION
    return identity


def standard_execution_version(base: str, account_name: str) -> str:
    """Version non-experiment orders without changing the legacy position label."""
    identity = json.dumps({
        "base": base,
        "account_name": account_name,
        "parameters": account_parameter_snapshot(account_name),
        "signal_identity": execution_signal_identity(account_name),
    }, sort_keys=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{base[:24]}:execution_v2:{digest}"


def experiment_parameters(account_name: str | None = None) -> dict:
    """Return the public protocol snapshot, scoped to one account when supplied."""
    common = {
        "PAPER_EXPERIMENT_VERSION": settings.PAPER_EXPERIMENT_VERSION,
        "PAPER_EXPERIMENT_START_DATE": settings.PAPER_EXPERIMENT_START_DATE,
        "PAPER_EXPERIMENT_ACTIVATION_AT": settings.PAPER_EXPERIMENT_ACTIVATION_AT,
        "PAPER_EXPERIMENT_REGIME_VERSION": settings.PAPER_EXPERIMENT_REGIME_VERSION,
    }
    if account_name is None:
        return common
    parameters = {
        **common,
        **account_parameter_snapshot(account_name),
        "signal_identity": execution_signal_identity(account_name),
    }
    if sentiment_is_required(account_name):
        from app.data.main_fund import MAIN_FUND_POLICY_VERSION
        parameters["MAIN_FUND_DATA_POLICY"] = MAIN_FUND_POLICY_VERSION
        parameters["FUND_FLOW_SOURCE_MAX_AGE_SEC"] = settings.FUND_FLOW_SOURCE_MAX_AGE_SEC
    return parameters


def execution_version(base: str, account_name: str | None = None) -> str:
    """Hash only this account's effective policy plus shared hard constraints.

    ``account_name`` should be supplied by execution callers.  The optional
    protocol-only fallback preserves compatibility for generic tooling that has
    no account identity; it must not be used to persist an account trade.
    """
    protocol = str(settings.PAPER_EXPERIMENT_VERSION)
    identity = json.dumps({
        "base": base,
        "protocol": protocol,
        "parameters": experiment_parameters(account_name),
    }, sort_keys=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{base[:24]}:{protocol[:20]}:{digest}"


def experiment_status(account_name: str, *, at=None) -> dict:
    active = experiment_active(account_name, at=at)
    return {
        "mode": "continuous_paper" if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED else "standard",
        "protocol_version": settings.PAPER_EXPERIMENT_VERSION,
        "start_date": settings.PAPER_EXPERIMENT_START_DATE,
        "activation_at": settings.PAPER_EXPERIMENT_ACTIVATION_AT,
        "first_session_scope": "afternoon_only",
        "active": active,
        "market_regime_policy": "tag_only" if active else "standard",
        "sentiment_required": sentiment_is_required(account_name),
        "real_order_connected": False,
        "force_daily_fill": False,
    }


async def freeze_entry_evidence(db, account_name: str, *, at: datetime, sentiment: dict) -> dict:
    """冻结下单时已知状态；不以未来收盘/事后改写的快照给过去交易贴标签。"""
    from app.api.v1 import paper
    from app.paper.experiment_regime import freeze_benchmark_regime, previous_known_trade_day

    snapshot = await db.scalar(
        select(MarketRegimeSnapshot).where(
            MarketRegimeSnapshot.as_of_at <= at,
            MarketRegimeSnapshot.created_at <= at,
            MarketRegimeSnapshot.trade_date <= at.date(),
        ).order_by(desc(MarketRegimeSnapshot.as_of_at), desc(MarketRegimeSnapshot.id)).limit(1)
    )
    previous = await previous_known_trade_day(db, at=at)
    benchmark = await freeze_benchmark_regime(db, at=at, previous_trade_date=previous)
    valid_style = bool(snapshot and snapshot.trade_date in {at.date(), previous}
                       and snapshot.quality_status == "good")
    return {
        **experiment_status(account_name, at=at),
        "observed_at": at.isoformat(),
        "strategy_version": paper._strategy_version(account_name),
        "base_strategy_version": paper._strategy_base_version(account_name),
        # Freeze only this account's effective entry/exit configuration plus
        # genuinely shared execution/data constraints; never embed peer values.
        "parameters": experiment_parameters(account_name),
        "exit_parameters": paper._strategy_sell_params_by_name(account_name),
        "entry_sentiment": dict(sentiment),
        "entry_bull_bear": benchmark,
        "entry_regime": {
            "label": snapshot.primary_regime if valid_style else "unknown",
            "observed_label": snapshot.primary_regime if snapshot else None,
            "snapshot_key": snapshot.snapshot_key if snapshot else None,
            "source_trade_date": snapshot.trade_date.isoformat() if snapshot else None,
            "as_of_at": snapshot.as_of_at.isoformat() if snapshot else None,
            "quality_status": snapshot.quality_status if snapshot else "missing",
            "confidence": snapshot.confidence if snapshot else None,
            "classification": "point_in_time_market_style",
            "scope": "same_session" if snapshot and snapshot.trade_date == at.date() else "prior_session",
            "bull_bear_label": benchmark["label"],
            "note": "情绪/市场风格不等同于长期牛熊；缺失或过期状态单独分组，不事后补标签",
        },
    }
