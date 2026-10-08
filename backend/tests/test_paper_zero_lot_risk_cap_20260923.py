"""Risk budget below a lot must remain zero, not disable the risk cap."""
from types import SimpleNamespace
import pytest
from app.api.v1 import paper

@pytest.mark.parametrize("price,cash,expected", [
    (100.0,50000.0,200),
    (200.0,50000.0,100),
    (200.01,50000.0,0),
    (300.0,50000.0,0),
    (300.0,100.0,0),
])
def test_auto_size_obeys_zero_and_exact_lot_hard_loss_budget(monkeypatch,price,cash,expected):
    monkeypatch.setattr(paper,"experiment_active",lambda *a,**kw:True)
    monkeypatch.setattr(paper,"_current_account_drawdown",lambda account:0)
    monkeypatch.setattr(paper.settings,"PAPER_AUTO_MAX_POSITIONS",2)
    monkeypatch.setattr(paper.settings,"PAPER_AUTO_HARD_STOP_MAX_LOSS_PCT",2.0)
    monkeypatch.setattr(paper.settings,"PAPER_AUTO_STOP_LOSS_PCT",5.0)
    monkeypatch.setattr(paper,"_auto_position_pct",lambda score:.5)
    account=SimpleNamespace(account_name="default",total_assets=50000.0,initial_capital=50000.0,current_capital=cash)
    qty=paper._auto_buy_amount(account,price,1,score=95)
    assert qty==expected
    assert qty%100==0 and qty>=0
    assert qty*price*.05<=50000*.02+1e-9

@pytest.mark.parametrize("standard",[False,True])
def test_zero_lot_contract_rotates_only_a_not_peer_accounts(monkeypatch,standard):
    from app.paper import experiment
    fn=experiment.standard_execution_version if standard else experiment.execution_version
    before={name:fn("fixed-base",name) for name in experiment.EXPERIMENT_ACCOUNTS}
    monkeypatch.setattr(experiment,"A_ZERO_LOT_RISK_CAP_CONTRACT_VERSION","a_zero_lot_risk_cap_test")
    after={name:fn("fixed-base",name) for name in experiment.EXPERIMENT_ACCOUNTS}
    assert {name for name in before if before[name]!=after[name]}=={"default"}
    for name in before:
        assert ("zero_lot_risk_cap_contract" in experiment.execution_signal_identity(name))==(name=="default")
