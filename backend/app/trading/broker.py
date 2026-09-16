"""券商适配器接口与模拟盘实现"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class BrokerOrderRequest:
    order_id: str
    code: str
    side: str
    price: float
    quantity: int
    order_type: str = "limit"
    reason: str = ""
    signal_id: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    source: str = ""
    account_name: str = "default"   # 五策略并行: 成交落账的目标账户 (2026-08-31)
    entry_sector_code: str | None = None
    entry_sector_name: str | None = None
    decision_round_id: str = ""
    fill_round_id: str = ""
    filled_at: datetime | None = None
    stop_loss_price: float | None = None


@dataclass
class BrokerFill:
    fill_id: str
    price: float
    quantity: int
    commission: float = 0
    tax: float = 0
    realized_pnl: float | None = None
    broker_trade_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    filled_at: datetime = field(default_factory=datetime.now)


@dataclass
class BrokerOrderResult:
    accepted: bool
    status: str
    external_order_id: str = ""
    error_message: str = ""
    fills: list[BrokerFill] = field(default_factory=list)


class BrokerAdapter(Protocol):
    broker_name: str

    async def place_order(self, db: AsyncSession, req: BrokerOrderRequest) -> BrokerOrderResult:
        ...

    async def cancel_order(self, db: AsyncSession, external_order_id: str) -> BrokerOrderResult:
        ...

    async def sync_account(self, db: AsyncSession) -> dict:
        ...

    async def sync_positions(self, db: AsyncSession) -> list[dict]:
        ...


class PaperBrokerAdapter:
    """用现有模拟盘作为 broker，打通委托/成交回报接口。"""

    broker_name = "paper"

    async def place_order(self, db: AsyncSession, req: BrokerOrderRequest) -> BrokerOrderResult:
        from app.api.v1 import paper
        from app.trading.paper_authorization import authorize_broker_request

        authorize_broker_request(db, req)
        # 五策略并行: 按 req.account_name 路由成交到对应账户, 不再硬编码 default (2026-08-31 修复)
        account_name = str(req.account_name or paper.PAPER_ACCOUNT_DEFAULT)

        challenger_context_token = None
        fill_context_token = paper._PAPER_FILL_CONTEXT.set({
            "decision_round_id": str(req.decision_round_id or ""),
            "fill_round_id": str(req.fill_round_id or ""),
            "committed_at": req.filled_at or datetime.now(),
        })
        strategy_version = str(
            req.strategy_version or paper._strategy_version(account_name)
        )
        strategy_context_token = paper._AUTO_STRATEGY_VERSION_CONTEXT.set(
            strategy_version
        )
        authorized_challenger_order = bool(
            account_name in paper.PAPER_CHALLENGER_ACCOUNTS
            and req.strategy_id == "paper-challenger-forward"
            and paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.get(req.source) == account_name
            and str(req.signal_id or "").startswith("chlg-")
        )
        # E2由高标主循环产生命令，而不是影子确认事件；仍严格限制账户/来源/策略。
        authorized_challenger_order = authorized_challenger_order or bool(
            account_name == paper.PAPER_ACCOUNT_CHALLENGER_E
            and req.strategy_id == "paper-auto-short"
            and req.source == "tenbagger_midline"
            and str(req.signal_id or "").startswith("auto-tenbagger_midline-")
        )
        authorized_challenger_order = authorized_challenger_order or bool(
            account_name in paper.PAPER_CHALLENGER_ACCOUNTS
            and req.side == "sell"
            and req.strategy_id == "paper-auto-short"
            and req.source == "position"
            and str(req.signal_id or "").startswith("auto-sell-")
        )
        if authorized_challenger_order:
            challenger_context_token = paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.set(True)
        try:
            if req.side == "buy":
                result = await paper._book_paper_buy(
                    paper.SimBuyRequest(
                        code=req.code,
                        price=req.price,
                        amount=req.quantity,
                        signal_id=req.signal_id or req.order_id,
                        reason=req.reason,
                        entry_sector_code=req.entry_sector_code,
                        entry_sector_name=req.entry_sector_name,
                        stop_loss_price=req.stop_loss_price,
                    ),
                    account_name=account_name,
                    db=db,
                )
            elif req.side == "sell":
                result = await paper._book_paper_sell(
                    paper.SimSellRequest(
                        code=req.code,
                        price=req.price,
                        amount=req.quantity,
                        signal_id=req.signal_id or req.order_id,
                        reason=req.reason,
                    ),
                    account_name=account_name,
                    db=db,
                )
            else:
                return BrokerOrderResult(
                    accepted=False,
                    status="rejected",
                    error_message=f"不支持的交易方向: {req.side}",
                )
        finally:
            if challenger_context_token is not None:
                paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.reset(challenger_context_token)
            paper._AUTO_STRATEGY_VERSION_CONTEXT.reset(strategy_context_token)
            paper._PAPER_FILL_CONTEXT.reset(fill_context_token)

        if isinstance(result, dict) and result.get("idempotent_replay") is True:
            # 相同委托键的重试已在service返回原回报。另一个委托不能认领旧
            # PaperTradeLog再造TradeFill，更不能把旧成交时间重贴为现在。
            return BrokerOrderResult(accepted=False, status="rejected",
                error_message="模拟账本已有该成交请求；禁止新委托重复认领旧成交回报")
        # 缺少真实落账凭证绝不能用原请求价/量拼出一张filled回报。
        trade = result.get("trade") if isinstance(result, dict) else None
        if (not isinstance(trade, dict) or not trade.get("id")
                or trade.get("price") != req.price or trade.get("amount") != req.quantity):
            raise ValueError("模拟账本未返回匹配的成交凭证，禁止合成成交回报")
        from app.trading.paper_authorization import ledger_timing_evidence
        timing = ledger_timing_evidence(db)
        filled_at = req.filled_at or datetime.now()
        if timing is not None:
            filled_at = datetime.fromisoformat(timing["before_mutation_checked_at"])
            if datetime.fromisoformat(trade["trade_time"]) != filled_at:
                raise ValueError("模拟账本成交时钟与变更前验收不一致")
            trade = {**trade, "ledger_execution_timing": timing}
        commission = float(trade.get("commission") or 0)
        tax = float(trade.get("tax") or 0)
        realized_pnl = trade.get("realized_pnl")
        if realized_pnl is None:
            realized_pnl = trade.get("pnl")
        fill = BrokerFill(
            fill_id=f"fill-{req.order_id}",
            price=float(trade["price"]),
            quantity=int(trade["amount"]),
            commission=commission,
            tax=tax,
            realized_pnl=realized_pnl,
            broker_trade_id=str(trade.get("id") or ""),
            raw=trade,
            filled_at=filled_at,
        )
        return BrokerOrderResult(
            accepted=True,
            status="filled",
            external_order_id=f"paper-{req.order_id}",
            fills=[fill],
        )

    async def cancel_order(self, db: AsyncSession, external_order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult(
            accepted=False,
            status="rejected",
            external_order_id=external_order_id,
            error_message="paper broker 委托即时成交，不能撤单",
        )

    async def sync_account(self, db: AsyncSession) -> dict:
        from app.api.v1 import paper

        account = await paper._get_or_create_account(db)
        account = await paper._refresh_account(db, account)
        return paper._account_payload(account)

    async def sync_positions(self, db: AsyncSession) -> list[dict]:
        from app.api.v1 import paper

        account = await paper._get_or_create_account(db)
        await paper._refresh_account(db, account)
        positions = await paper._open_positions(db, account.id)
        return [paper._position_payload(item) for item in positions]


def get_broker_adapter(name: str = "paper") -> BrokerAdapter:
    if name == "paper":
        return PaperBrokerAdapter()
    raise ValueError(f"未配置券商适配器: {name}")
