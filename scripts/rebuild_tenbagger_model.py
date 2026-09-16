"""十倍评分模型重建验证: 新旧模型收益对比 (2026-08-31)

基于特征重要性分析:
- 旧模型: 市值/增速/估值 高分好 → 实际负相关; 总分与未来收益负相关 → E回放全线亏损
- 新模型假设: 
  维度1: 资金(正相关, 高分好)
  维度2: 赛道(弱正相关, 60-79档最好)
  维度3: 反市值(小市值反而差 → 给大市值加分)
  维度4: 反增速(超高增速=已涨过 → 给低增速加分)
  维度5: 当日不追高(涨幅适中/回调买入)

用真实K线数据做网格搜索, 找"评分高 → 未来20日收益正期望"的组合.
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
            # 1. 提取候选原始字段 (不限评分, 保留全量作对照)
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
                    cands.append({
                        "code": str(item.get("code") or ""),
                        "sd": date.fromisoformat(str(td)),
                        "score": float(item.get("total_score") or 0),
                        "d_capital": float(dims.get("capital") or 0),
                        "d_sector": float(dims.get("sector") or 0),
                        "d_market": float(dims.get("market_cap") or 0),
                        "d_growth": float(dims.get("growth") or 0),
                        "cap": float(item.get("circ_market_cap_billion") or 0),
                        "growth": float(item.get("net_profit_growth") or 0),
                        "pe": float(item.get("pe_ttm") or 0),
                        "chg": float(item.get("change_pct") or 0),
                    })
            print(f"全量候选: {len(cands)}")

            # 2. 加载K线(含涨幅)
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

            # 3. 算未来收益 + 信号日前后特征
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
                # 信号日前5日涨幅 (近期已涨多少)
                prev5 = (kl[idx][2] / kl[idx - 5][2] - 1) * 100 if idx >= 5 else 0
                # 信号日前20日涨幅 (中期涨幅)
                prev20 = (kl[idx][2] / kl[idx - 20][2] - 1) * 100 if idx >= 20 else 0
                enriched.append({**c, "r10": r10, "r20": r20, "prev5": prev5, "prev20": prev20})
            print(f"有效样本: {len(enriched)}")

            # 4. 新模型: 用真实字段做条件组合网格
            print("\n=== 新模型候选条件组合 (按 r20 正期望排序) ===")
            configs = {
                "基线: 全量": lambda c: True,
                "资金维度≥60": lambda c: c["d_capital"] >= 60,
                "赛道维度≥80": lambda c: c["d_sector"] >= 80,
                "前5日回调(>-3%)": lambda c: c["prev5"] > -3,
                "前5日温和(0~10%)": lambda c: 0 < c["prev5"] <= 10,
                "前20日回踩(-15~0%)": lambda c: -15 <= c["prev20"] <= 0,
                "市值50-200亿": lambda c: 50 <= c["cap"] <= 200,
                "PE>0且<100": lambda c: c["pe"] > 0 and c["pe"] < 100,
                "资金≥60+前5日温和": lambda c: c["d_capital"] >= 60 and 0 < c["prev5"] <= 10,
                "资金≥60+前20回踩": lambda c: c["d_capital"] >= 60 and -15 <= c["prev20"] <= 0,
                "赛道≥80+资金≥60": lambda c: c["d_sector"] >= 80 and c["d_capital"] >= 60,
                "赛道≥80+资金≥60+前20回踩": lambda c: c["d_sector"] >= 80 and c["d_capital"] >= 60 and -15 <= c["prev20"] <= 0,
                "中盘+资金≥60+回踩": lambda c: 50 <= c["cap"] <= 200 and c["d_capital"] >= 60 and -15 <= c["prev20"] <= 0,
                "中盘+赛道≥80+资金≥60+回踩": lambda c: 50 <= c["cap"] <= 200 and c["d_sector"] >= 80 and c["d_capital"] >= 60 and -15 <= c["prev20"] <= 0,
            }
            print(f"{'组合':<36}{'样本':>6}{'r10均%':>9}{'r20均%':>9}{'胜率r20%':>10}")
            print("-" * 75)
            results = []
            for name, filt in configs.items():
                subs = [c for c in enriched if filt(c)]
                if not subs:
                    continue
                r10 = sum(c["r10"] for c in subs) / len(subs)
                r20 = sum(c["r20"] for c in subs) / len(subs)
                win = sum(1 for c in subs if c["r20"] > 0) / len(subs) * 100
                results.append((name, len(subs), r10, r20, win))
            for name, n, r10, r20, win in sorted(results, key=lambda x: -x[3]):
                print(f"{name:<36}{n:>6}{r10:>9.2f}{r20:>9.2f}{win:>10.1f}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
