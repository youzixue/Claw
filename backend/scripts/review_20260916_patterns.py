#!/usr/bin/env python3
"""2026-09-16 复盘：涨停池 + 上涨个股 K线形态与分时特征提取。

只读脚本：不写数据库、不下单、不训练。
输出到 outputs/today_review_20260916/
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd

DB = "/Users/youzix/WorkBuddy/Claw/backend/claw.db"
MINUTE_DIR = "/Users/youzix/WorkBuddy/Claw/runtime/quote_rounds/minute/trade_date=2026-09-16"
OUT = "/Users/youzix/WorkBuddy/Claw/outputs/today_review_20260916"
DATE = "2026-09-16"

os.makedirs(OUT, exist_ok=True)
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def limit_price(code: str, prev_close: float) -> float:
    """A股涨停价：主板10%，创业板/科创板20%，ST 5%。此处按代码前缀近似。"""
    if code.startswith(("30", "68")):
        rate = 0.20
    else:
        rate = 0.10
    return round(prev_close * (1 + rate), 2)


def load_minute() -> pd.DataFrame:
    files = sorted(f for f in os.listdir(MINUTE_DIR) if f.endswith(".parquet"))
    frames = []
    for f in files:
        df = pd.read_parquet(os.path.join(MINUTE_DIR, f), columns=[
            "code", "name", "minute", "open", "high", "low", "close",
            "prev_close", "change_pct", "volume", "amount", "avg_price",
        ])
        frames.append(df)
    m = pd.concat(frames, ignore_index=True)
    # minute 原始格式为 '2026-09-16T09:30:00'；必须规范成 'HHMM' 才能做时刻比较，
    # 否则 '2026-09-16T09:30:00' <= '1000' 恒为 False，会让所有时段切分失效。
    m["minute"] = pd.to_datetime(m["minute"]).dt.strftime("%H%M")
    m = m.sort_values(["code", "minute"]).reset_index(drop=True)
    return m


def kline_features(codes: list[str]) -> pd.DataFrame:
    q = """
    SELECT code, trade_date, open, high, low, close, volume, amount,
           turnover, change_pct, prev_close
    FROM stock_kline
    WHERE code IN ({}) AND trade_date <= ?
    ORDER BY code, trade_date
    """.format(",".join("?" * len(codes)))
    k = pd.read_sql_query(q, con, params=codes + [DATE])
    k["trade_date"] = k["trade_date"].astype(str)

    rows = []
    for code, g in k.groupby("code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if g.empty or g.iloc[-1]["trade_date"] != DATE:
            continue
        today = g.iloc[-1]
        hist = g.iloc[:-1]
        if len(hist) < 20:
            continue

        def ma(n):
            return float(hist["close"].tail(n).mean()) if len(hist) >= n else np.nan

        ma5, ma10, ma20 = ma(5), ma(10), ma(20)
        prior = hist.tail(10)
        prior_zt = int((prior["change_pct"] >= 9.7).sum())
        prior_5d_ret = float(hist["close"].iloc[-1] / hist["close"].iloc[-6] - 1) * 100 if len(hist) >= 6 else np.nan
        prior_20d_ret = float(hist["close"].iloc[-1] / hist["close"].iloc[-21] - 1) * 100 if len(hist) >= 21 else np.nan
        high20 = float(hist["close"].tail(20).max())
        low20 = float(hist["close"].tail(20).min())
        pos20 = (float(hist["close"].iloc[-1]) - low20) / (high20 - low20) * 100 if high20 > low20 else np.nan
        vol_ma5 = float(hist["volume"].tail(5).mean())
        vol_ratio = float(today["volume"] / vol_ma5) if vol_ma5 else np.nan
        ma20_dev = (float(hist["close"].iloc[-1]) / ma20 - 1) * 100 if ma20 else np.nan
        ma5_dev = (float(hist["close"].iloc[-1]) / ma5 - 1) * 100 if ma5 else np.nan

        # 昨日形态
        y = hist.iloc[-1]
        y_amp = float((y["high"] - y["low"]) / y["prev_close"] * 100) if y["prev_close"] else np.nan
        y_upper_shadow = float((y["high"] - max(y["open"], y["close"])) / y["prev_close"] * 100) if y["prev_close"] else np.nan

        rows.append(dict(
            code=code,
            fix_close=float(today["close"]),
            fix_chg=float(today["change_pct"]),
            fix_open=float(today["open"]),
            fix_high=float(today["high"]),
            fix_low=float(today["low"]),
            fix_volume=float(today["volume"]),
            fix_amount=float(today["amount"] or 0),
            fix_turnover=float(today["turnover"]) if today["turnover"] is not None else np.nan,
            prior_close=float(today["prev_close"]),
            ma5=ma5, ma10=ma10, ma20=ma20,
            ma5_dev=ma5_dev, ma20_dev=ma20_dev,
            pos20=pos20,
            prior_zt_10d=prior_zt,
            prior_5d_ret=prior_5d_ret,
            prior_20d_ret=prior_20d_ret,
            vol_ratio_5d=vol_ratio,
            y_chg=float(y["change_pct"]),
            y_amp=y_amp,
            y_upper_shadow=y_upper_shadow,
        ))
    return pd.DataFrame(rows)


def main():
    # ---------- 1. 今日涨停池 ----------
    zt = pd.read_sql_query(
        "SELECT code, name, limit_up_time, limit_up_price, seal_amount, break_count, "
        "consecutive_days, limit_up_reason, turnover FROM limit_up_pool WHERE trade_date=?",
        con, params=[DATE])
    zt["code"] = zt["code"].astype(str)

    # ---------- 2. 全市场上涨个股 ----------
    all_k = pd.read_sql_query(
        "SELECT code, close, prev_close, open, high, low, change_pct, volume, amount, turnover "
        "FROM stock_kline WHERE trade_date=?", con, params=[DATE])
    all_k["code"] = all_k["code"].astype(str)
    up = all_k[all_k["change_pct"] > 0].copy()
    print(f"[市场] 有数据 {len(all_k)} 只，上涨 {len(up)} 只，涨停池 {len(zt)} 只")

    names = pd.read_sql_query("SELECT code, name FROM stock_spot", con)
    names["code"] = names["code"].astype(str)
    name_map = dict(zip(names["code"], names["name"]))
    zt["name"] = zt["name"].fillna(zt["code"].map(name_map))

    # ---------- 3. 涨停池 K线特征 ----------
    zt_codes = zt["code"].tolist()
    kf = kline_features(zt_codes)
    z = zt.merge(kf, on="code", how="left")
    z["is_first_board"] = z["consecutive_days"].fillna(1) <= 1
    z["seal_yi"] = z["seal_amount"] / 1e8
    z["open_gap"] = (z["fix_open"] / z["prior_close"] - 1) * 100
    z["close_pos_in_range"] = (z["fix_close"] - z["fix_low"]) / (z["fix_high"] - z["fix_low"]).replace(0, np.nan) * 100
    z.to_csv(f"{OUT}/limitup_pool_features.csv", index=False, encoding="utf-8-sig")

    # ---------- 4. 分时路径特征 ----------
    m = load_minute()
    print(f"[分时] 载入 {len(m)} 行，{m['code'].nunique()} 只，{m['minute'].nunique()} 个分钟")
    m.to_parquet(f"{OUT}/minute_all_20260916.parquet", index=False)

    # 涨停价（用日K的 prev_close 与固定涨跌幅）
    zt_prev = dict(zip(z["code"], z["prior_close"]))
    recs = []
    for code in zt_codes:
        g = m[m["code"] == code].sort_values("minute")
        if g.empty:
            continue
        prev = zt_prev.get(code)
        if not prev or np.isnan(prev):
            continue
        lp = limit_price(code, prev)
        g = g.copy()
        g["chg"] = (g["close"] / prev - 1) * 100
        g["above_vwap"] = (g["close"] >= g["avg_price"]).astype(int)

        # 首次触及涨停的分钟
        touched = g[g["high"] >= lp - 1e-6]
        first_touch = touched["minute"].iloc[0] if not touched.empty else None
        # 最后封板分钟：从后往前找连续高于 = lp 的起点
        at_lp = g[g["close"] >= lp - 1e-6]
        last_seal = at_lp["minute"].iloc[0] if not at_lp.empty else None

        # 开盘30分钟行为
        early = g[g["minute"] <= "1000"]
        e_close = float(early["close"].iloc[-1]) if not early.empty else np.nan
        e_high = float(early["high"].max()) if not early.empty else np.nan
        e_low = float(early["low"].min()) if not early.empty else np.nan
        e_chg_close = (e_close / prev - 1) * 100 if not np.isnan(e_close) else np.nan
        e_chg_high = (e_high / prev - 1) * 100 if not np.isnan(e_high) else np.nan
        # 开盘是否破昨收（水下）
        below_water = bool((early["close"] < prev).any()) if not early.empty else False
        # 水下翻红时间
        red = early[early["close"] > prev]
        reclaim = red["minute"].iloc[0] if not red.empty else None
        # 分时上方占比（10:00后）
        late = g[g["minute"] > "1000"]
        vwap_hold = float(late["above_vwap"].mean()) if not late.empty else np.nan
        # 最大分时回撤（从最高到之后最低）
        peak_idx = g["close"].idxmax()
        after = g.loc[peak_idx:]
        max_dd = (after["close"].min() / g["close"].max() - 1) * 100 if len(after) else np.nan
        # 封板前拉升幅度（从当日最低到涨停）
        pre_run = (lp / float(g["low"].min()) - 1) * 100 if len(g) else np.nan

        recs.append(dict(
            code=code,
            limit_price=lp,
            first_touch_min=first_touch,
            last_seal_min=last_seal,
            open_chg=(float(g["open"].iloc[0]) / prev - 1) * 100,
            early_1000_chg=e_chg_close,
            early_1000_high=e_chg_high,
            early_1000_low=(e_low / prev - 1) * 100,
            below_water_open=below_water,
            reclaim_red_min=reclaim,
            vwap_hold_rate_late=vwap_hold,
            max_dd_after_peak=max_dd,
            pre_seal_run=pre_run,
            day_low_chg=(float(g["low"].min()) / prev - 1) * 100,
            day_high_chg=(float(g["high"].max()) / prev - 1) * 100,
            minutes_above_water=float((g["close"] > prev).mean()),
        ))
    mf = pd.DataFrame(recs)
    z = z.merge(mf, on="code", how="left")
    z.to_csv(f"{OUT}/limitup_pool_features.csv", index=False, encoding="utf-8-sig")

    # ---------- 5. 汇总统计 ----------
    summary = {
        "date": DATE,
        "generated_at": datetime.now().isoformat(),
        "market": {
            "total": int(len(all_k)),
            "up": int(len(up)),
            "up_ratio": round(len(up) / len(all_k) * 100, 2),
            "down": int((all_k["change_pct"] < 0).sum()),
            "flat": int((all_k["change_pct"] == 0).sum()),
            "up_gt_5": int((all_k["change_pct"] >= 5).sum()),
            "up_gt_9_8": int((all_k["change_pct"] >= 9.8).sum()),
            "limit_up_pool": int(len(zt)),
            "limit_up_touched_but_broke": int(((all_k["change_pct"] >= 9.8)).sum()) - int(len(zt)),
            "median_chg": round(float(all_k["change_pct"].median()), 2),
            "total_amount_yi": round(float(all_k["amount"].sum()) / 1e8, 0),
        },
        "limitup_structure": {
            "first_board": int((z["consecutive_days"].fillna(1) <= 1).sum()),
            "board_2": int((z["consecutive_days"] == 2).sum()),
            "board_ge_3": int((z["consecutive_days"] >= 3).sum()),
            "max_consecutive": int(z["consecutive_days"].max()),
            "avg_seal_yi": round(float(z["seal_yi"].mean()), 2),
            "median_seal_yi": round(float(z["seal_yi"].median()), 2),
            "seal_lt_0_5yi": int((z["seal_yi"] < 0.5).sum()),
            "seal_ge_2yi": int((z["seal_yi"] >= 2).sum()),
            "broke_once": int((z["break_count"] == 1).sum()),
            "broke_ge_2": int((z["break_count"] >= 2).sum()),
            "never_broke": int((z["break_count"] == 0).sum()),
            "open_gap_median": round(float(z["open_gap"].median()), 2),
            "open_below_water": int((z["open_gap"] < 0).sum()),
            "median_turnover": round(float(z["turnover"].median()), 2),
            "sealed_before_1000": int((z["limit_up_time"].fillna("999999") < "100000").sum()),
            "sealed_after_1300": int((z["limit_up_time"].fillna("000000") >= "130000").sum()),
        },
        "pattern_stats": {},
    }

    for col in ["prior_zt_10d", "prior_5d_ret", "prior_20d_ret", "pos20", "ma5_dev",
                "ma20_dev", "vol_ratio_5d", "y_amp", "y_upper_shadow",
                "vwap_hold_rate_late", "early_1000_chg", "max_dd_after_peak",
                "minutes_above_water", "pre_seal_run"]:
        if col in z.columns and z[col].notna().any():
            s = z[col].dropna()
            summary["pattern_stats"][col] = {
                "median": round(float(s.median()), 2),
                "p25": round(float(s.quantile(0.25)), 2),
                "p75": round(float(s.quantile(0.75)), 2),
                "n": int(len(s)),
            }

    with open(f"{OUT}/summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n输出：{OUT}")


if __name__ == "__main__":
    main()
