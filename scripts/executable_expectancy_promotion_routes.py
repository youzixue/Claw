"""B/C/D（晋升引擎三路线）可执行收益对照实验：概率门槛 × 盘中确认链。

为什么需要这个脚本
------------------
`promotion_prediction_record.actual_close_change_pct` 实测等于
`stock_kline.change_pct(outcome_trade_date)`，即 close(P)→close(P+1)，
是**预测命中口径**，不是成交回报。B 的候选主要来自 P 日**收盘后**产生的
1510/2000 批次（信号在 P 日 15:10/20:00 才可知），P 日收盘价买不到，
只能 P+1 入场 —— 而 P+1 入场者是**付出**隔夜跳空，不是获得它。
实测该批次：结算口径 +0.811%，隔夜跳空 +1.488%，可执行 −0.643%。

所以"B 该不该开单、0.25 门槛对不对"不能再用结算列回答。本脚本改用
**逐轮真实报价归档**（`runtime/quote_rounds/compact/`）作为入场价，
忠实复刻 B 的消费窗口与盘中确认链，做 2×2 对照：

    {用 / 不用 概率门槛} × {用 / 不用 盘中确认链}

数据源（全部只读）
------------------
* `promotion_prediction_snapshot` / `promotion_prediction_run` —— 候选与批次时点
* `stock_kline` —— 收盘价（出场）
* `runtime/quote_rounds/compact/trade_date=*/qr-*.parquet` —— 逐轮真实报价（入场）

忠实复刻的口径（与 `app/api/v1/paper.py` 对齐）
----------------------------------------------
B 的消费窗口：
  * P=D−1 且 context ∈ (promotion_1510, promotion_2000)  → 买入日 D
  * P=D   且 context ∈ (promotion_0925, promotion_0935)  → 买入日 D
确认链（按 paper.py:5249-5303 的判定顺序）：
  行情缺失 → 低开(<0.0) → 追高(>5.8) → 一字/涨停 → 缩量(量比<0.6) → ST/退市/停牌
买入窗口：09:35–14:50。

**已知缺口（必须显式说明，不得假装完整）**
    * 归档 parquet 没有 `volume_ratio`，也没有 `limit_up`。
      - `limit_up` 由 `prev_close` × 板块涨跌幅推出（主板 10%、ST 5%），
        是**推导值**，与当时 `stock_spot.limit_up` 可能有个别差异。
      - `volume_ratio` **无法复刻** → 「缩量」这一条在重放中被跳过。
        因此本脚本给出的"确认链通过"是**上界**（条件放松 → 通过数偏多）。
    * 归档只覆盖 2026-09-07 起，所以能检验的买入日 D 是 09-08 ~ 09-17。
    * 入场价取"该轮报价的 price"，与引擎实际成交价仍有差异（引擎还要过
      报价路径确认、分批建仓、风控），本脚本测的是**信号可执行性**，
      不是成交还原。
"""
from __future__ import annotations

import glob
import sqlite3
import statistics as st
from datetime import date, datetime, time, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "backend" / "claw.db"
ROUNDS = Path(__file__).resolve().parents[1] / "runtime" / "quote_rounds" / "compact"

FRICTION_PCT = 0.15            # 往返摩擦
BUY_START, BUY_END = time(9, 35), time(14, 50)
MIN_VOLUME_RATIO = 0.6
PRE_CLOSE = ("promotion_1510", "promotion_2000")

# 账户档案：与 `paper.py` 的 PAPER_PROMOTION_ACCOUNTS + settings 一一对应
PROFILES = {
    "B 二板晋级": {
        "account": "promotion", "route": "second_board_promotion", "target_board": 2,
        "intraday": ("promotion_0925", "promotion_0935"),
        "min_confirm": 0.0, "max_confirm": 5.8,          # PAPER_PROMOTION_*
        "floor": 0.25, "enforce_floor": True,
        "admission": "actionable_only",
    },
    "C 主线扩散": {
        "account": "mainline", "route": "mainline_spread_start", "target_board": 1,
        "intraday": ("promotion_0925", "promotion_0935", "promotion_1000",
                     "promotion_1030", "promotion_1305", "promotion_1400", "promotion_1430"),
        "min_confirm": -0.5, "max_confirm": 4.0,         # PAPER_MAINLINE_*
        "floor": 0.4, "enforce_floor": False,            # 首板低基准率，不套绝对阈值
        "admission": "actionable_or_ranked",
    },
    "D 竞价强攻": {
        "account": "auction", "route": "auction_surge_start", "target_board": 1,
        "intraday": ("promotion_0925", "promotion_0935"),
        "min_confirm": 1.0, "max_confirm": 6.0,          # PAPER_AUCTION_*
        "floor": 0.2, "enforce_floor": False,
        "admission": "actionable_only",
    },
}


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def next_trade_day(conn: sqlite3.Connection, day: str) -> str | None:
    row = conn.execute(
        "SELECT trade_date FROM trade_calendar WHERE trade_date>? AND is_trade_day=1 "
        "ORDER BY trade_date LIMIT 1", (day,),
    ).fetchone()
    return str(row[0]) if row else None


def prev_trade_day(conn: sqlite3.Connection, day: str) -> str | None:
    row = conn.execute(
        "SELECT trade_date FROM trade_calendar WHERE trade_date<? AND is_trade_day=1 "
        "ORDER BY trade_date DESC LIMIT 1", (day,),
    ).fetchone()
    return str(row[0]) if row else None


def load_closes(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    return {
        (str(code), str(d)): float(close)
        for code, d, close in conn.execute(
            "SELECT code, trade_date, close FROM stock_kline WHERE close>0"
        )
    }


def _round_files(day: str) -> list[str]:
    return sorted(glob.glob(str(ROUNDS / f"trade_date={day}" / "qr-*.parquet")))


def load_day_rounds(day: str, codes: set[str]) -> dict[str, list[tuple[str, float, float, float]]]:
    """返回 {code: [(hhmmss, price, prev_close, change_pct), ...]} 按时间升序。"""
    import pandas as pd

    frames = []
    for path in _round_files(day):
        # 归档列里没有 as_of_at；committed_at 是 ISO 字符串（如
        # '2026-09-17T09:22:04.724603'），直接切 [11:19] 取 HH:MM:SS。
        frames.append(pd.read_parquet(
            path, columns=["code", "price", "prev_close", "change_pct", "committed_at"],
        ))
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df = df[df["code"].isin(codes)]
    df = df.sort_values("committed_at")
    out: dict[str, list[tuple[str, float, float, float]]] = {}
    for code, price, prev_close, change, committed in zip(
        df["code"], df["price"], df["prev_close"], df["change_pct"], df["committed_at"],
    ):
        stamp = str(committed)
        ts = stamp[11:19] if len(stamp) >= 19 else ""
        if len(ts) != 8 or ts[2] != ":":
            continue
        out.setdefault(str(code), []).append(
            (ts, float(price or 0), float(prev_close or 0),
             float(change) if change is not None else None)
        )
    return out


def load_pool(conn: sqlite3.Connection, profile: dict) -> dict[str, list[dict]]:
    """按买入日 D 组织某账户的可消费候选池。"""
    rows = conn.execute("""
        SELECT r.snapshot_context              AS ctx,
               r.reference_trade_date          AS ref,
               r.as_of_at                      AS asof,
               s.code                          AS code,
               s.calibrated_probability        AS cal,
               s.actionable                    AS actionable,
               s.rank_scope                    AS rank_scope
        FROM promotion_prediction_snapshot s
        JOIN promotion_prediction_run r ON r.id = s.run_id
        WHERE s.candidate_route=? AND s.target_board=?
          AND s.trade_gate_passed=1 AND s.watch_only=0
          AND r.snapshot_source='schedule'
    """, (profile["route"], profile["target_board"])).fetchall()
    intraday = tuple(profile["intraday"])
    actionable_only = profile["admission"] == "actionable_only"

    pool: dict[str, list[dict]] = {}
    for ctx, ref, asof, code, cal, actionable, rank_scope in rows:
        # 注意：sqlite 返回的是整数 1/0，`1 is not True` 为 True —— 用真值判断，
        # 不要用 `is True`（第一版就是踩了这个，导致池子恒为空）。
        #
        # C 的准入是 `actionable OR rank_scope in (ranked, recall_ranked)`
        # （paper.py:5126-5129）；B/D 严格要求 actionable。
        admitted = bool(actionable) or (
            not actionable_only and str(rank_scope or "") in ("ranked", "recall_ranked")
        )
        if not admitted:
            continue
        if ctx in PRE_CLOSE:
            buy_day = next_trade_day(conn, str(ref))
            group = "pre_close"
        elif ctx in intraday:
            buy_day, group = str(ref), "intraday"
        else:
            continue
        if not buy_day:
            continue
        pool.setdefault(buy_day, []).append({
            "code": str(code), "cal": float(cal or 0), "ctx": ctx,
            "group": group, "asof": str(asof),
        })
    return pool


def confirm_pass(history: list[tuple[str, float, float, float]], *, after: str | None,
                 limit_pct: float, min_confirm: float, max_confirm: float) -> tuple[str, float] | None:
    """复刻确认链；返回 (时间, 入场价) 或 None。

    `after` 非空时只考虑该时刻之后的轮次（盘中上下文要求信号之后才可买）。
    跳过「缩量」（归档无量比），所以结果是**上界**。
    """
    for ts, price, prev_close, change in history:
        if ts < BUY_START.strftime("%H:%M:%S") or ts > BUY_END.strftime("%H:%M:%S"):
            continue
        if after and ts < after:
            continue
        if not price or price <= 0 or not prev_close or prev_close <= 0 or change is None:
            continue                                   # 行情缺失
        if change < min_confirm:                       # 低开
            continue
        if change > max_confirm:                       # 追高
            continue
        limit_up = round(prev_close * (1 + limit_pct), 2)
        if price >= limit_up * 0.998:                  # 一字/涨停
            continue
        return ts, price
    return None


def first_price(history: list[tuple[str, float, float, float]], *, after: str | None) -> float | None:
    for ts, price, _pc, _ch in history:
        if ts < BUY_START.strftime("%H:%M:%S") or ts > BUY_END.strftime("%H:%M:%S"):
            continue
        if after and ts < after:
            continue
        if price and price > 0:
            return price
    return None


def summarise(label: str, values: list[float]) -> str:
    n = len(values)
    if n < 2:
        return f"  {label:<34} n={n:<5} 样本不足"
    mean = st.mean(values)
    se = st.stdev(values) / n ** 0.5
    tc = 1.96 if n > 21 else 2.086
    win = sum(1 for v in values if v > 0) / n * 100
    return (f"  {label:<34} n={n:<5} 均值={mean:+7.3f}%  净={mean - FRICTION_PCT:+7.3f}%  "
            f"净CI下界={mean - tc * se - FRICTION_PCT:+7.3f}%  胜率={win:4.0f}%")


def run_profile(conn, closes, st_codes, profile: dict) -> dict:
    pool = load_pool(conn, profile)
    buy_days = sorted(d for d in pool if _round_files(d))
    stats = {k: [] for k in (
        "nofloor_nochain_close", "nofloor_nochain_next",
        "floor_nochain_close", "floor_nochain_next",
        "nofloor_chain_close", "nofloor_chain_next",
        "floor_chain_close", "floor_chain_next",
    )}
    counts = {"pool": 0, "floor": 0, "chain": 0, "floor_chain": 0}
    per_day = []
    for day in buy_days:
        entries = pool[day]
        rounds = load_day_rounds(day, {e["code"] for e in entries})
        nxt = next_trade_day(conn, day)
        bought_nochain = bought_chain = 0
        for entry in entries:
            code = entry["code"]
            history = rounds.get(code) or []
            if not history:
                continue
            close_d = closes.get((code, day))
            if not close_d:
                continue
            counts["pool"] += 1
            close_n = closes.get((code, nxt)) if nxt else None
            limit_pct = 0.05 if code in st_codes else 0.10
            after = None
            if entry["group"] == "intraday":
                try:
                    after = datetime.fromisoformat(entry["asof"]).strftime("%H:%M:%S")
                except Exception:
                    after = None
            # 概率门槛：C/D 设 enforce_floor=False，则门槛不参与筛选
            over_floor = (
                entry["cal"] >= profile["floor"] if profile["enforce_floor"] else True
            )
            p_any = first_price(history, after=after)
            p_conf = confirm_pass(history, after=after, limit_pct=limit_pct,
                                  min_confirm=profile["min_confirm"],
                                  max_confirm=profile["max_confirm"])

            def record(key: str, entry_price: float) -> None:
                stats[f"{key}_close"].append((close_d / entry_price - 1) * 100)
                if close_n:
                    stats[f"{key}_next"].append((close_n / entry_price - 1) * 100)

            if p_any:
                record("nofloor_nochain", p_any)
                bought_nochain += 1
                if over_floor:
                    counts["floor"] += 1
                    record("floor_nochain", p_any)
            if p_conf:
                record("nofloor_chain", p_conf[1])
                counts["chain"] += 1
                bought_chain += 1
                if over_floor:
                    counts["floor_chain"] += 1
                    record("floor_chain", p_conf[1])
        per_day.append((day, len(entries), bought_nochain, bought_chain))
    return {"stats": stats, "counts": counts, "per_day": per_day}


LABELS = (
    ("nofloor_nochain", "不用门槛 不用确认链"),
    ("floor_nochain", "用门槛   不用确认链"),
    ("nofloor_chain", "不用门槛 用确认链"),
    ("floor_chain", "用门槛   用确认链（≈现行）"),
)


def main() -> int:
    conn = _conn()
    closes = load_closes(conn)
    st_codes = {str(r[0]) for r in conn.execute(
        "SELECT code FROM stock_tags WHERE COALESCE(is_st,0)=1")}
    print("B/C/D 可执行收益对照实验（入场价 = 逐轮真实报价，出场 = 收盘价）")
    print(f"摩擦 {FRICTION_PCT}%；买入窗口 {BUY_START}-{BUY_END}")
    print("⚠️ 归档无 volume_ratio → 跳过「缩量」，「确认链通过」是**上界**")
    print("⚠️ C/D 的 enforce_probability_floor=False，故其「用门槛」列等同「不用门槛」\n")

    for name, profile in PROFILES.items():
        result = run_profile(conn, closes, st_codes, profile)
        c = result["counts"]
        print("=" * 78)
        print(f"{name}   route={profile['route']}  admission={profile['admission']}")
        print(f"  确认带 [{profile['min_confirm']}, {profile['max_confirm']}]  "
              f"概率门槛 {profile['floor']}（enforce={profile['enforce_floor']}）")
        print(f"  池={c['pool']}  过概率门槛={c['floor']}  确认链通过={c['chain']}  "
              f"门槛+链={c['floor_chain']}")
        print("  --- 出场=当日收盘 ---")
        for key, label in LABELS:
            print(summarise(label, result["stats"][f"{key}_close"]))
        print("  --- 出场=次日收盘（持有过夜）---")
        for key, label in LABELS:
            print(summarise(label, result["stats"][f"{key}_next"]))
        print()

    print("⚠️ 口径限制（不得省略）：")
    print("   1. 入场价是「该轮报价」而非成交价；引擎还要过报价路径确认/分批/风控。")
    print("   2. 缩量未复刻（归档无量比）→「确认链通过」偏多，结果是**上界**。")
    print("   3. 出场用收盘价是固定假设，引擎实际按各自卖出规则分批离场。")
    print("   4. 归档只从 2026-09-07 起，样本仅 9 个买入日：")
    print("      B 池=153、C 池=8、D 池=2 —— C/D 样本量不足以判定，只能说「候选几乎不存在」。")
    print("   5. 本脚本只覆盖晋升引擎路线；E(十倍)/F(反转) 直读 limit_up_pool，")
    print("      是另一套候选机制，需单独做同样的实验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
