from app.api.v1 import paper
from app.config.settings import settings
from app.paper.account_policy import (
    ACCOUNT_NAMES,
    account_entry_exit_policy,
    account_parameter_snapshot,
    account_sell_params,
)
from app.paper.experiment import execution_version


def test_all_twelve_accounts_have_explicit_effective_snapshots():
    snapshots = {name: account_parameter_snapshot(name) for name in ACCOUNT_NAMES}

    assert len(snapshots) == 12
    assert all(snapshot["account_name"] == name for name, snapshot in snapshots.items())
    assert all(snapshot["account"] for snapshot in snapshots.values())
    assert all(snapshot["shared_execution"] for snapshot in snapshots.values())


def test_secondary_defaults_match_current_values_without_runtime_inheritance():
    assert account_entry_exit_policy("challenger_b") == {
        "position_pct": 0.20, "max_daily_buys": 2, "max_positions": 3,
        "take_profit_pct": 12.0, "stop_loss_pct": 5.0, "max_hold_days": 3,
    }
    assert account_entry_exit_policy("challenger_c")["stop_loss_pct"] == 2.5
    assert account_entry_exit_policy("challenger_d")["position_pct"] == 0.15
    assert account_entry_exit_policy("challenger_e")["take_profit_pct"] == 18.0
    assert account_entry_exit_policy("challenger_f2")["stop_loss_pct"] == 8.0


def test_sell_profile_contains_every_implicit_short_exit_dependency():
    expected = {
        "take_profit_pct", "stop_loss_pct", "small_stop_loss_pct",
        "open_severe_stop_loss_pct", "next_day_min_profit_pct", "max_hold_days",
        "pullback_from_high_pct", "breakeven_protect_high_profit_pct",
        "breakeven_protect_low_pct", "breakeven_protect_high_pct",
        "volume_negative_ratio", "open_noise_end", "trade_t_enabled",
        "t_sell_pct", "t_weak_sell_pct", "short_full_exit_profit_max_amount",
        # 2026-09-17 复盘修复：开盘噪声窗的独立走弱证据门槛也是隐式退出依赖，
        # 必须随账户参数冻结并进入 exit_parameters 审计，否则窗内豁免无法回放。
        "open_noise_stop_min_evidence", "open_noise_weak_min_evidence",
        # 2026-09-17 改1/改3：窗外弱信号 rung 的独立证据门槛
        "weak_exit_min_evidence",
    }

    for account_name in ACCOUNT_NAMES:
        assert expected == set(account_sell_params(account_name))
        assert account_parameter_snapshot(account_name)["sell"] == account_sell_params(account_name)


def test_default_account_keeps_exact_configurable_auto_exit_values(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT", 6.75)
    monkeypatch.setattr(settings, "PAPER_AUTO_PULLBACK_FROM_HIGH_PCT", 2.75)
    monkeypatch.setattr(settings, "PAPER_AUTO_T_SELL_PCT", 0.45)

    params = account_sell_params("default")

    assert params["open_severe_stop_loss_pct"] == 6.75
    assert params["pullback_from_high_pct"] == 2.75
    assert params["t_sell_pct"] == 0.45


def test_global_auto_exit_mechanics_do_not_silently_change_secondary(monkeypatch):
    before = account_parameter_snapshot("challenger_b")
    version = execution_version("b2-route", "challenger_b")

    monkeypatch.setattr(settings, "PAPER_AUTO_PULLBACK_FROM_HIGH_PCT", 99.0)
    monkeypatch.setattr(settings, "PAPER_AUTO_T_SELL_PCT", 0.99)

    assert account_parameter_snapshot("challenger_b") == before
    assert execution_version("b2-route", "challenger_b") == version


def test_main_account_change_does_not_change_secondary_snapshot_or_version(monkeypatch):
    before_snapshot = account_parameter_snapshot("challenger_b")
    before_version = execution_version("b2-route", "challenger_b")

    monkeypatch.setattr(settings, "PAPER_PROMOTION_STOP_LOSS_PCT", 99.0)

    assert account_parameter_snapshot("challenger_b") == before_snapshot
    assert execution_version("b2-route", "challenger_b") == before_version


def test_one_secondary_change_only_rotates_its_own_version(monkeypatch):
    b_before = execution_version("b2-route", "challenger_b")
    c_before = execution_version("c2-route", "challenger_c")

    monkeypatch.setattr(settings, "PAPER_CHALLENGER_B_STOP_LOSS_PCT", 5.25)

    assert execution_version("b2-route", "challenger_b") != b_before
    assert execution_version("c2-route", "challenger_c") == c_before


def test_e2_entry_constraints_clone_current_values_but_are_independent(monkeypatch):
    before = account_parameter_snapshot("challenger_e")
    assert before["account"]["PAPER_CHALLENGER_E_REQUIRE_ABOVE_VWAP"] is True
    assert before["account"]["PAPER_CHALLENGER_E_MAX_PULLBACK_FROM_HIGH_PCT"] == 2.0
    assert before["account"]["PAPER_CHALLENGER_E_LIMIT_UP_QUEUE_ENABLED"] is True
    assert before["account"]["PAPER_CHALLENGER_E_INTRADAY_BUY_START"] == "09:30"
    assert before["account"]["PAPER_CHALLENGER_E_QUEUE_CANCEL_TIME"] == "14:50"

    monkeypatch.setattr(settings, "PAPER_HIGHBOARD_REQUIRE_ABOVE_VWAP", False)
    monkeypatch.setattr(settings, "PAPER_HIGHBOARD_MAX_PULLBACK_FROM_HIGH_PCT", 99.0)

    assert account_parameter_snapshot("challenger_e") == before


def test_real_strategy_version_call_isolates_all_account_parameters(monkeypatch):
    before = {name: paper._strategy_version(name) for name in ACCOUNT_NAMES}

    monkeypatch.setattr(settings, "PAPER_CHALLENGER_B_STOP_LOSS_PCT", 5.25)

    after = {name: paper._strategy_version(name) for name in ACCOUNT_NAMES}
    changed = {name for name in ACCOUNT_NAMES if before[name] != after[name]}
    assert changed == {"challenger_b"}


def test_legacy_confirmation_settings_only_belong_to_a_not_peer_accounts(monkeypatch):
    before = {name: paper._strategy_version(name) for name in ACCOUNT_NAMES}

    monkeypatch.setattr(settings, "PAPER_INTRADAY_CONFIRM_MIN_SAMPLES", 4)

    after = {name: paper._strategy_version(name) for name in ACCOUNT_NAMES}
    changed = {name for name in ACCOUNT_NAMES if before[name] != after[name]}
    assert changed == {"default"}
    assert "PAPER_AUTO_TRADE_T_ENABLED" not in account_parameter_snapshot(
        "promotion"
    )["shared_strategy_constraints"]


def test_shared_execution_change_rotates_each_account_version(monkeypatch):
    b_before = execution_version("b2-route", "challenger_b")
    c_before = execution_version("c2-route", "challenger_c")

    monkeypatch.setattr(settings, "PAPER_EXECUTION_SLIPPAGE_PCT", 0.11)

    assert execution_version("b2-route", "challenger_b") != b_before
    assert execution_version("c2-route", "challenger_c") != c_before
