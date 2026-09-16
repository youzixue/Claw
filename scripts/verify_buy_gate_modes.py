"""验证三种回撤开仓闸门模式对当前账户的决策影响."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.api.v1 import paper as paper_api  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.models.paper import PaperAccount  # noqa: E402


async def evaluate_mode(session: AsyncSession, mode: str) -> dict:
    """用指定模式评估当前账户的开仓闸门状态."""
    # 临时切换模式
    original = settings.PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE
    settings.PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE = mode
    try:
        account = (
            await session.execute(
                select(PaperAccount).where(
                    PaperAccount.account_name == "default",
                    PaperAccount.status == "active",
                )
            )
        ).scalars().first()
        if not account:
            return {"error": "no active paper account"}
        drawdown = paper_api._current_account_drawdown(account)
        # 模拟一个普通 candidate
        open_count = 0
        today_new_buy_count = 0
        candidate_score = 75.0
        pause_reason = paper_api._auto_buy_pause_reason(
            account,
            open_count=open_count,
            today_new_buy_count=today_new_buy_count,
            candidate_score=candidate_score,
        )
        recovery_buy_ok = paper_api._is_drawdown_recovery_buy(
            account,
            open_count=open_count,
            today_new_buy_count=today_new_buy_count,
            candidate_score=candidate_score,
        )
        # 单笔仓位数
        amount = paper_api._auto_buy_amount(
            account, price=10.0, open_count=open_count, score=candidate_score
        )
        return {
            "mode": mode,
            "current_drawdown_pct": drawdown,
            "max_drawdown_pct": abs(float(account.max_drawdown or 0)),
            "score_75_can_buy": pause_reason == "",
            "score_75_recovery_buy": recovery_buy_ok,
            "buy_amount_at_10.0": amount,
            "pause_reason": pause_reason or "(放行)",
        }
    finally:
        settings.PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE = original


async def main() -> None:
    db_url = settings.DATABASE_URL
    engine = create_async_engine(db_url, future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            import json
            for mode in ["pause", "cautious", "unlimited"]:
                print(f"\n=== {mode.upper()} 模式 ===")
                result = await evaluate_mode(session, mode)
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
