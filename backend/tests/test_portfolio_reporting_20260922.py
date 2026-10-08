"""Read-only reporting contract on temporary async SQLite only."""
import json
from datetime import datetime
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models.paper import PaperAccount, PaperPosition, PaperNav, PaperPortfolioSignal, PaperPortfolioDecision, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.paper.portfolio_reporting import portfolio_report

TABLES=[PaperAccount,PaperPosition,PaperNav,PaperTradeLog,TradeOrder,TradeFill,PaperPortfolioSignal,PaperPortfolioDecision]
NOW=datetime(2026,9,22,13,30)

async def setup():
    engine=create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        for model in TABLES:
            await c.run_sync(lambda conn,m=model:m.__table__.create(conn))
    return engine,async_sessionmaker(engine,expire_on_commit=False)

def guard(engine):
    statements=[]
    def before(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT"), statement
    event.listen(engine.sync_engine,"before_cursor_execute",before)
    return statements

@pytest.mark.asyncio
async def test_empty_read_does_not_create_or_flush():
    engine,session=await setup()
    try:
        async with session() as db:
            db.add(PaperAccount(account_name="default",initial_capital=50000))
            statements=guard(engine)
            result=await portfolio_report(db)
            assert result["status"]=="not_started"
            assert result["wallet"] is None and result["initial_budget"]==50000
            assert all(result[k]==[] for k in ("positions","pending_orders","nav","signals","decisions"))
            assert len(db.new)==1 and statements
            json.dumps(result,allow_nan=False)
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_single_wallet_real_ledger_and_all_decisions():
    engine,session=await setup()
    try:
        async with session() as db:
            db.add_all([PaperAccount(id=1,account_name="shared_50k",initial_capital=50000,current_capital=30000,total_assets=50123,total_return=.246),
                PaperAccount(id=2,account_name="default",initial_capital=999999,current_capital=999999),
                PaperPosition(account_id=1,code="600000",name="test",buy_price=10,buy_amount=100,buy_time=NOW,current_price=11,profit_loss=100,profit_pct=10,is_closed=False),
                PaperNav(account_id=1,trade_date=NOW.date(),nav=1.00246,daily_return=None),
                TradeOrder(order_id="o",broker="paper",account_id="shared_50k",code="600001",side="buy",order_type="limit",price=10,quantity=200,filled_quantity=100,status="partial",trade_date=NOW.date()),
                TradeOrder(order_id="other",broker="paper",account_id="default",code="600002",side="buy",order_type="limit",price=99,quantity=100,status="pending",trade_date=NOW.date()),
                PaperPortfolioSignal(id=1,signal_key="k",portfolio_version="old",origin_account="default",origin_account_id=2,origin_version="v1",source="radar",code="600001",source_signal_id="s",confirmed_at=NOW,decision_round_id="r",as_of_at=NOW,observed_at=NOW,candidate_json="{}",entry_policy_json="{}",exit_policy_json="{}"),
                PaperPortfolioDecision(decision_key="d",signal_id=1,portfolio_version="old",account_id=1,decision_round_id="r",as_of_at=NOW,observed_at=NOW,decision="rejected",reason_code="risk_blocked",reason="blocked",order_id="o",budget_json="{}")])
            await db.commit()
            statements=guard(engine)
            result=await portfolio_report(db)
            assert result["wallet"]["cash"]==30000
            assert result["wallet"]["stored_total_assets"]==50123
            assert result["wallet"]["reserved_cash"]==1005
            assert result["wallet"]["available_cash"]==28995
            assert len(result["pending_orders"])==1
            assert result["positions"][0]["profit_loss"]==100
            assert result["positions"][0]["origin"]["status"]=="unknown"
            assert result["nav"][0]["nav"]==1.00246
            assert result["decisions"][0]["decision"]=="rejected"
            assert result["decisions"][0]["order_status"]=="partial"
            assert result["signals"][0]["origin_account"]=="default"
            assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
            json.dumps(result,allow_nan=False)
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_known_first_fill_origin():
    from app.paper.portfolio_contract import entry_version
    engine,session=await setup()
    at=datetime(2000,1,1,10)
    version=entry_version("original",policy_version="old_policy")
    origin=dict(portfolio_signal_key="key",origin_account="default",origin_account_id=2,
                origin_version="original",portfolio_version="old_policy",entry_version=version)
    try:
        async with session() as db:
            db.add_all([
                PaperAccount(id=1,account_name="shared_50k",initial_capital=50000,current_capital=49000,total_assets=50000),
                PaperPosition(account_id=1,code="600000",buy_price=10,buy_amount=100,buy_time=at,current_price=10,is_closed=False,strategy_version=version),
                PaperTradeLog(id=7,account_id=1,code="600000",trade_type="buy",price=10,amount=100,trade_time=at,strategy_version=version),
                TradeOrder(order_id="fill-order",broker="paper",account_id="shared_50k",code="600000",side="buy",order_type="limit",price=10,quantity=100,filled_quantity=100,status="filled",source="radar",signal_id="s",strategy_version=version,risk_json=json.dumps({"paper_portfolio_origin":origin}),created_at=at),
                TradeFill(fill_id="f",order_id="fill-order",broker="paper",code="600000",side="buy",price=10,quantity=100,broker_trade_id="7",filled_at=at),
                PaperPortfolioSignal(signal_key="key",portfolio_version="old_policy",origin_account="default",origin_account_id=2,origin_version="original",source="radar",code="600000",source_signal_id="s",confirmed_at=at,as_of_at=at,observed_at=at,decision_round_id="r",candidate_json="{}",entry_policy_json="{}",exit_policy_json="{}")
            ])
            await db.commit()
            guard(engine)
            result=await portfolio_report(db)
            assert result["positions"][0]["origin"]["status"]=="known"
            assert result["positions"][0]["origin"]["origin_version"]=="original"
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_display_limit_does_not_truncate_wallet_reservations():
    engine,session=await setup()
    try:
        async with session() as db:
            db.add(PaperAccount(id=1,account_name="shared_50k",initial_capital=50000,current_capital=50000,total_assets=50000))
            for i in range(3):
                db.add(TradeOrder(order_id=f"pending{i}",broker="paper",account_id="shared_50k",code=f"60000{i}",side="buy",order_type="limit",price=10,quantity=300,filled_quantity=100,status="partial"))
            await db.commit()
            guard(engine)
            result=await portfolio_report(db,limit=1)
            assert len(result["pending_orders"])==1
            assert result["wallet"]["reserved_cash"]==6030  # 3 orders * 2 remaining minimum-fee slices
            assert result["pending_orders"][0]["reserved_cash"]==2010
            assert result["counts"]["pending_orders"]==3
    finally:
        await engine.dispose()

@pytest.mark.parametrize("price,quantity,filled",[(float("inf"),100,0),(1e308,100,0),(10,100,200),(10,100,None)])
@pytest.mark.asyncio
async def test_invalid_pending_values_no_invented_reservation(price,quantity,filled):
    engine,session=await setup()
    try:
        async with session() as db:
            db.add(PaperAccount(id=1,account_name="shared_50k",initial_capital=50000,current_capital=50000,total_assets=50000))
            db.add(TradeOrder(order_id="bad",broker="paper",account_id="shared_50k",code="600000",side="buy",order_type="limit",price=price,quantity=quantity,filled_quantity=filled,status="pending"))
            await db.commit()
            guard(engine)
            result=await portfolio_report(db)
            if filled is None: # ORM default converts omitted/null value to zero.
                assert result["wallet"]["reserved_cash"]==1005
            else:
                assert result["wallet"]["reserved_cash"] is None
                assert result["status"]=="degraded"
            json.dumps(result,allow_nan=False)
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_bad_values_and_duplicate_wallet_fail_closed():
    engine,session=await setup()
    try:
        async with session() as db:
            db.add(PaperAccount(id=1,account_name="shared_50k",initial_capital=50000,current_capital=float("inf"),total_assets=None))
            await db.commit()
            listener=[]
            def before(conn,cursor,statement,parameters,context,executemany):
                listener.append(statement)
                assert statement.lstrip().upper().startswith("SELECT")
            event.listen(engine.sync_engine,"before_cursor_execute",before)
            result=await portfolio_report(db,limit=0)
            assert result["wallet"]["cash"] is None and result["status"]=="degraded"
            json.dumps(result,allow_nan=False)
            event.remove(engine.sync_engine,"before_cursor_execute",before)
            db.add(PaperAccount(id=2,account_name="shared_50k",initial_capital=50000,current_capital=50000))
            await db.commit()
            guard(engine)
            result=await portfolio_report(db)
            assert result["wallet"] is None and result["status"]=="degraded"
            assert "ambiguous_shared_wallet" in result["warnings"]
    finally:
        await engine.dispose()
