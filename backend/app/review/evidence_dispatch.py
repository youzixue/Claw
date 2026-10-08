"""Local MCP adapter into isolated existing evidence. No business HTTP fallback."""
from __future__ import annotations
import asyncio
import os

from sqlalchemy.exc import SQLAlchemyError

from app.review.evidence_store import bounded, cutoff, evidence_session, trade_day


async def read_local(tool_name, arguments, *, database_url=None):
    from scripts.claw_evidence_contract import MODEL_READ_TOOLS
    database_url = database_url or os.environ.get("CLAW_RESEARCH_DATABASE_URL")
    at = cutoff(arguments.get("as_of"))
    day = trade_day(arguments["trade_date"], as_of=at) if "trade_date" in arguments and tool_name not in MODEL_READ_TOOLS else None
    try:
        if tool_name == "paper_research_artifact":
            from app.review.research_artifacts import read_artifact
            result = await asyncio.to_thread(read_artifact, day, as_of=at,
                **{k: v for k, v in arguments.items() if k not in {"trade_date", "as_of"}})
            return bounded(result)
        async with evidence_session(database_url) as db:
            if tool_name in MODEL_READ_TOOLS:
                from app.review.model_evidence import read_model_evidence
                result = await read_model_evidence(db, tool_name=tool_name, arguments=arguments)
            elif tool_name == "ashare_premarket_context":
                from app.review.overnight_evidence import read_premarket_context, read_news_page
                section = arguments.get("section", "context")
                if section == "news_page":
                    result = await read_news_page(db, trade_date=day, as_of=at,
                        cursor=arguments.get("cursor", ""), limit=arguments.get("limit", 20))
                elif section == "context" and not arguments.get("cursor"):
                    result = await read_premarket_context(db, trade_date=day, as_of=at,
                                                          limit=arguments.get("limit", 100))
                else:
                    raise ValueError("unknown premarket section or cursor outside news_page")
            else:
                from app.review.evidence_readers import readiness, execution_evidence, decision_trace, market_universe
                kwargs = {k: v for k, v in arguments.items() if k not in {"trade_date", "as_of"}}
                if tool_name == "ashare_review_readiness":
                    result = await readiness(db, day=day, at=at, **kwargs)
                elif tool_name == "paper_execution_evidence":
                    result = await execution_evidence(db, day=day, at=at, **kwargs)
                elif tool_name in {"paper_decision_trace", "paper_notification_ledger"}:
                    result = await decision_trace(db, day=day, at=at,
                        notification_only=tool_name == "paper_notification_ledger", **kwargs)
                elif tool_name == "ashare_market_review_universe":
                    section = kwargs.pop("section", "page")
                    cohort = kwargs.pop("cohort", "all")
                    if section == "page":
                        if cohort != "all":
                            raise ValueError("cohort filters require summary or features")
                        result = await market_universe(db, day=day, at=at, **kwargs)
                    else:
                        from app.review.market_batch import read_market_batch
                        kwargs.pop("include_history", None)  # Raw-page option; batch always keeps six bars.
                        result = await read_market_batch(db, day=day, at=at,
                                                         section=section, cohort=cohort, **kwargs)
                elif tool_name == "ashare_price_evidence":
                    from app.review.price_evidence import price_evidence
                    result = await price_evidence(db, day=day, at=at, **kwargs)
                else:
                    raise ValueError("unknown local evidence tool")
            return bounded(result)
    except (SQLAlchemyError, OSError, ImportError) as exc:
        # Missing migrations/optional archives are missing evidence, never trigger
        # ensure/init/download or disclose SQL, database paths or credentials.
        return {"status": "unavailable", "reason": "stored_evidence_unavailable",
                "error_type": type(exc).__name__, "tool": tool_name,
                "trade_date": day.isoformat() if day else None, "as_of": at.isoformat(),
                "read_only": True, "no_refresh_or_initialization_attempted": True}
