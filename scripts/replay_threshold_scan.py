"""轨道2: walk-forward 参数寻优 (2026-08-31)

对策略B/C/E 做阈值敏感性扫描:
- 用不同 min_probability (B/C) / min_score (E) 重新回放
- 观察胜率/盈亏比/总盈亏随阈值的变化, 找出最优阈值
- 遵循"先验证后上线": 只输出建议, 不直接改 settings

用法:
    python scripts/replay_threshold_scan.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from replay_five_strategies import (
    replay_strategy,
)


async def scan_threshold(session, key: str, thresholds: list[float], attr: str) -> list[dict]:
    results = []
    for th in thresholds:
        r = await replay_strategy(session, key, config_override={attr: th})
        st = r.get("stats") or {}
        results.append({
            "threshold": th,
            "signals": r.get("signals", 0),
            "executed": r.get("executed", 0),
            "win_rate": st.get("win_rate", 0),
            "avg_pct": st.get("avg_profit_pct", 0),
            "payoff": st.get("profit_loss_ratio", 0),
            "total_pnl": st.get("total_pnl", 0),
            "max_loss": st.get("max_single_loss", 0),
            "avg_hold": st.get("avg_hold_days", 0),
            "note": r.get("error") or "",
        })
    return results


async def scan_risk_params(session, key: str, tp_values: list[float], sl_values: list[float]) -> list[dict]:
    """扫描止盈/止损参数组合 (walk-forward 参数寻优核心)."""
    results = []
    for tp in tp_values:
        for sl in sl_values:
            if sl >= tp:
                continue  # 止损必须小于止盈
            r = await replay_strategy(
                session, key,
                config_override={"take_profit_pct": tp, "stop_loss_pct": sl},
            )
            st = r.get("stats") or {}
            results.append({
                "tp": tp, "sl": sl,
                "signals": r.get("signals", 0),
                "executed": r.get("executed", 0),
                "win_rate": st.get("win_rate", 0),
                "avg_pct": st.get("avg_profit_pct", 0),
                "payoff": st.get("profit_loss_ratio", 0),
                "total_pnl": st.get("total_pnl", 0),
                "max_loss": st.get("max_single_loss", 0),
                "avg_hold": st.get("avg_hold_days", 0),
            })
    return results


def _print_risk_rows(rows: list[dict]) -> None:
    header = f"{'止盈':>5}{'止损':>6}{'信号':>6}{'成交':>6}{'胜率%':>8}{'平均%':>8}{'盈亏比':>8}{'总盈亏':>10}{'最大单亏':>10}{'均持仓':>8}"
    print(header)
    print("-" * 100)
    for r in sorted(rows, key=lambda x: x["total_pnl"], reverse=True):
        print(
            f"{r['tp']:>5.1f}{r['sl']:>6.1f}{r['signals']:>6}{r['executed']:>6}"
            f"{r['win_rate']:>8}{r['avg_pct']:>8}{r['payoff']:>8}{r['total_pnl']:>10.2f}"
            f"{r['max_loss']:>10.2f}{r['avg_hold']:>8}"
        )


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            print("=" * 100)
            print("轨道2: walk-forward 参数寻优 (阈值 + 止盈止损)")
            print("=" * 100)

            print("\n【策略B · 晋级二板】 min_probability 扫描 (当前 0.25):")
            b = await scan_threshold(session, "B", [0.25, 0.30, 0.35, 0.40, 0.45, 0.50], "min_probability")
            _print_rows(b)

            print("\n【策略C · 主线扩散】 min_probability 扫描 (当前 0.20):")
            c = await scan_threshold(session, "C", [0.20, 0.25, 0.30, 0.35, 0.40, 0.45], "min_probability")
            _print_rows(c)

            print("\n【策略B · 晋级二板】 止盈×止损 组合扫描 (当前 8%/6%):")
            b_r = await scan_risk_params(session, "B", [6.0, 8.0, 10.0, 12.0], [4.0, 5.0, 6.0, 8.0])
            _print_risk_rows(b_r)

            print("\n【策略C · 主线扩散】 止盈×止损 组合扫描 (当前 4.5%/3.5%):")
            c_r = await scan_risk_params(session, "C", [3.5, 4.5, 6.0, 8.0], [2.5, 3.5, 5.0])
            _print_risk_rows(c_r)

            print("\n【策略E · 十倍潜力】 止盈×止损 组合扫描 (当前 15%/8%):")
            e_r = await scan_risk_params(session, "E", [10.0, 15.0, 20.0, 25.0], [6.0, 8.0, 10.0, 12.0])
            _print_risk_rows(e_r)
    finally:
        await engine.dispose()


def _print_rows(rows: list[dict]) -> None:
    header = f"{'阈值':>6}{'信号':>6}{'成交':>6}{'胜率%':>8}{'平均%':>8}{'盈亏比':>8}{'总盈亏':>10}{'最大单亏':>10}{'均持仓':>8}"
    print(header)
    print("-" * 100)
    for r in rows:
        print(
            f"{r['threshold']:>6.2f}{r['signals']:>6}{r['executed']:>6}"
            f"{r['win_rate']:>8}{r['avg_pct']:>8}{r['payoff']:>8}{r['total_pnl']:>10.2f}"
            f"{r['max_loss']:>10.2f}{r['avg_hold']:>8}"
        )


if __name__ == "__main__":
    asyncio.run(main())
