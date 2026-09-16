#!/usr/bin/env python3
"""Research-only full-sample study for one A-share limit-up day.

The script reads the frozen local database in read-only mode, downloads public
minute data into an output cache, and writes research artifacts. It never
changes production tables, strategy parameters, orders, or positions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import ssl
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "claw.db"
DEFAULT_DATE = "2026-09-01"
SSL_CONTEXT = ssl._create_unverified_context()
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def pct(price: float | None, prev_close: float | None) -> float | None:
    if price is None or prev_close in (None, 0):
        return None
    return round((price / prev_close - 1.0) * 100.0, 4)


def hhmmss(value: Any) -> str:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 6:
        return raw[-6:]
    if len(raw) == 4:
        return raw + "00"
    return raw.zfill(6)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fetch_json(url: str, retries: int = 2, timeout: int = 15) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://quote.eastmoney.com/",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(str(last_error))


def symbol_for(code: str) -> str:
    if code.startswith(("8", "9")):
        return "bj" + code
    if code.startswith("6"):
        return "sh" + code
    return "sz" + code


def eastmoney_secid(code: str) -> str | None:
    if code.startswith(("8", "9")):
        return None
    return ("1." if code.startswith("6") else "0.") + code


def load_pool(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["code"] = str(row["code"]).zfill(6)
        row["consecutive_days"] = integer(row.get("consecutive_days"))
        row["break_count"] = integer(row.get("break_count"))
        row["turnover_pct"] = number(row.get("turnover_pct"))
        row["close"] = number(row.get("close"))
        row["close_change_pct"] = number(row.get("close_change_pct"))
        row["is_st"] = integer(row.get("is_st"))
    return rows


def open_readonly_db(path: Path) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def get_tencent_payload(code: str, cache_dir: Path, refresh: bool) -> tuple[Any | None, str]:
    cache = cache_dir / (code + ".json")
    if cache.exists() and not refresh:
        return read_json(cache), "cache"
    symbol = symbol_for(code)
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + symbol
    try:
        payload = fetch_json(url, retries=2)
        write_json(cache, payload)
        return payload, "remote"
    except Exception as exc:
        return None, "error:" + str(exc)


def parse_tencent(code: str, trade_date: str, payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    symbol = symbol_for(code)
    node = ((payload or {}).get("data") or {}).get(symbol) or {}
    data_node = node.get("data") or {}
    raw_rows = data_node.get("data") or []
    quote = (node.get("qt") or {}).get(symbol) or []

    def q(index: int) -> float | None:
        return number(quote[index]) if len(quote) > index else None

    meta = {
        "name_tencent": quote[1] if len(quote) > 1 else "",
        "prev_close": q(4),
        "official_open": q(5),
        "official_high": q(33),
        "official_low": q(34),
        "official_close": q(3),
        "quote_time": quote[30] if len(quote) > 30 else "",
    }

    result: list[dict[str, Any]] = []
    previous_volume = 0
    previous_amount = 0.0
    for raw in raw_rows:
        parts = str(raw).split()
        if len(parts) < 3:
            continue
        minute = parts[0].zfill(4)
        if minute < "0930" or minute > "1500":
            continue
        price = number(parts[1])
        cumulative_volume = integer(parts[2])
        cumulative_amount = number(parts[3]) if len(parts) >= 4 else None
        minute_volume = max(0, cumulative_volume - previous_volume)
        minute_amount = (
            max(0.0, cumulative_amount - previous_amount)
            if cumulative_amount is not None
            else None
        )
        previous_volume = cumulative_volume
        if cumulative_amount is not None:
            previous_amount = cumulative_amount
        result.append(
            {
                "trade_date": trade_date,
                "code": code,
                "time": minute + "00",
                "price": price,
                "change_pct": pct(price, meta["prev_close"]),
                "cumulative_volume": cumulative_volume,
                "cumulative_amount": (
                    round(cumulative_amount, 2) if cumulative_amount is not None else None
                ),
                "minute_volume": minute_volume,
                "minute_amount": (
                    round(minute_amount, 2) if minute_amount is not None else None
                ),
                "source": "tencent_minute",
            }
        )
    return result, meta


def get_eastmoney_payload(code: str, cache_dir: Path, refresh: bool) -> tuple[Any | None, str]:
    secid = eastmoney_secid(code)
    if secid is None:
        return None, "unsupported_bse"
    cache = cache_dir / (code + ".json")
    if cache.exists() and not refresh:
        return read_json(cache), "cache"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "iscr": "1",
        "ndays": "1",
        "secid": secid,
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get?" + urllib.parse.urlencode(params)
    try:
        payload = fetch_json(url, retries=1)
        if not ((payload or {}).get("data") or {}).get("trends"):
            raise RuntimeError("empty trends")
        write_json(cache, payload)
        return payload, "remote"
    except Exception as exc:
        return None, "error:" + str(exc)


def parse_eastmoney_auction(code: str, trade_date: str, payload: Any) -> list[dict[str, Any]]:
    node = (payload or {}).get("data") or {}
    prev_close = number(node.get("preClose"))
    result: list[dict[str, Any]] = []
    for raw in node.get("trends") or []:
        fields = str(raw).split(",")
        if len(fields) < 8:
            continue
        stamp = fields[0]
        if not stamp.startswith(trade_date):
            continue
        minute = stamp[-5:].replace(":", "")
        if minute >= "0930":
            continue
        open_price = number(fields[1])
        close_price = number(fields[2])
        high = number(fields[3])
        low = number(fields[4])
        volume = integer(fields[5])
        amount = number(fields[6], 0.0) or 0.0
        avg = number(fields[7])
        representative = close_price or open_price
        if representative is None or representative <= 0:
            continue
        result.append(
            {
                "trade_date": trade_date,
                "code": code,
                "time": minute + "00",
                "open": open_price,
                "close": close_price,
                "high": high,
                "low": low,
                "price": representative,
                "change_pct": pct(representative, prev_close),
                "volume": volume,
                "amount": round(amount, 2),
                "avg_price": avg,
                "source": "eastmoney_trends_iscr1",
            }
        )
    return result


def load_local_auction(connection: sqlite3.Connection, code: str, trade_date: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT auction_time, auction_price, auction_volume, auction_amount,
               prev_close, open_change, volume_ratio
        FROM auction_data
        WHERE code = ? AND trade_date = ?
        ORDER BY auction_time
        """,
        (code, trade_date),
    ).fetchall()
    result = []
    for row in rows:
        price = number(row["auction_price"])
        if price is None or price <= 0:
            continue
        result.append(
            {
                "trade_date": trade_date,
                "code": code,
                "time": hhmmss(row["auction_time"]),
                "open": None,
                "close": price,
                "high": None,
                "low": None,
                "price": price,
                "change_pct": number(row["open_change"]),
                "volume": integer(row["auction_volume"]),
                "amount": number(row["auction_amount"], 0.0),
                "avg_price": None,
                "volume_ratio": number(row["volume_ratio"]),
                "source": "local_auction_final_only",
            }
        )
    return result


def load_kline(connection: sqlite3.Connection, code: str, trade_date: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT trade_date, open, close, high, low, volume, amount, turnover,
               change_pct, prev_close, source
        FROM stock_kline
        WHERE code = ? AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT 140
        """,
        (code, trade_date),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def load_limit_history(connection: sqlite3.Connection, code: str, trade_date: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT trade_date, consecutive_days, limit_up_time, break_count, turnover
        FROM limit_up_pool
        WHERE code = ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (code, trade_date),
    ).fetchall()
    return [dict(row) for row in rows]


def first_at_or_after(rows: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
    return next((row for row in rows if str(row["time"]) >= marker), None)


def first_threshold(rows: list[dict[str, Any]], threshold: float) -> str:
    row = next(
        (
            item
            for item in rows
            if item.get("change_pct") is not None and float(item["change_pct"]) >= threshold
        ),
        None,
    )
    return str(row["time"]) if row else ""


def extrema(rows: list[dict[str, Any]], mode: str) -> tuple[float | None, str]:
    valid = [row for row in rows if row.get("change_pct") is not None]
    if not valid:
        return None, ""
    chosen = min(valid, key=lambda row: row["change_pct"]) if mode == "min" else max(
        valid, key=lambda row: row["change_pct"]
    )
    return number(chosen["change_pct"]), str(chosen["time"])


def recent_limit_memory(
    kline: list[dict[str, Any]],
    limit_history: list[dict[str, Any]],
    trade_date: str,
) -> dict[str, Any]:
    by_date = {str(row["trade_date"]): index for index, row in enumerate(kline)}
    today_index = by_date.get(trade_date)
    prior = [row for row in limit_history if str(row["trade_date"]) < trade_date]
    last = prior[-1] if prior else None
    result = {
        "prior_limit_date": "",
        "prior_limit_consecutive": 0,
        "gap_trade_sessions": None,
        "anchor_to_prev_close_pct": None,
        "anchor_max_low_drawdown_pct": None,
        "limit_count_prior_10_sessions": 0,
    }
    if today_index is not None:
        start = max(0, today_index - 10)
        recent_dates = {str(row["trade_date"]) for row in kline[start:today_index]}
        result["limit_count_prior_10_sessions"] = sum(
            1 for row in prior if str(row["trade_date"]) in recent_dates
        )
    if last is None:
        return result
    prior_date = str(last["trade_date"])
    result["prior_limit_date"] = prior_date
    result["prior_limit_consecutive"] = integer(last["consecutive_days"])
    prior_index = by_date.get(prior_date)
    if today_index is None or prior_index is None:
        return result
    result["gap_trade_sessions"] = max(0, today_index - prior_index - 1)
    anchor_close = number(kline[prior_index].get("close"))
    previous_close = number(kline[today_index - 1].get("close")) if today_index > 0 else None
    result["anchor_to_prev_close_pct"] = pct(previous_close, anchor_close)
    interim = kline[prior_index + 1 : today_index]
    lows = [number(row.get("low")) for row in interim]
    lows = [value for value in lows if value is not None]
    if lows and anchor_close:
        result["anchor_max_low_drawdown_pct"] = pct(min(lows), anchor_close)
    return result


def lifecycle_label(current_consecutive: int, memory: dict[str, Any]) -> str:
    if current_consecutive >= 2:
        return "continuous_promotion"
    gap = memory.get("gap_trade_sessions")
    prior_consecutive = integer(memory.get("prior_limit_consecutive"))
    if gap is not None and gap <= 5 and prior_consecutive >= 3:
        return "highboard_break_relaunch"
    if gap is not None and gap <= 5 and prior_consecutive >= 1:
        return "recent_firstboard_relaunch"
    return "cold_or_long_gap_firstboard"


def build_feature(
    pool_row: dict[str, Any],
    minute_rows: list[dict[str, Any]],
    quote: dict[str, Any],
    auction_rows: list[dict[str, Any]],
    auction_status: str,
    kline: list[dict[str, Any]],
    limit_history: list[dict[str, Any]],
    trade_date: str,
) -> dict[str, Any]:
    prev_close = number(quote.get("prev_close"))
    official_open = number(quote.get("official_open"))
    official_low = number(quote.get("official_low"))
    official_high = number(quote.get("official_high"))
    open_pct = pct(official_open, prev_close)
    low_pct = pct(official_low, prev_close)
    high_pct = pct(official_high, prev_close)
    minute_low_pct, minute_low_time = extrema(minute_rows, "min")
    minute_high_pct, minute_high_time = extrema(minute_rows, "max")
    memory = recent_limit_memory(kline, limit_history, trade_date)
    seal_time = hhmmss(pool_row.get("limit_up_time"))

    auction_valid = [
        row
        for row in auction_rows
        if row.get("price") is not None and number(row.get("price"), 0.0) > 0
    ]
    auction_pre = [row for row in auction_valid if str(row["time"]) < "092500"]
    auction_final = next(
        (row for row in reversed(auction_valid) if str(row["time"]) >= "092500"),
        auction_valid[-1] if auction_valid else None,
    )
    auction_min_pct = min(
        (number(row.get("change_pct")) for row in auction_pre if row.get("change_pct") is not None),
        default=None,
    )
    auction_start_pct = number(auction_pre[0].get("change_pct")) if auction_pre else None
    auction_final_pct = number(auction_final.get("change_pct")) if auction_final else open_pct
    auction_recovery = (
        round(auction_final_pct - auction_min_pct, 4)
        if auction_final_pct is not None and auction_min_pct is not None
        else None
    )

    tags: list[str] = []
    if open_pct is not None:
        if open_pct <= -5:
            tags.append("extreme_negative_open")
        elif open_pct < -1:
            tags.append("negative_open")
        elif open_pct <= 1:
            tags.append("zero_axis_open")
        elif open_pct < 3:
            tags.append("moderate_open")
        else:
            tags.append("high_open")
    if auction_min_pct is not None and auction_min_pct <= -8 and (auction_final_pct or -99) >= -1:
        tags.append("auction_limitdown_recovery")
    if low_pct is not None and low_pct <= -3:
        tags.append("intraday_deep_wash")
    elif low_pct is not None and low_pct < 0:
        tags.append("intraday_underwater")
    else:
        tags.append("no_underwater")
    if seal_time <= "093500":
        tags.append("instant_seal")
    elif seal_time <= "100000":
        tags.append("early_seal")
    elif seal_time <= "113000":
        tags.append("morning_seal")
    else:
        tags.append("afternoon_seal")
    if integer(pool_row.get("break_count")) > 0:
        tags.append("opened_board")
    lifecycle = lifecycle_label(integer(pool_row.get("consecutive_days")), memory)
    tags.append(lifecycle)

    feature: dict[str, Any] = {
        "trade_date": trade_date,
        "code": pool_row["code"],
        "name": pool_row.get("name", ""),
        "board_type": pool_row.get("board_type", ""),
        "board_tag": pool_row.get("board_tag", ""),
        "is_st": integer(pool_row.get("is_st")),
        "consecutive_days": integer(pool_row.get("consecutive_days")),
        "limit_up_time": seal_time,
        "break_count": integer(pool_row.get("break_count")),
        "turnover_pct": pool_row.get("turnover_pct"),
        "limit_up_reason": pool_row.get("limit_up_reason", ""),
        "prev_close": prev_close,
        "official_open": official_open,
        "open_pct": open_pct,
        "official_low": official_low,
        "low_pct": low_pct,
        "official_high": official_high,
        "high_pct": high_pct,
        "minute_low_pct": minute_low_pct,
        "minute_low_time": minute_low_time,
        "minute_high_pct": minute_high_pct,
        "minute_high_time": minute_high_time,
        "auction_status": auction_status,
        "auction_point_count": len(auction_valid),
        "auction_start_pct": auction_start_pct,
        "auction_min_pct": auction_min_pct,
        "auction_final_pct": auction_final_pct,
        "auction_recovery_pct_points": auction_recovery,
        "first_cross_0_time": first_threshold(minute_rows, 0.0),
        "first_cross_2_time": first_threshold(minute_rows, 2.0),
        "first_cross_5_time": first_threshold(minute_rows, 5.0),
        "first_cross_8_time": first_threshold(minute_rows, 8.0),
        "minute_point_count": len(minute_rows),
        "kline_point_count": len(kline),
        "lifecycle": lifecycle,
        "pattern_tags": "|".join(tags),
    }
    for marker in ("093000", "093500", "094500", "100000", "103000", "110000", "130000", "133000", "140000"):
        row = first_at_or_after(minute_rows, marker)
        feature["pct_" + marker[:4]] = number(row.get("change_pct")) if row else None
    feature.update(memory)
    return feature


def aggregate_report(features: list[dict[str, Any]]) -> dict[str, Any]:
    board_counts = Counter("firstboard" if integer(row["consecutive_days"]) == 1 else "promotion" for row in features)
    lifecycle_counts = Counter(str(row["lifecycle"]) for row in features)
    open_buckets = Counter()
    seal_buckets = Counter()
    tags = Counter()
    for row in features:
        open_pct = number(row.get("open_pct"))
        if open_pct is None:
            open_buckets["missing"] += 1
        elif open_pct < -1:
            open_buckets["below_-1"] += 1
        elif open_pct <= 1:
            open_buckets["-1_to_1"] += 1
        elif open_pct < 2.6:
            open_buckets["1_to_2.6"] += 1
        elif open_pct <= 6:
            open_buckets["2.6_to_6"] += 1
        else:
            open_buckets["above_6"] += 1
        seal = str(row.get("limit_up_time") or "")
        if seal <= "093500":
            seal_buckets["by_09:35"] += 1
        elif seal <= "100000":
            seal_buckets["09:35_to_10:00"] += 1
        elif seal <= "113000":
            seal_buckets["10:00_to_11:30"] += 1
        else:
            seal_buckets["afternoon"] += 1
        for tag in str(row.get("pattern_tags") or "").split("|"):
            if tag:
                tags[tag] += 1
    return {
        "sample_count": len(features),
        "board_counts": dict(board_counts),
        "open_buckets": dict(open_buckets),
        "seal_buckets": dict(seal_buckets),
        "lifecycle_counts": dict(lifecycle_counts),
        "tag_counts": dict(tags),
    }


def markdown_report(
    trade_date: str,
    manifest: dict[str, Any],
    aggregate: dict[str, Any],
    features: list[dict[str, Any]],
) -> str:
    examples = {"600127", "600121", "600479", "003040", "002721", "002295"}
    selected = [row for row in features if row["code"] in examples]
    session_label = (
        "上午连续竞价"
        if manifest.get("session_scope") == "morning"
        else "全日连续竞价"
    )
    lines = [
        "# " + trade_date + " 涨停全样本形态研究",
        "",
        "## 研究边界",
        "",
        "- 本文件是研究产物，不修改生产策略、阈值、订单、持仓或风控。",
        "- 样本为当日已涨停个股，属于结果条件样本；不能单独据此估计胜率或交易期望。",
        "- 分时价格来自腾讯公开分钟行情；竞价路径优先使用东方财富 iscr=1 非官方接口，失败时仅保留本地最终竞价快照。",
        "- 北交所按项目规则仅观察，不作为 A-F 自动交易阈值的调参样本。",
        "",
        "## 数据覆盖",
        "",
        "- 涨停样本数：" + str(manifest["pool_count"]),
        f"- 腾讯{session_label}完整个股数：" + str(manifest["tencent_success_count"]),
        "- 分时样本截止：" + str(manifest.get("session_end_time") or "未知"),
        "- 含完整竞价路径个股数：" + str(manifest["eastmoney_full_auction_count"]),
        "- 仅本地最终竞价快照个股数：" + str(manifest["local_final_auction_count"]),
        "- K 线有历史个股数：" + str(manifest["kline_success_count"]),
        "",
        "## 描述性统计",
        "",
        "- 首板/连板：" + json.dumps(aggregate["board_counts"], ensure_ascii=False),
        "- 开盘涨幅分桶：" + json.dumps(aggregate["open_buckets"], ensure_ascii=False),
        "- 首封时间分桶：" + json.dumps(aggregate["seal_buckets"], ensure_ascii=False),
        "- 生命周期：" + json.dumps(aggregate["lifecycle_counts"], ensure_ascii=False),
        "",
        "## 用户举例的六只股票",
        "",
        "|代码|名称|连板|开盘%|最低%|首封|生命周期|形态标签|",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for row in sorted(selected, key=lambda item: item["code"]):
        lines.append(
            "|{code}|{name}|{consecutive_days}|{open_pct}|{low_pct}|{limit_up_time}|{lifecycle}|{pattern_tags}|".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "## 当前可下结论与不可下结论",
            "",
            "1. 可描述当日赢家的开盘、下杀、回零轴、冲板和近期涨停记忆形态分布。",
            "2. 不可仅凭赢家样本认定某个开盘区间优于另一个区间，因为缺少同条件未涨停对照组。",
            "3. 不可用当日收盘后才知道的最低价、首封结果或 K 线形态反推 09:25 可交易信号。",
            "4. 下一步必须冻结时间切片，构造同开盘分桶、同板块记忆、同流动性的未涨停对照组，再回放 A-F 各层漏斗。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default=DEFAULT_DATE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--skip-eastmoney-auction", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--eastmoney-delay", type=float, default=0.7)
    args = parser.parse_args()

    trade_date = args.trade_date
    compact = trade_date.replace("-", "")
    output_dir = args.output_dir or ROOT / "outputs" / ("daily_review_" + compact)
    pool_path = output_dir / ("涨停池_" + compact + ".csv")
    if not pool_path.exists():
        raise SystemExit("missing frozen pool: " + str(pool_path))

    pool = load_pool(pool_path)
    cache_root = output_dir / ("raw_intraday_" + compact)
    tencent_cache = cache_root / "tencent"
    eastmoney_cache = cache_root / "eastmoney"
    connection = open_readonly_db(args.db)

    all_minutes: list[dict[str, Any]] = []
    all_auction: list[dict[str, Any]] = []
    all_kline_export: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    for index, pool_row in enumerate(pool):
        code = pool_row["code"]
        tencent_payload, tencent_status = get_tencent_payload(code, tencent_cache, args.refresh)
        if tencent_payload:
            minute_rows, quote = parse_tencent(code, trade_date, tencent_payload)
        else:
            minute_rows, quote = [], {}
        all_minutes.extend(minute_rows)

        auction_rows: list[dict[str, Any]] = []
        eastmoney_status = "skipped"
        if not args.skip_eastmoney_auction:
            eastmoney_payload, eastmoney_status = get_eastmoney_payload(
                code, eastmoney_cache, args.refresh
            )
            if eastmoney_payload:
                auction_rows = parse_eastmoney_auction(code, trade_date, eastmoney_payload)
            if index + 1 < len(pool):
                time.sleep(max(0.0, args.eastmoney_delay))
        if not auction_rows:
            local_rows = load_local_auction(connection, code, trade_date)
            auction_rows = local_rows
            auction_status = "local_final_only" if local_rows else eastmoney_status
        else:
            auction_status = "full_path"
        all_auction.extend(auction_rows)

        kline = load_kline(connection, code, trade_date)
        limit_history = load_limit_history(connection, code, trade_date)
        for row in kline:
            exported = {"code": code, "name": pool_row.get("name", "")}
            exported.update(row)
            all_kline_export.append(exported)

        feature = build_feature(
            pool_row,
            minute_rows,
            quote,
            auction_rows,
            auction_status,
            kline,
            limit_history,
            trade_date,
        )
        features.append(feature)
        statuses.append(
            {
                "code": code,
                "name": pool_row.get("name", ""),
                "tencent_status": tencent_status,
                "minute_point_count": len(minute_rows),
                "auction_status": auction_status,
                "auction_point_count": len(auction_rows),
                "eastmoney_status": eastmoney_status,
                "kline_point_count": len(kline),
            }
        )
        print(
            "{}/{} {} {} minute={} auction={} kline={}".format(
                index + 1,
                len(pool),
                code,
                pool_row.get("name", ""),
                len(minute_rows),
                len(auction_rows),
                len(kline),
            ),
            flush=True,
        )

    connection.close()

    minute_path = output_dir / ("涨停全样本分钟分时_" + compact + ".csv")
    auction_path = output_dir / ("涨停全样本竞价路径_" + compact + ".csv")
    kline_path = output_dir / ("涨停全样本K线_" + compact + ".csv")
    feature_path = output_dir / ("涨停全样本形态特征_" + compact + ".csv")
    manifest_path = output_dir / ("涨停全样本数据清单_" + compact + ".json")
    report_path = output_dir / ("涨停全样本形态研究_" + compact + ".md")

    write_csv(minute_path, all_minutes)
    write_csv(auction_path, all_auction)
    write_csv(kline_path, all_kline_export)
    write_csv(feature_path, features)

    aggregate = aggregate_report(features)
    latest_minute_time = max(
        (str(row.get("time") or "") for row in all_minutes),
        default="",
    )
    session_scope = "morning" if latest_minute_time and latest_minute_time <= "113000" else "full_day"
    minimum_complete_points = 121 if session_scope == "morning" else 240
    manifest = {
        "trade_date": trade_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_only": True,
        "pool_count": len(pool),
        "firstboard_count": sum(1 for row in pool if integer(row["consecutive_days"]) == 1),
        "promotion_count": sum(1 for row in pool if integer(row["consecutive_days"]) >= 2),
        "session_scope": session_scope,
        "session_end_time": latest_minute_time or None,
        "minimum_complete_minute_points": minimum_complete_points,
        "tencent_success_count": sum(
            1
            for row in statuses
            if row["minute_point_count"] >= minimum_complete_points
        ),
        "eastmoney_full_auction_count": sum(1 for row in statuses if row["auction_status"] == "full_path"),
        "local_final_auction_count": sum(1 for row in statuses if row["auction_status"] == "local_final_only"),
        "kline_success_count": sum(1 for row in statuses if row["kline_point_count"] > 0),
        "minute_row_count": len(all_minutes),
        "auction_row_count": len(all_auction),
        "kline_row_count": len(all_kline_export),
        "aggregate": aggregate,
        "status_by_code": statuses,
        "artifacts": [
            str(minute_path),
            str(auction_path),
            str(kline_path),
            str(feature_path),
            str(report_path),
        ],
    }
    fingerprint_payload = json.dumps(
        {
            "pool": pool,
            "features": features,
            "statuses": statuses,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    manifest["data_version_sha256"] = hashlib.sha256(fingerprint_payload).hexdigest()
    write_json(manifest_path, manifest)
    report_path.write_text(
        markdown_report(trade_date, manifest, aggregate, features),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
