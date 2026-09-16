"""评审脚本 v2: 深挖明日预案可执行候选为 0 的原因 + 三链路买点一致性."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.api.v1 import paper as paper_api  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.core.data_date import resolve_latest_trade_date  # noqa: E402
from app.models.stock import SectorPersistence  # noqa: E402


async def main() -> None:
    db_url = settings.DATABASE_URL
    engine = create_async_engine(db_url, future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            target_date = await resolve_latest_trade_date(session, SectorPersistence.trade_date)
            print(f"最新交易日: {target_date}")

            from app.api.v1.tenbagger import prewarm_next_day_plan_snapshot, _next_day_plan_action_priority

            snapshot = await prewarm_next_day_plan_snapshot(
                session, target_date, force_refresh=False, limit=20
            )
            plans = snapshot.get("plans") or []
            print(f"\n快照 trade_date={snapshot.get('trade_date')}, plans 总数={len(plans)}")
            for i, p in enumerate(plans[:10]):
                code = str(p.get("code") or "")
                name = str(p.get("name") or "")
                is_tradeable = p.get("is_tradeable")
                action = str(p.get("action") or "")
                strategy = str(p.get("strategy") or "")
                src = str(p.get("candidate_source") or "")
                priority = _next_day_plan_action_priority(p)
                strategies = [(s.get("strategy_type"), s.get("strategy_label")) for s in (p.get("strategies") or [])]
                print(f"  [{i}] {code} {name} tradeable={is_tradeable} action={action} strategy={strategy} src={src} priority={priority} strategies={strategies}")

            # 模拟盘侧: 为什么 _plan_is_direct_buy_candidate 过滤掉?
            print("\n=== 模拟盘 _next_day_plan_buy_candidates 过滤分析 ===")
            a_plan_candidates, a_plan_notes = await paper_api._next_day_plan_buy_candidates(
                session, limit=20, trade_date=target_date
            )
            print(f"候选: {len(a_plan_candidates)} 只, notes: {a_plan_notes}")

            # 检查 _plan_is_direct_buy_candidate 与 _plan_hot_sector_gate 各过滤掉什么
            plan_types = paper_api._PAPER_ACTIONABLE_PLAN_TYPES
            for p in plans[:20]:
                code = str(p.get("code") or "")
                st = paper_api._plan_strategy_types(p)
                direct = paper_api._plan_is_direct_buy_candidate(p)
                if not direct:
                    reason = "非可交易" if p.get("is_tradeable") is False else (
                        f"策略类型不在{plan_types}" if not st.intersection(plan_types) else "action_priority>3"
                    )
                    print(f"  ✗ {code} {p.get('name')}: 被过滤 {reason}")
                else:
                    sector_ok, sector_reason, _ = paper_api._plan_hot_sector_gate(p)
                    if not sector_ok:
                        print(f"  ✗ {code} {p.get('name')}: 板块闸门拒绝 -> {sector_reason}")
                    else:
                        print(f"  ✓ {code} {p.get('name')}: 可进入模拟盘候选")

            # 晋级预测侧: 与模拟盘候选池的源重叠
            from app.api.v1.promotion import promotion_candidates
            try:
                c_result = await promotion_candidates(
                    limit=12, ranked_limit=30, compact=True, force_refresh=False, db=session
                )
            except TypeError:
                c_result = await promotion_candidates(
                    session, limit=12, ranked_limit=30, compact=True
                )
            c_codes: set[str] = set()
            for key in ("ranked_first_board_candidates", "first_board_candidates", "second_board_candidates", "ranked_second_board_candidates"):
                for item in c_result.get(key) or []:
                    code = str(item.get("code") or "").strip()
                    if code:
                        c_codes.add(code)
            print(f"\n[C] 晋级预测 {len(c_codes)} 只: {sorted(c_codes)}")

            # 晋级预测的候选来自哪些源?
            print("\n=== 晋级预测候选来源分析 ===")
            seen: set[str] = set()
            for key in ("ranked_first_board_candidates", "first_board_candidates", "second_board_candidates", "ranked_second_board_candidates"):
                for item in c_result.get(key) or []:
                    code = str(item.get("code") or "").strip()
                    if code in seen:
                        continue
                    seen.add(code)
                    route = str(item.get("candidate_route") or "")
                    lane = str(item.get("strategy_lane_label") or "")
                    prob = item.get("main_probability")
                    print(f"  {code} {item.get('name')}: route={route} lane={lane} prob={prob}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
