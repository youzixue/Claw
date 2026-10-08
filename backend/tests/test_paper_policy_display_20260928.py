"""Offline display/policy contract tests. Run directly with python -B (no conftest).

Extract real function ASTs, never import app, connect DB or load .env.
Fake ORM objects exercise parameter selection only, NOT database join integrity.
Existing test_paper_position_policy.py covers real isolated DB attribution.
"""
from __future__ import annotations
import ast
import asyncio
import builtins
import json
import math
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend/app"
ACCOUNTS = ("default", "promotion", "mainline", "auction", "tenbagger", "reversal",
            "challenger_a", "challenger_b", "challenger_c", "challenger_d",
            "challenger_e", "challenger_f2")
UNUSED_HIGHBOARD = ("PAPER_HIGHBOARD_MAX_PEAK_CHANGE_PCT",
                   "PAPER_HIGHBOARD_CONFIRM_MIN_SAMPLES",
                   "PAPER_HIGHBOARD_CONFIRM_MIN_PERSISTENCE_SEC")


def tree(rel):
    return ast.parse((APP / rel).read_text())


def fn(rel, name):
    return next(n for n in tree(rel).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def load_functions(rel, names, env):
    nodes = [fn(rel, name) for name in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
                           names=[ast.alias(name="annotations")], level=0), *nodes],
                        type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(APP / rel), "exec"), env)


def literal_settings():
    cls = next(n for n in tree("config/settings.py").body
               if isinstance(n, ast.ClassDef) and n.name == "Settings")
    values = {}
    for n in cls.body:
        if not isinstance(n, ast.AnnAssign) or not isinstance(n.target, ast.Name):
            continue
        value = n.value
        if isinstance(value, ast.Call):
            value = next((k.value for k in value.keywords if k.arg == "default"), None)
        try:
            values[n.target.id] = ast.literal_eval(value)
        except (ValueError, TypeError):
            pass
    values["PAPER_ACCOUNT_CONFIRMATION_POLICIES"] = {}
    return SimpleNamespace(**values)


def policy_env():
    env = {"settings": literal_settings(), "Any": object}
    for n in tree("paper/account_policy.py").body:
        if isinstance(n, ast.Assign):
            try:
                value = ast.literal_eval(n.value)
            except (ValueError, TypeError):
                if isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "frozenset":
                    value = frozenset(ast.literal_eval(n.value.args[0]))
                else:
                    continue
            for target in n.targets:
                if isinstance(target, ast.Name):
                    env[target.id] = value
    cls = next(n for n in tree("config/settings.py").body
               if isinstance(n, ast.ClassDef) and n.name == "PaperConfirmationPolicy")
    defaults = {}
    for n in cls.body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.value, ast.Call):
            defaults[n.target.id] = ast.literal_eval(next(k.value for k in n.value.keywords if k.arg == "default"))
    env["PaperConfirmationPolicy"] = lambda: SimpleNamespace(model_dump=lambda: dict(defaults))
    load_functions("paper/account_policy.py", ["account_sell_params", "account_confirmation_policy"], env)
    return env


def display_env():
    env = policy_env()
    for n in tree("api/v1/paper.py").body:
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Name) and target.id.startswith("PAPER_ACCOUNT_"):
                    try:
                        env[target.id] = ast.literal_eval(n.value)
                    except (ValueError, TypeError):
                        pass
    env["PAPER_CHALLENGER_ACCOUNTS"] = ()
    env["_strategy_sell_params_by_name"] = env["account_sell_params"]
    load_functions("api/v1/paper.py", ["_strategy_status_override", "_midline_sell_reason"], env)
    env["_to_float"] = lambda value: float(value) if value is not None else None
    return env


class PolicyDisplayContract(unittest.TestCase):
    def test_e_display_does_not_invent_ma20_exit(self):
        env = display_env()
        policy = env["_strategy_status_override"]("tenbagger")["signal_policy"]
        self.assertNotIn("跌破MA20", policy["sell_guard"])
        self.assertIn("宽限", policy["sell_guard"])
        self.assertIn("冻结", policy["sell_guard"])

    def test_primary_short_display_discloses_weak_exit_and_frozen_basis(self):
        env = display_env()
        for name in ("promotion", "mainline", "auction"):
            with self.subTest(account=name):
                policy = env["_strategy_status_override"](name)["signal_policy"]
                prose = policy.get("value_entry_guard", "") + policy["sell_guard"]
                for false_claim in ("不因噪音离场", "无短线噪音卖点",
                                    "无小止损/次日不强就走噪音卖点"):
                    self.assertNotIn(false_claim, prose)
                self.assertIn("弱势", policy["sell_guard"])
                self.assertIn("冻结", policy["sell_guard"])

    def test_f_display_discloses_profit_expiry_grace(self):
        policy = display_env()["_strategy_status_override"]("reversal")["signal_policy"]
        self.assertIn("宽限", policy["sell_guard"])
        self.assertIn("冻结", policy["sell_guard"])

    def test_midline_actual_behavior_has_no_ma20_and_keeps_frozen_grace(self):
        env = display_env()
        sell = env["_midline_sell_reason"]
        params = dict(take_profit_pct=18, stop_loss_pct=6, max_hold_days=3, expiry_grace_days=5)
        pos = SimpleNamespace(stop_loss_price=9)
        ctx = dict(price=10.1, ma20=99)
        self.assertEqual(sell(pos, ctx, 1, 3, params), "")
        self.assertIn("宽限", sell(pos, ctx, 1, 8, params))
        self.assertIn("到期", sell(pos, ctx, 0, 3, params))
        self.assertIn("止损", sell(pos, dict(price=8.9), -11, 0, params))

    def test_e_confirmation_uses_account_policy_not_unused_highboard_knobs(self):
        env = policy_env()
        resolve = env["account_confirmation_policy"]
        before = {name: resolve(name) for name in ("tenbagger", "challenger_e")}
        for key in UNUSED_HIGHBOARD:
            setattr(env["settings"], key, 999)
        self.assertEqual(before, {name: resolve(name) for name in before})
        # Explicit account mapping is consumed; do not hook legacy 3/120 constants.
        explicit = dict(before["tenbagger"], min_samples=7, min_persistence_sec=83)
        env["settings"].PAPER_ACCOUNT_CONFIRMATION_POLICIES["tenbagger"] = SimpleNamespace(model_dump=lambda: dict(explicit))
        self.assertEqual(resolve("tenbagger"), explicit)
        self.assertEqual(resolve("challenger_e"), before["challenger_e"])
        consumer = fn("api/v1/paper.py", "_champion_intraday_confirmation_status")
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                            and n.func.id == "account_confirmation_policy" for n in ast.walk(consumer)))

    def test_entry_freeze_retains_named_account_exit_parameters(self):
        freeze = fn("paper/experiment.py", "freeze_entry_evidence")
        calls = [n for n in ast.walk(freeze) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_strategy_sell_params_by_name"]
        self.assertTrue(calls)
        self.assertTrue(any(c.args and isinstance(c.args[0], ast.Name)
                            and c.args[0].id == "account_name" for c in calls))


class _Expr:
    def __getattr__(self, _):
        return self
    def __call__(self, *_, **__):
        return self
    def __eq__(self, _):
        return self
    def __ge__(self, _):
        return self
    def __le__(self, _):
        return self


def position_env():
    def guarded_import(name, *args, **kwargs):
        if name in {"__future__", "_strptime"}:
            return builtins.__import__(name, *args, **kwargs)
        if name == "app.paper.portfolio_contract":
            return SimpleNamespace(PORTFOLIO_ACCOUNT="shared_portfolio")
        raise AssertionError("Unexpected production import: " + name)
    env = {"__builtins__": dict(vars(builtins), __import__=guarded_import),
           "datetime": datetime, "math": math, "json": json,
           "select": lambda *_: _Expr(), "PaperTradeLog": _Expr(),
           "TradeOrder": _Expr(), "TradeFill": _Expr()}
    load_functions("paper/position_policy.py", ["_object", "position_exit_policy"], env)
    return env


class FrozenPositionContract(unittest.TestCase):
    def evaluate(self, name, frozen, defaults, evidence_version="entry-v1"):
        at = datetime(2026, 9, 28, 10)
        evidence = dict(strategy_version=evidence_version, observed_at="2026-09-21T09:35:00",
                        exit_parameters=frozen)
        buy = SimpleNamespace(id=123, strategy_version="entry-v1", trade_time=datetime(2026, 9, 21, 9, 36))
        order = SimpleNamespace(order_id="original", strategy_version="entry-v1",
                                risk_json=json.dumps({"experiment_entry": evidence}))
        rows = iter([buy, order])
        async def scalar(_):
            return next(rows)
        pos = SimpleNamespace(account_id=12, code="600001", strategy_version="entry-v1",
                              buy_time=datetime(2026, 9, 21, 9, 36))
        return asyncio.run(position_env()["position_exit_policy"](
            SimpleNamespace(scalar=scalar), account_name=name, position=pos,
            defaults=defaults, as_of=at))

    def test_all_twelve_accounts_keep_complete_old_frozen_values(self):
        resolve = policy_env()["account_sell_params"]
        for name in ACCOUNTS:
            with self.subTest(account=name):
                frozen = resolve(name)
                defaults = dict(frozen, stop_loss_pct=99, take_profit_pct=98,
                                max_hold_days=97, expiry_grace_days=0)
                actual, trace = self.evaluate(name, frozen, defaults)
                self.assertEqual(actual, frozen)
                self.assertEqual(trace["basis"], "frozen_entry_order")
                self.assertEqual(trace["missing_keys"], [])
                self.assertEqual(trace["position_strategy_version"], "entry-v1")

    def test_partial_freeze_is_explicit_and_zero_remains_zero(self):
        actual, trace = self.evaluate("tenbagger", {"expiry_grace_days": 0},
                                    {"stop_loss_pct": 6, "expiry_grace_days": 5})
        self.assertEqual(actual, {"stop_loss_pct": 6, "expiry_grace_days": 0})
        self.assertEqual(trace["basis"], "partial_legacy_snapshot")
        self.assertEqual(trace["missing_keys"], ["stop_loss_pct"])

    def test_mismatched_evidence_never_masquerades_as_frozen(self):
        actual, trace = self.evaluate("tenbagger", {"stop_loss_pct": 99},
                                    {"stop_loss_pct": 6}, "other-version")
        self.assertEqual(actual["stop_loss_pct"], 6)
        self.assertEqual(trace["basis"], "entry_version_mismatch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
