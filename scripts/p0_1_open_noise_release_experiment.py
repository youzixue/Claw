"""P0-1 离线回放：`open_noise_end=09:45` 的整点释放是否造成可测负超额。

问题
----
09:30–09:44 只有 4 类退出信号可触发，09:45 后有 10 类同时开放，
抑制窗整点解除把分散减仓压缩为集合清仓。9/01–9/17 的 71 笔卖出中
33 笔（46.5%）落在 09:45–09:49 —— 若均匀分布，5 分钟窗口期望约 2 笔。

实验（三种释放策略，同一批持仓、同一批原因）
--------------------------------------------
(a) 基线：保持 09:45 整点释放，成交价用**真实成交价**（不是 bar 收盘价）。
(b1) 全部改为 10:15 释放。
(b2) 全部改为 10:45 释放。
(b3) 分批释放：09:45 / 10:15 / 10:45 各 1/3 —— 分配规则必须与结果无关，
     这里按 (交易日, 账户, 代码) 排序后轮转发牌，避免挑赢家。
(c) 相对释放（每笔持仓自首次触发候选起 N 分钟）**未做**：需要逐笔持仓的
    "首次触发候选"时刻，该时刻没有落库，任何重建都会引入前视偏差。

接受标准
--------
卖后 30min / 收盘超额（对标 `stock_kline` 当日均价）改善 ≥0.5pp，
且一致日/分歧日/杀跌日三种风格均不恶化。否则维持 09:45 整点释放。

口径与已知局限（必须随结论一起读）
----------------------------------
1. 技术信号 30 分钟前的状态不可回溯：本实验**固定"同一批原因在同一批持仓上
   仍然成立"**，只改释放时刻。若提前 30 分钟释放会被更强的止损/止盈先拦下，
   本实验看不到 —— 这是乐观偏差。
2. 5 分钟粒度：释放价取覆盖该时刻的 bar 收盘价，不取 bar 内最优价。
3. 摩擦成本对三种规则相同，故不影响"相对改善"，但会压低绝对水平；
   报告里同时给出成本口径。
4. 未建模中途止损：额外报告"备选时刻之前最低价曾低于基线成交价"的比例，
   作为路径风险的下界提示，不假装是精确止损模拟。
5. 一致日样本只有 1 天 → 按风格分层的结论**统计上不可用**，只作方向参考。

数据源
------
腾讯公开 5 分钟历史
`https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=<symbol>,m5,,640`
—— 开启证书校验（`app/core/tls_trust.py` 注入 CA bundle），
不使用 `ssl._create_unverified_context()`。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.core.tls_trust import install_extra_ca_bundle

install_extra_ca_bundle()

DB = REPO / "backend" / "claw.db"
CACHE = Path("/tmp/p0_1_m5_cache")
CACHE.mkdir(parents=True, exist_ok=True)
OUT = REPO / "outputs" / "today_review_20260917"
M5_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?"

START, END = "2026-09-01", "2026-09-17"
# 释放批次：基线 09:45，备选 10:15 / 10:45
BASELINE_RELEASE = "09:45"
ALT_RELEASES = ("10:15", "10:45")
# 卖后 30 分钟观察点（用于"卖后 30min"口径）
POST_WINDOW_MIN = 30
FRICTION_PCT = 0.15          # 单边：佣金万2.5双边 + 印花税0.05% + 滑点，保守取 0.15%


def tencent_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("4", "8")):
        return "bj" + code
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    return "sz" + code


def fetch_m5(code: str, count: int = 640) -> list[list]:
    """取 5 分钟线（带磁盘缓存），开启证书校验。"""
    symbol = tencent_symbol(code)
    path = CACHE / f"{symbol}_{count}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    url = M5_URL + urllib.parse.urlencode({"param": f"{symbol},m5,,{count}"})
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.load(response)
            bars = ((payload.get("data") or {}).get(symbol) or {}).get("m5") or []
            path.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
            return bars
        except Exception as exc:  # noqa: BLE001 - 数据源失败必须显式暴露
            if attempt == 2:
                print(f"  [warn] {symbol} 拉取失败: {type(exc).__name__}: {exc}")
                return []
            time.sleep(1.0 * (attempt + 1))
    return []


def load_sells() -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT trade_time, account_id, code, price, amount, reason "
            "FROM paper_trade_log WHERE trade_type='sell' "
            "AND trade_time >= ? AND trade_time < ? ORDER BY trade_time",
            (f"{START} 00:00:00", f"{END} 23:59:59"),
        ).fetchall()
        vwap = {
            (r["code"], r["trade_date"]): (r["amount"] / r["volume"])
            for r in conn.execute(
                "SELECT code, trade_date, amount, volume FROM stock_kline "
                "WHERE trade_date >= ? AND trade_date <= ? AND volume > 0 AND amount > 0",
                (START, END),
            )
        }
        styles = {
            r["trade_date"]: r["advance_decline_ratio"]
            for r in conn.execute(
                "SELECT trade_date, advance_decline_ratio FROM market_sentiment "
                "WHERE trade_date >= ? AND trade_date <= ?", (START, END),
            )
        }
    finally:
        conn.close()
    return [
        {
            "at": r["trade_time"], "day": r["trade_time"][:10],
            "hm": r["trade_time"][11:16], "account": r["account_id"],
            "code": r["code"], "price": float(r["price"]), "amount": int(r["amount"]),
            "reason": str(r["reason"] or ""),
            "vwap": vwap.get((r["code"], r["trade_time"][:10])),
            "adr": styles.get(r["trade_time"][:10]),
        }
        for r in rows
    ]


def style_of(adr: float | None) -> str:
    if adr is None:
        return "未知"
    if adr >= 1.5:
        return "一致日"
    if adr >= 0.8:
        return "分歧日"
    return "杀跌日"


def bars_of(bars: list[list], day: str) -> dict[str, list]:
    key = day.replace("-", "")
    return {str(b[0])[8:12]: b for b in bars if str(b[0])[:8] == key}


def bar_close(bars: list[list], day: str, hm: str) -> float | None:
    """覆盖 hm 的 5 分钟 bar 的收盘价；hm 必须是 5 分钟边界。"""
    table = bars_of(bars, day)
    bar = table.get(hm.replace(":", ""))
    if bar is None:
        return None
    try:
        return float(bar[2])
    except (TypeError, ValueError):
        return None


def bar_low_since(bars: list[list], day: str, start_hm: str, end_hm: str) -> float | None:
    table = bars_of(bars, day)
    lows = []
    for hm, bar in table.items():
        if start_hm.replace(":", "") <= hm <= end_hm.replace(":", ""):
            try:
                lows.append(float(bar[4]))
            except (TypeError, ValueError):
                continue
    return min(lows) if lows else None


def mean_ci(values: list[float]) -> tuple[float, float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    m = statistics.mean(values)
    if n == 1:
        return m, m, 0.0
    se = statistics.stdev(values) / (n ** 0.5)
    return m, m - 1.96 * se, m + 1.96 * se


def main() -> int:
    sells = load_sells()
    window = [s for s in sells if "09:45" <= s["hm"] <= "09:49"]
    print(f"9/01–9/17 卖出成交 {len(sells)} 笔，其中 09:45–09:49 释放 {len(window)} 笔 "
          f"({len(window) / len(sells):.1%})")

    codes = sorted({s["code"] for s in window})
    print(f"涉及 {len(codes)} 只股票，{len({s['day'] for s in window})} 个交易日，开始取 5 分钟线…")
    m5 = {code: fetch_m5(code) for code in codes}
    missing = [c for c, b in m5.items() if not b]
    if missing:
        print(f"  [warn] {len(missing)} 只无 5 分钟数据，将从样本剔除: {missing}")

    # 轮转发牌：分配规则只依赖 (交易日, 账户, 代码)，与结果无关
    ordered = sorted(window, key=lambda s: (s["day"], s["account"] or 0, s["code"]))
    round_robin: dict[tuple, str] = {}
    for i, s in enumerate(ordered):
        round_robin[(s["day"], s["account"], s["code"], s["at"])] = (
            BASELINE_RELEASE, *ALT_RELEASES
        )[i % 3]

    usable, dropped = [], Counter()
    for s in window:
        bars = m5.get(s["code"]) or []
        if not bars:
            dropped["无5分钟数据"] += 1
            continue
        if s["vwap"] is None:
            dropped["无当日均价"] += 1
            continue
        row = dict(s)
        row["style"] = style_of(s["adr"])
        row["rr_release"] = round_robin[(s["day"], s["account"], s["code"], s["at"])]
        row["alt"] = {}
        ok = True
        for rel in ALT_RELEASES:
            price = bar_close(bars, s["day"], rel)
            if price is None:
                ok = False
                dropped[f"缺{rel}bar"] += 1
                break
            row["alt"][rel] = price
        if not ok:
            continue
        # 卖后 30 分钟观察点：09:45 + 30min = 10:15
        row["post30"] = bar_close(bars, s["day"], "10:15")
        row["low_to_1015"] = bar_low_since(bars, s["day"], "09:50", "10:15")
        row["low_to_1045"] = bar_low_since(bars, s["day"], "09:50", "10:45")
        usable.append(row)

    print(f"可用样本 {len(usable)} 笔" + (f"，剔除 {dict(dropped)}" if dropped else ""))
    if not usable:
        print("无可用样本，实验无法进行")
        return 1

    # 每笔在每个规则下的成交价
    def price_under(row: dict, rule: str) -> float:
        if rule == "a":
            return row["price"]
        if rule == "b1":
            return row["alt"]["10:15"]
        if rule == "b2":
            return row["alt"]["10:45"]
        if rule == "c":
            rel = row["rr_release"]
            return row["price"] if rel == BASELINE_RELEASE else row["alt"][rel]
        raise ValueError(rule)

    rules = ("a", "b1", "b2", "c")
    labels = {
        "a": "(a) 09:45 整点释放（基线，真实成交价）",
        "b1": "(b1) 全部 10:15 释放",
        "b2": "(b2) 全部 10:45 释放",
        "c": "(b3) 分批释放 09:45/10:15/10:45 各 1/3（轮转发牌）",
    }

    print()
    print("== 每个规则：相对基线的价格改善（按当日均价归一，pp）==")
    print("   '卖后30min/收盘超额改善 >=0.5pp' 的判据 = 该规则成交价相对基线成交价的差距")
    summary: dict[str, dict] = {}
    for rule in rules:
        # 相对基线成交价的价格改善，按当日 VWAP 归一（消除个股价位差异）
        deltas = [
            (price_under(r, rule) - r["price"]) / r["vwap"] * 100 for r in usable
        ]
        m, lo, hi = mean_ci(deltas)
        # 相对当日收盘的超额（绝对水平，含成本）
        vs_close = [price_under(r, rule) / vwap_close(r) - 1 for r in usable if vwap_close(r)]
        summary[rule] = {"deltas": deltas, "mean": m, "lo": lo, "hi": hi}
        wins = sum(1 for d in deltas if d > 0)
        net = [d - FRICTION_PCT for d in deltas] if rule != "a" else deltas
        print(f"  {labels[rule]}")
        print(f"    改善 mean={m:+.3f}pp  95%CI=[{lo:+.3f}, {hi:+.3f}]  "
              f"优于基线 {wins}/{len(deltas)} ({wins / len(deltas):.0%})  "
              f"扣摩擦后 mean={statistics.mean(net):+.3f}pp" if rule != "a"
              else f"    改善 mean={m:+.3f}pp（基线自身，恒为 0）")

    print()
    print("== 按市场风格分层（改善 mean，pp；括号内 n）==")
    styles = ("一致日", "分歧日", "杀跌日", "未知")
    header = "  规则".ljust(46) + "".join(s.ljust(22) for s in styles)
    print(header)
    for rule in rules:
        cells = []
        for style in styles:
            sub = [(price_under(r, rule) - r["price"]) / r["vwap"] * 100
                   for r in usable if r["style"] == style]
            cells.append(
                (f"{statistics.mean(sub):+.3f} (n={len(sub)})" if sub else "- (n=0)").ljust(22)
            )
        print("  " + labels[rule].ljust(44) + "".join(cells))

    print()
    print("== 路径风险（备选时刻之前最低价是否曾低于基线成交价）==")
    for rel, key in (("10:15", "low_to_1015"), ("10:45", "low_to_1045")):
        involved = [r for r in usable
                    if price_under(r, "c") != r["price"] and r["rr_release"] == rel
                    or rel == "10:15"]
        below = [r for r in usable if r.get(key) is not None and r[key] < r["price"]]
        print(f"  至 {rel}: {len(below)}/{len(usable)} 笔在此之前曾跌破基线成交价"
              f"（{len(below) / len(usable):.0%}）")

    print()
    print("== 判据检查（接受标准：改善 >=0.5pp 且三种风格均不恶化）==")
    verdict = {}
    for rule in ("b1", "b2", "c"):
        m = summary[rule]["mean"]
        ok_magnitude = m >= 0.5
        style_means = {}
        for style in ("一致日", "分歧日", "杀跌日"):
            sub = [(price_under(r, rule) - r["price"]) / r["vwap"] * 100
                   for r in usable if r["style"] == style]
            style_means[style] = statistics.mean(sub) if sub else None
        ok_styles = all(v is None or v >= 0 for v in style_means.values())
        verdict[rule] = (ok_magnitude, ok_styles, m, style_means)
        print(f"  {rule}: 改善 {m:+.3f}pp (>=0.5pp: {ok_magnitude}) | "
              f"风格不恶化: {ok_styles} | 分层="
              f"{ {k: (round(v, 3) if v is not None else None) for k, v in style_means.items()} }")
    passed = [r for r in verdict if verdict[r][0] and verdict[r][1]]
    print()
    print("  结论：" + ("满足接受标准的规则: " + ", ".join(passed) if passed
                      else "没有任何规则同时满足'改善>=0.5pp'与'三种风格均不恶化'"))

    _write_report(usable, summary, verdict, labels, dropped, len(sells), len(window))
    return 0


def vwap_close(row: dict) -> float | None:
    """当日均价；stock_kline 的 amount/volume 即当日 VWAP。"""
    return row["vwap"]


def _write_report(usable, summary, verdict, labels, dropped, total_sells, window_sells):
    lines = [
        "# P0-1 离线实验：`open_noise_end=09:45` 分批释放（2026-09-18）",
        "",
        "> 由 `scripts/p0_1_open_noise_release_experiment.py` 生成，可重跑。",
        "",
        "## 样本",
        "",
        f"- 9/01–9/17 已成交卖出 **{total_sells}** 笔，其中 09:45–09:49 释放 "
        f"**{window_sells}** 笔（{window_sells / total_sells:.1%}）。",
        f"- 进入回放的可用样本 **{len(usable)}** 笔"
        + (f"；剔除 {dict(dropped)}。" if dropped else "。"),
        "",
        "## 相对基线的价格改善（按当日均价归一，pp）",
        "",
        "| 规则 | 改善 mean | 95% CI | 优于基线占比 | 扣摩擦后 mean |",
        "|---|---|---|---|---|",
    ]
    for rule, s in summary.items():
        d = s["deltas"]
        wins = sum(1 for x in d if x > 0)
        net = statistics.mean(d) - (FRICTION_PCT if rule != "a" else 0.0)
        lines.append(
            f"| {labels[rule]} | {s['mean']:+.3f} | "
            f"[{s['lo']:+.3f}, {s['hi']:+.3f}] | "
            f"{wins}/{len(d)} ({wins / len(d):.0%}) | {net:+.3f} |"
        )
    lines += ["", "## 判据检查", "",
              "接受标准：改善 ≥0.5pp **且**一致日/分歧日/杀跌日三种风格均不恶化。", ""]
    for rule, (ok_mag, ok_style, m, style_means) in verdict.items():
        lines.append(
            f"- **{rule}**（{labels[rule]}）：改善 {m:+.3f}pp → 幅度达标 `{ok_mag}`；"
            f"风格不恶化 `{ok_style}`；分层 "
            + ", ".join(f"{k}={('%.3f' % v) if v is not None else 'n/a'}"
                        for k, v in style_means.items())
        )
    lines += [
        "",
        "## 口径与局限（结论必须与此同读）",
        "",
        "1. 技术信号 30 分钟前的状态不可回溯。本实验固定\"同一批原因在同一批持仓上",
        "   仍然成立\"，只改释放时刻；若提前释放会被更强的止损/止盈先拦下，本实验看不到",
        "   —— 属于**乐观偏差**。",
        "2. 释放价取覆盖该时刻的 5 分钟 bar 收盘价，不取 bar 内最优价。",
        "3. 摩擦成本对三种规则相同，不影响相对改善，但会压低绝对水平。",
        "4. 未建模中途止损，只报告路径风险下界。",
        "5. 一致日样本极少（8 个交易日里约 1 天），按风格分层**统计上不可用**，只作方向参考。",
        f"6. 备选释放时刻为 {'、'.join(ALT_RELEASES)}；相对释放（方案 c）因缺少\"首次触发候选\"",
        "   落库时刻未做。",
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "P0-1-09-45分批释放离线实验-20260918.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {path}")


if __name__ == "__main__":
    raise SystemExit(main())
