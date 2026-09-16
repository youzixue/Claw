"""策略E重建验证: 旧规则(评分≥85全买) vs 新规则(中盘50-200亿+回踩MA20+缩量企稳)

方法:
1. 从 tenbagger-rank 快照提取评分≥85 候选
2. 对每个候选, 用信号日当时的K线判断是否满足新规则(回踩确认)
3. 分别用"旧规则买入全部"和"新规则买入通过者"计算持有收益
4. 对比两组收益/胜率, 判断重建是否有效
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


def _pullback_check(klines: list[dict], signal_idx: int, price: float) -> tuple[bool, str]:
    """用信号日及之前的K线判断回踩确认 (只能用 <= 信号日的数据, 防未来函数)."""
    hist = klines[:signal_idx + 1]
    if len(hist) < 25:
        return False, "K线不足"
    closes = [k["close"] for k in hist]
    volumes = [k["volume"] for k in hist]
    ma20 = sum(closes[-20:]) / 20
    high_60d = max(closes[-60:]) if len(closes) >= 60 else max(closes)
    ma20_dist = (price / ma20 - 1) * 100
    if ma20_dist > 3.0:
        return False, f"MA20偏离{ma20_dist:.1f}%>3%"
    if ma20_dist < -3.0:
        return False, f"深破MA20 {ma20_dist:.1f}%"
    pullback = (price / high_60d - 1) * 100
    if pullback > -5.0:
        return False, f"未回调(距高{abs(pullback):.1f}%)"
    if pullback < -20.0:
        return False, f"破位(回撤{abs(pullback):.1f}%)"
    recent_vol = sum(volumes[-5:]) / 5
    prev_vol = sum(volumes[-25:-5]) / 20
    vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 999
    if vol_ratio > 0.8:
        return False, f"未缩量({vol_ratio:.2f})"
    return True, f"回踩OK MA20{ma20_dist:.1f}% 回撤{abs(pullback):.1f}% 量比{vol_ratio:.2f}"


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            # 1. 加载十倍快照候选(评分≥85)
            rows = (
                await session.execute(
                    text("SELECT payload_json, trade_date FROM dashboard_snapshot WHERE snapshot_key LIKE 'tenbagger-rank:tenbagger%'")
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
                    score = float(item.get("total_score") or 0)
                    if score >= 85:
                        cands.append({
                            "code": str(item.get("code") or ""),
                            "name": str(item.get("name") or ""),
                            "sd": date.fromisoformat(str(td)),
                            "score": score,
                            "cap": float(item.get("circ_market_cap_billion") or 0),
                        })
            print(f"评分≥85候选: {len(cands)}")

            # 2. 加载K线
            codes = list(dict.fromkeys(c["code"] for c in cands))
            ph = ",".join(f":c{i}" for i in range(len(codes)))
            rows2 = (
                await session.execute(
                    text(
                        f"SELECT code, trade_date, open, close, high, low, volume FROM stock_kline "
                        f"WHERE code IN ({ph}) ORDER BY code, trade_date"
                    ).bindparams(**{f"c{i}": c for i, c in enumerate(codes)})
                )
            ).all()
            km: dict[str, list[dict]] = defaultdict(list)
            for r in rows2:
                km[r.code].append({
                    "date": date.fromisoformat(str(r.trade_date)),
                    "open": float(r.open or 0), "close": float(r.close or 0),
                    "high": float(r.high or 0), "low": float(r.low or 0),
                    "volume": float(r.volume or 0),
                })

            # 3. 逐候选: 多组规则对比
            configs = {
                "旧规则(全买)": {"cap_min": 0, "cap_max": 99999, "ma20_max": 999, "shr": 999, "pull_min": -999, "pull_max": 0},
                "仅中盘50-200亿": {"cap_min": 50, "cap_max": 200, "ma20_max": 999, "shr": 999, "pull_min": -999, "pull_max": 0},
                "仅回踩(MA20≤3%+缩量)": {"cap_min": 0, "cap_max": 99999, "ma20_max": 3.0, "shr": 0.8, "pull_min": -999, "pull_max": 0},
                "中盘+回踩(完整)": {"cap_min": 50, "cap_max": 200, "ma20_max": 3.0, "shr": 0.8, "pull_min": 5.0, "pull_max": 20.0},
                "中盘+回踩(放宽MA20≤5%)": {"cap_min": 50, "cap_max": 200, "ma20_max": 5.0, "shr": 0.8, "pull_min": 5.0, "pull_max": 20.0},
                "中盘+回踩(不要求缩量)": {"cap_min": 50, "cap_max": 200, "ma20_max": 5.0, "shr": 999, "pull_min": 5.0, "pull_max": 20.0},
            }
            results = {}
            for cfg_name, cfg in configs.items():
                gains, wins = [], 0
                for c in cands:
                    kl = km.get(c["code"], [])
                    idx = next((i for i, k in enumerate(kl) if k["date"] > c["sd"]), None)
                    if idx is None or idx + 20 >= len(kl):
                        continue
                    buy_price = kl[idx]["open"] or kl[idx]["close"]
                    if buy_price <= 0:
                        continue
                    cap = c["cap"]
                    if cap < cfg["cap_min"] or cap > cfg["cap_max"]:
                        continue
                    if cfg["ma20_max"] < 900:
                        signal_close = kl[idx - 1]["close"] if idx >= 1 else buy_price
                        hist = kl[:idx]
                        if len(hist) < 25:
                            continue
                        closes = [k["close"] for k in hist]
                        ma20 = sum(closes[-20:]) / 20
                        ma20_dist = (signal_close / ma20 - 1) * 100
                        if abs(ma20_dist) > cfg["ma20_max"]:
                            continue
                        if cfg["shr"] < 900:
                            volumes = [k["volume"] for k in hist]
                            recent_vol = sum(volumes[-5:]) / 5
                            prev_vol = sum(volumes[-25:-5]) / 20
                            vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 999
                            if vol_ratio > cfg["shr"]:
                                continue
                        if cfg["pull_min"] > -900:
                            high_60d = max(closes[-60:]) if len(closes) >= 60 else max(closes)
                            pullback = (signal_close / high_60d - 1) * 100
                            if pullback > -cfg["pull_min"] or pullback < -cfg["pull_max"]:
                                continue
                    sell_price = kl[idx + 20]["close"]
                    ret = (sell_price / buy_price - 1) * 100
                    gains.append(ret)
                    if ret > 0:
                        wins += 1
                results[cfg_name] = (gains, wins)

            def stats(gains, wins):
                if not gains:
                    return "无样本"
                return (f"样本{len(gains)} 平均{sum(gains)/len(gains):.2f}% "
                        f"胜率{wins/len(gains)*100:.1f}% 最大盈{max(gains):.1f}% 最大亏{min(gains):.1f}%")

            print("\n=== 20日持有收益对比 (多组规则) ===")
            for cfg_name, (gains, wins) in results.items():
                print(f"{cfg_name:<22}: {stats(gains, wins)}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
