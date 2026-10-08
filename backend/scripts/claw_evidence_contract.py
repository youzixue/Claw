"""Flat MCP contracts, no application imports or startup side effects."""
ACCOUNTS = [
    "default", "promotion", "mainline", "auction", "tenbagger", "reversal",
    "challenger_a", "challenger_b", "challenger_c", "challenger_d", "challenger_e", "challenger_f2",
]
DAY = {"type": "string", "format": "date"}
AT = {"type": "string", "maxLength": 40, "description": "ISO cutoff; naive means Asia/Shanghai; aware normalized; never future"}
ACCOUNT = {"type": "string", "enum": ACCOUNTS}
CODE = {"type": "string", "pattern": "^[0-9]{6}$", "maxLength": 6}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}
CURSOR = {"type": "integer", "minimum": 0, "maximum": 1000000000, "default": 0}


def definition(description, properties, required=("trade_date",)):
    return {"description": description, "inputSchema": {
        "type": "object", "properties": {"trade_date": DAY, "as_of": AT, **properties},
        "required": list(required), "additionalProperties": False}}


EVIDENCE_TOOLS = {
    "ashare_review_readiness": definition(
        "Read stored trading calendar, expected/actual dates, immutable snapshots and finalized outcome/artifact readiness. No calendar sync or build; late premarket is blocked.",
        {"phase": {"type": "string", "enum": ["postmarket", "premarket"]}},
        ("trade_date", "phase")),
    "paper_research_artifact": definition(
        "Read a bounded section/page of already-published immutable signal/portfolio or post-exit research files. No publish/replay; retain all missing/excluded denominators.",
        {"artifact_id": {"type": "string", "maxLength": 100},
         "section": {"type": "string", "enum": ["summary", "signal_portfolio", "post_exit"], "default": "summary"},
         "account_name": ACCOUNT, "code": CODE, "cursor": CURSOR, "limit": LIMIT}),
    "paper_execution_evidence": definition(
        "SELECT-only twelve-account all-version actual execution/accounting facts, daily orders/fills/trades, current stored positions and complete-cycle details. Current projections are not historical as-of valuations.",
        {"account_name": ACCOUNT, "code": CODE, "cursor": CURSOR, "limit": LIMIT,
         "section": {"type": "string", "enum": ["summary", "orders", "fills", "trades", "positions", "cycles"], "default": "summary"}}),
    "paper_decision_trace": definition(
        "Read per-stock candidate/confirmation/risk/entry/exit logs, frozen inputs and reused audit IDs. No scanning or account creation. Missing trace remains unknown.",
        {"account_name": ACCOUNT, "code": CODE, "run_id": {"type": "string", "maxLength": 80},
         "strategy_version": {"type": "string", "maxLength": 100},
         "log_id": {"type": "integer", "minimum": 1}, "cursor": CURSOR, "limit": LIMIT}),
    "paper_notification_ledger": definition(
        "Read historical buy_signal/ingress/push evidence by day/account/code with exact recorded transport receipt links. sent is not user received or a fill. C3 stays separate.",
        {"account_name": ACCOUNT, "code": CODE, "cursor": CURSOR, "limit": LIMIT}),
    "ashare_market_review_universe": definition(
        "Read the stored market universe: legacy raw page, full bounded descriptive summary, or compact feature pages. Batch streams sampled minute files once per call for all stored codes and winner/non-rising cohorts; missing/truncated counts remain explicit, not a frozen strategy-control denominator.",
        {"cursor": {"type": "string", "maxLength": 10, "default": ""},
         "limit": LIMIT, "include_history": {"type": "boolean", "default": True},
         "section": {"type": "string", "enum": ["page", "summary", "features"], "default": "page"},
         "cohort": {"type": "string", "enum": ["all", "rising_or_limit", "non_rising"], "default": "all"}}),
    "ashare_price_evidence": definition(
        "Read explicit-date daily compatibility bars or archived sampled 1m/5m with missing minutes, source clocks/hashes and basis. Cumulative volume is not minute volume; no history backfill.",
        {"code": CODE, "period": {"type": "string", "enum": ["daily", "sampled_1m", "sampled_5m"], "default": "daily"},
         "cursor": CURSOR, "limit": LIMIT}, ("trade_date", "code")),
    "ashare_premarket_context": definition(
        "SELECT-only prior frozen review + PIT overnight news across holidays + bounded stored external evidence. Unknown US sessions/futures or stale projections stay unavailable; never fetch/refresh.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
         "section": {"type": "string", "enum": ["context", "news_page"], "default": "context"},
         "cursor": {"type": "string", "maxLength": 256, "default": "",
                    "description": "news_page keyset cursor bound to the exact date/as_of; pages cap 25"}}),
}
MODEL_READ_TOOLS = frozenset({
    "ashare_prediction_runs", "ashare_training_runs", "ashare_shadow_runs",
    "ashare_shadow_evaluations", "ashare_deployments",
})
