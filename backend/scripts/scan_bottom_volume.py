"""
底部放量扫描 — 双档形态识别 (2026-08-26)

【严格档】经典底部反转（吸筹后启动初期）:
  - 近20日跌幅 > 10%
  - 今日成交额 / 5日均 ≥ 1.5
  - 今日涨幅 0% ~ 5%
  - 量比 > 1.5

【宽松档】超跌反弹（覆盖更宽形态）:
  - 近20日跌幅 > 5%
  - 今日成交额 / 5日均 ≥ 1.5
  - 今日涨幅 0% ~ 7%
  - 量比 > 1.5

【交易范围】遵循 AGENTS.md 规则:
  - 主板可交易: board_type IN ('main_sh','main_sz')
  - 排除 ST/停牌/退市/次新股 (is_ipo_recent=1)
  - 排除创业板/科创板/北交所

【输出】
  - 终端表格 (Top 30)
  - outputs/historical_low_bottom_volume_YYYYMMDD.csv (严格档, 全量)
  - outputs/historical_low_bottom_volume_relaxed_YYYYMMDD.csv (宽松档, 全量)
  - outputs/historical_low_bottom_volume_observe_YYYYMMDD.csv (观察池, 涨5-7%)

【评分】
  score = vol_ratio * (1 + drop_pct / 20)
  兼顾放量倍数和底部深度, 避免单一极端

复跑: cd backend && python3 scripts/scan_bottom_volume.py
"""
from __future__ import annotations

import asyncio
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.session import async_session  # noqa: E402


# ---------- 双档参数 ----------
STRICT = {
    "drop_min": 10.0,
    "change_max": 5.0,
    "suffix": "",  # 默认严格档
}
RELAXED = {
    "drop_min": 5.0,
    "change_max": 7.0,
    "suffix": "_relaxed",
}
COMMON = {
    "lookback_days": 20,
    "vol_ratio_min": 1.5,
    "spot_vol_ratio_min": 1.5,
    "change_min": 0.0,
    "top_n_print": 30,
}


def _parse_date(v):
    if isinstance(v, str):
        return datetime.strptime(v, "%Y-%m-%d").date()
    if isinstance(v, datetime):
        return v.date()
    return v


async def fetch_kline_window(session, lookback_start: date, today: date) -> list[dict]:
    rows = (await session.execute(
        text(
            "SELECT code, trade_date, close, high, low, amount "
            "FROM stock_kline WHERE trade_date BETWEEN :s AND :e "
            "ORDER BY code, trade_date"
        ),
        {"s": lookback_start, "e": today},
    )).all()
    out = []
    for r in rows:
        out.append({
            "code": r[0],
            "trade_date": _parse_date(r[1]),
            "close": float(r[2]) if r[2] is not None else None,
            "high": float(r[3]) if r[3] is not None else None,
            "low": float(r[4]) if r[4] is not None else None,
            "amount": float(r[5]) if r[5] is not None else None,
        })
    return out


async def fetch_spot(session) -> dict[str, dict]:
    rows = (await session.execute(
        text(
            "SELECT code, name, price, change_pct, volume_ratio, turnover, "
            "       amount, main_net_inflow, limit_up, limit_down, high, low "
            "FROM stock_spot"
        )
    )).all()
    out = {}
    for r in rows:
        code = r[0]
        price = float(r[2]) if r[2] is not None else None
        change_pct = float(r[3]) if r[3] is not None else None
        limit_up = float(r[8]) if r[8] is not None else None
        limit_down = float(r[9]) if r[9] is not None else None
        # 涨跌停判定: 价格贴近涨跌停价 或 涨幅>=9.5%(主板)/19.5%(创科)
        is_limit_up = False
        if price is not None and limit_up is not None and limit_up > 0:
            if abs(price - limit_up) / limit_up * 100 < 0.3:
                is_limit_up = True
            elif change_pct is not None and change_pct >= 9.4:
                is_limit_up = True
        is_limit_down = False
        if price is not None and limit_down is not None and limit_down > 0:
            if abs(price - limit_down) / limit_down * 100 < 0.3:
                is_limit_down = True
            elif change_pct is not None and change_pct <= -9.4:
                is_limit_down = True
        out[code] = {
            "code": code, "name": r[1],
            "price": price, "change_pct": change_pct,
            "volume_ratio": float(r[4]) if r[4] is not None else None,
            "turnover": float(r[5]) if r[5] is not None else None,
            "amount": float(r[6]) if r[6] is not None else None,
            "main_net_inflow": float(r[7]) if r[7] is not None else None,
            "is_limit_up": is_limit_up, "is_limit_down": is_limit_down,
            "limit_up": limit_up, "limit_down": limit_down,
            "high": float(r[10]) if r[10] is not None else None,
            "low": float(r[11]) if r[11] is not None else None,
        }
    return out


async def fetch_tags(session) -> dict[str, dict]:
    rows = (await session.execute(
        text(
            "SELECT code, board_type, is_st, is_suspended, is_delisting, is_ipo_recent "
            "FROM stock_tags"
        )
    )).all()
    out = {}
    for r in rows:
        out[r[0]] = {
            "code": r[0], "board_type": r[1],
            "is_st": bool(r[2]) if r[2] is not None else False,
            "is_suspended": bool(r[3]) if r[3] is not None else False,
            "is_delisting": bool(r[4]) if r[4] is not None else False,
            "is_ipo_recent": bool(r[5]) if r[5] is not None else False,
        }
    return out


def is_tradeable(tag: dict | None) -> bool:
    """主板可交易: 排除创业板/科创板/北交所/ST/停牌/退市/次新"""
    if tag is None:
        return False
    if tag["board_type"] not in ("main_sh", "main_sz"):
        return False
    if tag["is_st"] or tag["is_suspended"] or tag["is_delisting"] or tag["is_ipo_recent"]:
        return False
    return True


def group_kline_by_code(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["code"], []).append(r)
    for code in out:
        out[code].sort(key=lambda x: x["trade_date"])
    return out


def scan_one(code: str, klines: list[dict], spot: dict, profile: dict) -> dict | None:
    """单只股票: 计算底部+放量, 返回命中 dict 或 None"""
    if len(klines) < 6:
        return None
    last = klines[-1]
    prev5 = klines[-6:-1]
    today_amount = last["amount"]
    if today_amount is None or today_amount <= 0:
        return None
    prev5_amounts = [k["amount"] for k in prev5 if k["amount"] is not None]
    if len(prev5_amounts) < 3:
        return None
    avg5_amount = sum(prev5_amounts) / len(prev5_amounts)
    if avg5_amount <= 0:
        return None
    vol_ratio_kline = today_amount / avg5_amount

    window = klines[-COMMON["lookback_days"]:] if len(klines) >= COMMON["lookback_days"] else klines
    highs = [k["high"] for k in window if k["high"] is not None]
    if not highs:
        return None
    period_high = max(highs)
    current_close = last["close"]
    if current_close is None or period_high <= 0:
        return None
    drop_pct = (period_high - current_close) / period_high * 100

    prev_close = klines[-2]["close"] if len(klines) >= 2 else None
    if prev_close is None or prev_close <= 0:
        return None
    today_change_pct = (current_close - prev_close) / prev_close * 100

    if drop_pct < profile["drop_min"]:
        return None
    if vol_ratio_kline < COMMON["vol_ratio_min"]:
        return None
    if today_change_pct < COMMON["change_min"] or today_change_pct > profile["change_max"]:
        return None
    if spot is None:
        return None
    if spot["change_pct"] is not None and (
        spot["change_pct"] < COMMON["change_min"] or spot["change_pct"] > profile["change_max"]
    ):
        return None
    if spot["volume_ratio"] is None or spot["volume_ratio"] < COMMON["spot_vol_ratio_min"]:
        return None

    score = vol_ratio_kline * (1 + drop_pct / 20.0)

    return {
        "code": code,
        "name": spot["name"],
        "price": spot["price"],
        "change_pct_kline": round(today_change_pct, 2),
        "change_pct_spot": round(spot["change_pct"], 2) if spot["change_pct"] is not None else None,
        "vol_ratio_kline": round(vol_ratio_kline, 2),
        "vol_ratio_spot": round(spot["volume_ratio"], 2),
        "drop_pct_20d": round(drop_pct, 2),
        "period_high": round(period_high, 2),
        "today_amount_yi": round(today_amount / 1e8, 2),
        "avg5_amount_yi": round(avg5_amount / 1e8, 2),
        "turnover": round(spot["turnover"], 2) if spot["turnover"] is not None else None,
        "main_net_inflow_wan": round(spot["main_net_inflow"] / 1e4, 2) if spot["main_net_inflow"] is not None else None,
        "is_limit_up": spot["is_limit_up"],
        "score": round(score, 2),
    }


def print_table(rows: list[dict], today: date, profile_name: str, profile: dict):
    print(f"\n{'=' * 130}")
    print(
        f"【{profile_name}】{today}  "
        f"条件: 20日跌幅>{profile['drop_min']}% + 量/5日均≥{COMMON['vol_ratio_min']} + "
        f"涨幅{COMMON['change_min']}~{profile['change_max']}% + 量比>{COMMON['spot_vol_ratio_min']}"
    )
    print(f"{'=' * 130}")
    if not rows:
        print("⚠️ 无命中标的")
        return
    print(
        f"{'排名':<5}{'代码':<8}{'名称':<12}{'现价':>7}{'涨幅K%':>8}{'涨幅S%':>8}"
        f"{'量比K':>7}{'量比S':>7}{'20日跌幅%':>10}{'今成交亿':>10}{'5日均亿':>10}"
        f"{'换手%':>7}{'主力流入万':>10}{'涨停':>5}{'评分':>7}"
    )
    print("-" * 130)
    for i, r in enumerate(rows[:COMMON["top_n_print"]], 1):
        print(
            f"{i:<5}{r['code']:<8}{r['name']:<12}"
            f"{r['price']:>7.2f}{r['change_pct_kline']:>8.2f}{r['change_pct_spot']:>8.2f}"
            f"{r['vol_ratio_kline']:>7.2f}{r['vol_ratio_spot']:>7.2f}"
            f"{r['drop_pct_20d']:>10.2f}"
            f"{r['today_amount_yi']:>10.2f}{r['avg5_amount_yi']:>10.2f}"
            f"{r['turnover']:>7.2f}{r['main_net_inflow_wan']:>10.2f}"
            f"{'✓' if r['is_limit_up'] else '':>5}{r['score']:>7.2f}"
        )
    print(f"\n共 {len(rows)} 只命中, 仅显示前 {min(COMMON['top_n_print'], len(rows))} 只")


def save_csv(rows: list[dict], path: Path):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def scan_with_profile(grouped, tradeable_codes, spot_map, profile) -> list[dict]:
    out = []
    for code, klines_code in grouped.items():
        if code not in tradeable_codes or code not in spot_map:
            continue
        result = scan_one(code, klines_code, spot_map[code], profile)
        if result is not None:
            out.append(result)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def scan_observe(grouped, tradeable_codes, spot_map) -> list[dict]:
    """观察池: 涨幅 5-7% 的, 错过买点但值得跟踪"""
    out = []
    for code, klines_code in grouped.items():
        if code not in tradeable_codes or code not in spot_map or len(klines_code) < 6:
            continue
        last = klines_code[-1]
        today_amount = last["amount"]
        prev5 = klines_code[-6:-1]
        prev5_amts = [k["amount"] for k in prev5 if k["amount"] is not None]
        if today_amount is None or len(prev5_amts) < 3:
            continue
        avg5 = sum(prev5_amts) / len(prev5_amts)
        if avg5 <= 0:
            continue
        vr = today_amount / avg5
        window = klines_code[-COMMON["lookback_days"]:] if len(klines_code) >= COMMON["lookback_days"] else klines_code
        highs = [k["high"] for k in window if k["high"] is not None]
        if not highs or last["close"] is None:
            continue
        period_high = max(highs)
        drop_pct = (period_high - last["close"]) / period_high * 100
        prev_close = klines_code[-2]["close"] if len(klines_code) >= 2 else None
        if prev_close is None or prev_close <= 0:
            continue
        chg = (last["close"] - prev_close) / prev_close * 100
        sp = spot_map[code]
        if (
            drop_pct >= RELAXED["drop_min"]
            and vr >= COMMON["vol_ratio_min"]
            and 5.0 < chg <= 7.0
            and sp["volume_ratio"] is not None
            and sp["volume_ratio"] >= COMMON["spot_vol_ratio_min"]
        ):
            out.append({
                "code": code, "name": sp["name"], "price": sp["price"],
                "change_pct": round(chg, 2),
                "vol_ratio_kline": round(vr, 2),
                "vol_ratio_spot": round(sp["volume_ratio"], 2),
                "drop_pct_20d": round(drop_pct, 2),
                "turnover": round(sp["turnover"], 2) if sp["turnover"] is not None else None,
                "reason": "已涨5-7%, 错过最佳买点, 列入跟踪",
            })
    out.sort(key=lambda x: x["drop_pct_20d"] * x["vol_ratio_kline"], reverse=True)
    return out


async def main():
    print("🔍 底部放量扫描 (双档) 启动 ...")

    async with async_session() as session:
        latest_raw = (await session.execute(text("SELECT MAX(trade_date) FROM stock_kline"))).first()[0]
        today = _parse_date(latest_raw)
        lookback_start = today - timedelta(days=COMMON["lookback_days"] + 10)
        print(f"📅 最新交易日: {today}, 回看 {COMMON['lookback_days']} 日")

        print("📥 拉取数据 ...")
        klines = await fetch_kline_window(session, lookback_start, today)
        spot_map = await fetch_spot(session)
        tags = await fetch_tags(session)
        tradeable_codes = {c for c, t in tags.items() if is_tradeable(t)}
        grouped = group_kline_by_code(klines)
        print(f"   K线 {len(klines)} 行 | Spot {len(spot_map)} 只 | Tags {len(tags)} 只 | 主板可交易 {len(tradeable_codes)} 只")

        # 严格档
        strict_rows = scan_with_profile(grouped, tradeable_codes, spot_map, STRICT)
        print_table(strict_rows, today, "严格档：经典底部反转", STRICT)

        # 宽松档
        relaxed_rows = scan_with_profile(grouped, tradeable_codes, spot_map, RELAXED)
        print_table(relaxed_rows, today, "宽松档：超跌反弹", RELAXED)

        # 观察池
        observe = scan_observe(grouped, tradeable_codes, spot_map)

        # 输出
        out_dir = Path(__file__).resolve().parent.parent.parent / "outputs"
        out_dir.mkdir(exist_ok=True)
        date_str = today.strftime("%Y%m%d")

        p_strict = out_dir / f"historical_low_bottom_volume_{date_str}.csv"
        save_csv(strict_rows, p_strict)
        print(f"\n💾 严格档: {p_strict} ({len(strict_rows)} 行)")

        p_relaxed = out_dir / f"historical_low_bottom_volume_relaxed_{date_str}.csv"
        save_csv(relaxed_rows, p_relaxed)
        print(f"💾 宽松档: {p_relaxed} ({len(relaxed_rows)} 行)")

        p_obs = out_dir / f"historical_low_bottom_volume_observe_{date_str}.csv"
        save_csv(observe, p_obs)
        print(f"👀 观察池: {p_obs} ({len(observe)} 行)")

        print(
            f"\n📊 统计: 主板可交易 {len(tradeable_codes)} 只 → "
            f"严格 {len(strict_rows)} | 宽松 {len(relaxed_rows)} | 观察 {len(observe)}"
        )
        if strict_rows:
            t3 = strict_rows[:3]
            print("🏆 严格档 Top 3: " + " | ".join(
                f"{r['code']} {r['name']} ({r['score']})" for r in t3
            ))


if __name__ == "__main__":
    asyncio.run(main())
