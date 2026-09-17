"""T2-8 重做：用腾讯 5 分钟线做入场后路径模拟（2026-09-17 复盘）。

背景
----
初版 T2-8 用"拒绝价 → 当日收盘"作收益口径，并据此把假设判为"拒绝"。
该口径与策略真实口径不符（`max_hold_days=5` + `take_profit_pct=8.0`
+ `stop_loss_pct=5.0`），且初版对照组仅 n=3。经用户质疑后复核，
初版结论更正为"证据不足"。本脚本用真实路径重做。

方法
----
1. 样本：`paper_auto_trade_log` 中 `reason LIKE '%确认后价格漂移X%'` 的拒绝行
   （这些是"现行 0.60% 阈值下被拒"的样本，因此本实验度量的是**增量成交**）。
2. 入场价：拒绝时刻观测价（`log.price`），入场时点为拒绝时刻之后的第一根 5 分钟 bar。
3. 逐 bar 推进，按账户12 的退出参数判定：
   - 止损 -5.0%  → 以 `entry × (1 − 0.05)` 成交
   - 止盈 +8.0%  → 以 `entry × (1 + 0.08)` 成交
   - 持有满 5 个交易日 → 以该日最后一根 bar 的收盘价成交
4. **5 分钟粒度内的顺序不确定性**：若同一根 bar 内同时触及止损与止盈，
   无法判断先后，故给出两个界：
   - 悲观界：先止损（下界）
   - 乐观界：先止盈（上界）
   真实值必落在两界之间。**不假装精确**。
5. 摩擦：按实际单笔 5,000 元的往返成本 0.25%（佣金万2.5最低5元双边
   + 印花税0.05% + 过户费0.001%双边）。

数据源
------
腾讯公开 5 分钟历史 `https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=<symbol>,m5,,640`
—— **开启证书校验**（`app/core/tls_trust.py` 已注入 CA bundle），
不使用 `ssl._create_unverified_context()`。
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.tls_trust import install_extra_ca_bundle

install_extra_ca_bundle()

CACHE = Path("/tmp/t2_8_m5_cache")
CACHE.mkdir(parents=True, exist_ok=True)

STOP_PCT = -5.0
TAKE_PCT = 8.0
MAX_HOLD_DAYS = 5
FRICTION_PCT = 0.25          # 单笔 5,000 元的往返摩擦
DRIFT_RE = re.compile(r"确认后价格漂移([0-9.]+)%")
M5_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?"


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
        except Exception as exc:
            if attempt == 2:
                print(f"  [warn] {symbol} 拉取失败: {type(exc).__name__}")
                return []
            time.sleep(1.0 * (attempt + 1))
    return []


def simulate(bars: list[list], entry_bar_index: int, entry: float) -> dict | None:
    """按 SL/TP/max_hold 逐 bar 推进，返回悲观/乐观两界的收益率。"""
    if entry_bar_index >= len(bars):
        return None
    stop = entry * (1 + STOP_PCT / 100)
    take = entry * (1 + TAKE_PCT / 100)

    hold_days: list[str] = []
    last_close = None
    for i in range(entry_bar_index, len(bars)):
        bar = bars[i]
        day = str(bar[0])[:8]
        if not hold_days or hold_days[-1] != day:
            hold_days.append(day)
            if len(hold_days) > MAX_HOLD_DAYS:
                return {"pessimistic": (last_close - entry) / entry * 100,
                        "optimistic": (last_close - entry) / entry * 100,
                        "bars": i - entry_bar_index, "ambiguous": False,
                        "exit": "持有到期", "censored": False}
        _t, _open, close, high, low = (str(bar[0]), float(bar[1]), float(bar[2]),
                                       float(bar[3]), float(bar[4]))
        last_close = close
        hit_stop = low <= stop
        hit_take = high >= take
        if hit_stop and hit_take:
            return {
                "pessimistic": (stop - entry) / entry * 100,
                "optimistic": (take - entry) / entry * 100,
                "bars": i - entry_bar_index + 1, "ambiguous": True,
                "exit": "同bar冲突", "censored": False,
            }
        if hit_stop:
            return {"pessimistic": (stop - entry) / entry * 100,
                    "optimistic": (stop - entry) / entry * 100,
                    "bars": i - entry_bar_index + 1, "ambiguous": False,
                    "exit": "止损", "censored": False}
        if hit_take:
            return {"pessimistic": (take - entry) / entry * 100,
                    "optimistic": (take - entry) / entry * 100,
                    "bars": i - entry_bar_index + 1, "ambiguous": False,
                    "exit": "止盈", "censored": False}
    if last_close is None:
        return None
    ret = (last_close - entry) / entry * 100
    # 数据用尽而非策略主动到期 -> 被截断，不能当作"持有到期"的实现收益
    return {"pessimistic": ret, "optimistic": ret, "bars": None, "ambiguous": False,
            "exit": "数据截断", "censored": True}


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    import sqlite3

    db = sqlite3.connect(str(Path(__file__).resolve().parent.parent / "backend" / "claw.db"))
    rows = db.execute(
        """SELECT trade_date, code, name, price, reason, coalesce(as_of_at, created_at)
           FROM paper_auto_trade_log WHERE reason LIKE '%确认后价格漂移%' ORDER BY trade_date"""
    ).fetchall()

    # 按 code 聚合，避免重复拉取
    by_code: dict[str, list[tuple]] = defaultdict(list)
    for td, code, name, price, reason, at in rows:
        m = DRIFT_RE.search(reason)
        if not m or not price:
            continue
        by_code[code].append((td, name, float(price), float(m.group(1)), str(at)))

    codes = sorted(by_code)
    print(f"样本 {sum(len(v) for v in by_code.values())} 笔 / {len(codes)} 只标的，开始拉取 5 分钟线…")
    t0 = time.time()
    bars_by_code = {code: fetch_m5(code) for code in codes}
    fetched = sum(1 for v in bars_by_code.values() if v)
    print(f"拉取完成: {fetched}/{len(codes)} 只成功, 耗时 {time.time()-t0:.1f}s\n")

    records = []
    skipped = Counter()
    for code, events in by_code.items():
        bars = bars_by_code[code]
        if not bars:
            skipped["无行情"] += len(events)
            continue
        times = [str(b[0]) for b in bars]
        for td, name, price, drift, at in events:
            key = td.replace("-", "")
            hhmm = at[11:16].replace(":", "")
            entry_idx = next((i for i, t in enumerate(times)
                              if t[:8] == key and t[8:] >= hhmm), None)
            if entry_idx is None:
                skipped["入场时点无对应bar"] += 1
                continue
            sim = simulate(bars, entry_idx, price)
            if sim is None:
                skipped["路径不足"] += 1
                continue
            records.append({
                "td": td, "code": code, "name": name, "price": price, "drift": drift,
                "at": at[11:19], **sim,
            })

    print(f"可模拟 {len(records)} 笔；跳过 {dict(skipped)}\n")

    def report(label: str, subset: list[dict]) -> dict:
        if not subset:
            print(f"{label}: 无样本")
            return {}
        pess = [r["pessimistic"] - FRICTION_PCT for r in subset]
        opt = [r["optimistic"] - FRICTION_PCT for r in subset]
        amb = sum(1 for r in subset if r["ambiguous"])
        print(f"{label}")
        print(f"    n={len(subset)}  其中同 bar 内 SL/TP 冲突（取两界）{amb} 笔")
        print(f"    悲观界: 均值 {statistics.mean(pess):+6.2f}%  中位 {statistics.median(pess):+6.2f}%  "
              f"胜率 {sum(1 for x in pess if x > 0)/len(pess)*100:>3.0f}%")
        print(f"    乐观界: 均值 {statistics.mean(opt):+6.2f}%  中位 {statistics.median(opt):+6.2f}%  "
              f"胜率 {sum(1 for x in opt if x > 0)/len(opt)*100:>3.0f}%")
        return {"n": len(subset), "pess": pess, "opt": opt}

    print("=" * 84)
    print("按漂移阈值分组的**增量成交**（现行 0.60% 下被拒、放宽后可成交）")
    print("=" * 84)
    for T in (1.0, 1.5):
        subset = [r for r in records if 0.6 < r["drift"] <= T]
        report(f"\n【阈值 {T}%】(全样本，含被数据边界截断的)", subset)
        clean = [r for r in subset if not r["censored"]]
        report(f"  └ 仅未截断子样本（入场后满 5 个交易日数据齐全）", clean)

    print("\n" + "=" * 84)
    print("分日拆解（阈值 1.0%，悲观界，已扣 0.25% 摩擦）")
    print("=" * 84)
    subset = [r for r in records if 0.6 < r["drift"] <= 1.0]
    byday = defaultdict(list)
    for r in subset:
        byday[r["td"]].append(r["pessimistic"] - FRICTION_PCT)
    contrib = {}
    print(f"  {'日期':<12}{'n':>4}{'均值%':>9}{'中位%':>9}{'胜率':>7}{'累计贡献pp':>12}")
    for d in sorted(byday):
        v = byday[d]
        contrib[d] = sum(v)
        print(f"  {d:<12}{len(v):>4}{statistics.mean(v):>9.2f}{statistics.median(v):>9.2f}"
              f"{sum(1 for x in v if x > 0)/len(v)*100:>6.0f}%{sum(v):>12.1f}")
    total = sum(contrib.values())
    print(f"\n  总累计 {total:+.1f}pp")
    if total:
        for d in sorted(contrib, key=lambda k: -abs(contrib[k]))[:3]:
            print(f"    {d}: {contrib[d]:+.1f}pp 占总 {contrib[d]/total*100:+.0f}%")
        top2 = sum(contrib[d] for d in sorted(contrib, key=lambda k: -abs(contrib[k]))[:2])
        print(f"    前 2 日合计 {top2:+.1f}pp = 总收益的 {top2/total*100:+.0f}%")
        rest = [x for d in sorted(contrib, key=lambda k: -abs(contrib[k]))[2:] for x in byday[d]]
        if rest:
            print(f"    剔除前 2 日后: n={len(rest)} 均值 {statistics.mean(rest):+.2f}%")

    print("\n" + "=" * 84)
    print("退出方式分布（阈值 1.0%）—— 验证路径模拟是否真的在起作用")
    print("=" * 84)
    exits = Counter()
    for r in subset:
        p = r["pessimistic"]
        if abs(p + 5.0) < 0.01:
            exits["止损 -5%"] += 1
        elif abs(p - 8.0) < 0.01:
            exits["止盈 +8%"] += 1
        else:
            exits["持有到期"] += 1
    print("  ", dict(exits))

    print("\n" + "=" * 84)
    print("个案核对：600356 恒丰纸业 2026-09-17（本假设的触发个案）")
    print("=" * 84)
    for r in records:
        if r["code"] == "600356":
            print(f"  拒绝 {r['at']} 价 {r['price']} 漂移 {r['drift']:.2f}% "
                  f"→ 悲观 {r['pessimistic']:+.2f}% 乐观 {r['optimistic']:+.2f}% "
                  f"持仓 {r['bars']} 根bar 冲突={r['ambiguous']}")
    return 0


raise SystemExit(main())
