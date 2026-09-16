"""策略E 信号质量诊断 (2026-08-31)

目标: 判断十倍潜力策略亏损的根因 — 买入时机 / 评分维度 / 行情风格.

方法:
1. 从 tenbagger-rank 快照提取评分≥85的候选(信号)
2. 关联 K线, 计算信号日次日开盘买入后的 3/5/10/20日收益
3. 按 评分档位/市值档位/时间月份 三个维度拆解收益
4. 对比同期市场基准(用全市场K线均值)做风格归因
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings


async def _load_candidates(session) -> list[dict]:
    rows = (
        await session.execute(
            text("SELECT payload_json, trade_date FROM dashboard_snapshot WHERE snapshot_key LIKE 'tenbagger-rank:tenbagger%'")
        )
    ).all()
    cands: list[dict] = []
    for row in rows:
        trade_date = row[1]
        if not trade_date:
            continue
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        for item in payload.get("rank") or []:
            score = float(item.get("total_score") or 0)
            if score >= 80:  # 覆盖 80-95 全档位做诊断
                cands.append({
                    "code": str(item.get("code") or ""),
                    "name": str(item.get("name") or item.get("code") or ""),
                    "signal_date": date.fromisoformat(str(trade_date)),
                    "score": score,
                    "level": str(item.get("level") or ""),
                    "circ_market_cap": float(item.get("circ_market_cap_billion") or 0),
                    "net_profit_growth": float(item.get("net_profit_growth") or 0),
                    "pe_ttm": float(item.get("pe_ttm") or 0),
                })
    return cands


async def _load_kline_map(session, codes: list[str], start: date) -> dict[str, list[dict]]:
    if not codes:
        return {}
    import sqlalchemy
    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    rows = (
        await session.execute(
            text(
                f"SELECT code, trade_date, open, close, high, low, change_pct FROM stock_kline "
                f"WHERE code IN ({placeholders}) AND trade_date >= :start ORDER BY code, trade_date"
            ).bindparams(**{f"c{i}": c for i, c in enumerate(codes)}, start=start)
        )
    ).all()
    kline_map: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        kline_map[r.code].append({
            "date": date.fromisoformat(str(r.trade_date)),
            "open": float(r.open or 0), "close": float(r.close or 0),
            "high": float(r.high or 0), "low": float(r.low or 0),
            "change_pct": float(r.change_pct or 0),
        })
    return dict(kline_map)


def _forward_returns(klines: list[dict], signal_date: date, horizons: list[int]) -> dict:
    """信号日之后第 N 个交易日的收益 (次日开盘买入)."""
    # 找信号日后第一个交易日
    idx = None
    for i, k in enumerate(klines):
        if k["date"] > signal_date:
            idx = i
            break
    if idx is None:
        return {}
    buy_price = klines[idx]["open"] or klines[idx]["close"]
    if buy_price <= 0:
        return {}
    out = {}
    for h in horizons:
        target = idx + h
        if target < len(klines):
            sell = klines[target]["close"]
            out[f"r{h}d"] = round((sell / buy_price - 1) * 100, 2)
        else:
            out[f"r{h}d"] = None
    # 持有期最大浮盈/浮亏
    highs = [k["high"] for k in klines[idx:idx + 20]]
    lows = [k["low"] for k in klines[idx:idx + 20]]
    if highs and lows:
        out["max20d"] = round((max(highs) / buy_price - 1) * 100, 2)
        out["min20d"] = round((min(lows) / buy_price - 1) * 100, 2)
    return out


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            cands = await _load_candidates(session)
            print(f"十倍候选(评分≥80): {len(cands)} 条")
            if not cands:
                return
            codes = list(dict.fromkeys(c["code"] for c in cands))
            min_date = min(c["signal_date"] for c in cands) - timedelta(days=10)
            kline_map = await _load_kline_map(session, codes, min_date)

            # 逐候选算收益
            enriched = []
            for c in cands:
                klines = kline_map.get(c["code"], [])
                if not klines:
                    continue
                rets = _forward_returns(klines, c["signal_date"], [1, 3, 5, 10, 20])
                enriched.append({**c, **rets})

            print(f"有效样本(有K线): {len(enriched)}")

            # ===== 维度1: 评分档位 =====
            print("\n=== 维度1: 评分档位 ===")
            _bucket(enriched, lambda c: f"评分≥95" if c["score"] >= 95 else (f"90-95" if c["score"] >= 90 else (f"85-90" if c["score"] >= 85 else "80-85")))

            # ===== 维度2: 市值档位 =====
            print("\n=== 维度2: 市值档位(亿) ===")
            _bucket(enriched, lambda c: "小盘<50亿" if c["circ_market_cap"] < 50 else ("中盘50-200亿" if c["circ_market_cap"] < 200 else "大盘≥200亿"))

            # ===== 维度3: 时间月份 =====
            print("\n=== 维度3: 信号月份 ===")
            _bucket(enriched, lambda c: c["signal_date"].strftime("%Y-%m"))

            # ===== 维度4: 整体 =====
            print("\n=== 整体 ===")
            _bucket(enriched, lambda c: "全部")

            # 与市场基准对比: 同期全市场平均5日收益
            await _market_benchmark(session, enriched)
    finally:
        await engine.dispose()


def _bucket(enriched: list[dict], key_fn) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in enriched:
        groups[key_fn(c)].append(c)
    print(f"{'分组':<16}{'样本':>6}{'r3d均%':>9}{'r5d均%':>9}{'r10d均%':>10}{'r20d均%':>10}{'胜率r5d%':>10}{'max20d%':>9}{'min20d%':>9}")
    print("-" * 95)
    for g, items in sorted(groups.items()):
        n = len(items)
        def avg(key):
            vals = [i.get(key) for i in items if i.get(key) is not None]
            return round(sum(vals) / len(vals), 2) if vals else 0
        win5 = sum(1 for i in items if (i.get("r5d") or 0) > 0)
        print(
            f"{g:<16}{n:>6}{avg('r3d'):>9}{avg('r5d'):>9}{avg('r10d'):>10}"
            f"{avg('r20d'):>10}{round(win5 / n * 100, 1):>10}{avg('max20d'):>9}{avg('min20d'):>9}"
        )


async def _market_benchmark(session, enriched: list[dict]) -> None:
    """同期全市场 5日收益基准 (风格归因)."""
    dates = sorted({c["signal_date"] for c in enriched})
    if not dates:
        return
    start = dates[0] - timedelta(days=5)
    end = dates[-1] + timedelta(days=30)
    rows = (
        await session.execute(
            text("SELECT code, trade_date, close FROM stock_kline WHERE trade_date BETWEEN :s AND :e")
            .bindparams(s=start, e=end)
        )
    ).all()
    by_date: dict[date, dict[str, float]] = defaultdict(dict)
    for r in rows:
        by_date[date.fromisoformat(str(r.trade_date))][r.code] = float(r.close or 0)
    all_dates = sorted(by_date.keys())
    print(f"\n=== 市场基准 (信号日次日开盘买入持有5日, 全市场均值) ===")
    for d in dates:
        idx = None
        for i, ad in enumerate(all_dates):
            if ad > d:
                idx = i
                break
        if idx is None or idx + 5 >= len(all_dates):
            continue
        buy_day = all_dates[idx]
        sell_day = all_dates[idx + 5]
        gains = []
        for code, price in by_date[buy_day].items():
            sell = by_date[sell_day].get(code)
            if price and price > 0 and sell:
                gains.append((sell / price - 1) * 100)
        if gains:
            avg = round(sum(gains) / len(gains), 2)
            print(f"  {d}: 全市场5日均值 {avg}%")


if __name__ == "__main__":
    asyncio.run(main())
