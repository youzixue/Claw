"""十倍评分模型重建: 特征重要性分析 (2026-08-31)

用历史十倍快照的候选 + 5个维度分 + 未来N天收益, 分析每个维度的预测力.

方法:
1. 从 tenbagger-rank 快照提取所有候选(评分≥40, 样本充足)的 5维分数
2. 关联 K线算未来 10/20/30 日收益(信号日次日开盘买入)
3. 计算每个维度与未来收益的相关性(IC), 分档收益率
4. 输出: 哪些维度有预测力(正相关), 哪些无/负相关(应降权或反用)
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            # 1. 提取候选 + 5维 + 原始字段
            rows = (
                await session.execute(
                    text("SELECT payload_json, trade_date FROM dashboard_snapshot WHERE snapshot_key LIKE 'tenbagger-rank:tenbagger%' ORDER BY snapshot_time")
                )
            ).all()
            cands = []
            for row in rows:
                td = row[1]
                if not td:
                    continue
                try:
                    payload = json.loads(row[0])
                except Exception:
                    continue
                for item in payload.get("rank") or []:
                    dims = item.get("dimensions") or {}
                    score = float(item.get("total_score") or 0)
                    if score < 30:  # 保留低分作对照
                        continue
                    cands.append({
                        "code": str(item.get("code") or ""),
                        "sd": date.fromisoformat(str(td)),
                        "score": score,
                        "d_market": float(dims.get("market_cap") or 0),
                        "d_growth": float(dims.get("growth") or 0),
                        "d_value": float(dims.get("valuation") or 0),
                        "d_sector": float(dims.get("sector") or 0),
                        "d_capital": float(dims.get("capital") or 0),
                        "cap": float(item.get("circ_market_cap_billion") or 0),
                        "growth": float(item.get("net_profit_growth") or 0),
                        "pe": float(item.get("pe_ttm") or 0),
                        "chg": float(item.get("change_pct") or 0),
                    })
            print(f"候选样本(评分≥30): {len(cands)}")

            # 2. 加载K线
            codes = list(dict.fromkeys(c["code"] for c in cands))
            ph = ",".join(f":c{i}" for i in range(len(codes)))
            rows2 = (
                await session.execute(
                    text(
                        f"SELECT code, trade_date, open, close, high, low FROM stock_kline "
                        f"WHERE code IN ({ph}) ORDER BY code, trade_date"
                    ).bindparams(**{f"c{i}": c for i, c in enumerate(codes)})
                )
            ).all()
            km: dict[str, list[tuple[date, float, float, float]]] = defaultdict(list)
            for r in rows2:
                km[r.code].append((date.fromisoformat(str(r.trade_date)), float(r.open or 0), float(r.close or 0), float(r.high or 0)))
            for code in km:
                km[code].sort(key=lambda x: x[0])

            # 3. 算未来收益
            enriched = []
            for c in cands:
                kl = km.get(c["code"], [])
                idx = next((i for i, k in enumerate(kl) if k[0] > c["sd"]), None)
                if idx is None or idx + 20 >= len(kl):
                    continue
                buy = kl[idx][1] or kl[idx][2]
                if buy <= 0:
                    continue
                r10 = (kl[idx + 10][2] / buy - 1) * 100
                r20 = (kl[idx + 20][2] / buy - 1) * 100
                peak = max(k[3] for k in kl[idx:idx + 20])
                enriched.append({**c, "r10": r10, "r20": r20, "peak20": (peak / buy - 1) * 100})
            print(f"有效样本(有未来K线): {len(enriched)}")

            # 4. 特征重要性: 按维度分档看收益
            print("\n" + "=" * 80)
            print("各维度分档 vs 未来20日收益 (IC分析)")
            print("=" * 80)
            dims = [("d_market", "市值维度"), ("d_growth", "增速维度"), ("d_value", "估值维度"),
                    ("d_sector", "赛道维度"), ("d_capital", "资金维度"), ("score", "总分")]
            for key, label in dims:
                print(f"\n--- {label} ---")
                buckets = defaultdict(list)
                for c in enriched:
                    v = c.get(key, 0)
                    b = f"{int(v)//20*20}-{int(v)//20*20+19}" if key != "score" else f"{int(v)//10*10}-{int(v)//10*10+9}"
                    buckets[b].append(c["r20"])
                for b in sorted(buckets, key=lambda x: int(x.split('-')[0])):
                    vals = buckets[b]
                    avg = sum(vals) / len(vals)
                    win = sum(1 for v in vals if v > 0) / len(vals) * 100
                    print(f"  {b:>8}: 样本{len(vals):>4} 平均{avg:>7.2f}% 胜率{win:>5.1f}%")

            # 5. 简单相关性 (Spearman 近似用分档均值趋势)
            print("\n" + "=" * 80)
            print("维度相关性汇总 (分档均值从低到高的单调性)")
            print("=" * 80)
            for key, label in dims:
                buckets = defaultdict(list)
                for c in enriched:
                    v = c.get(key, 0)
                    b = int(v) // 10 * 10
                    buckets[b].append(c["r20"])
                ordered = sorted(buckets.items())
                if len(ordered) >= 3:
                    lows = [sum(v) / len(v) for _, v in ordered[:2]]
                    highs = [sum(v) / len(v) for _, v in ordered[-2:]]
                    trend = "正相关(高分更好)" if sum(highs) > sum(lows) else "负相关(高分更差)" if sum(highs) < sum(lows) else "无趋势"
                    print(f"  {label:<10}: 低档均值{sum(lows)/len(lows):>6.2f}% vs 高档均值{sum(highs)/len(highs):>6.2f}% → {trend}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
