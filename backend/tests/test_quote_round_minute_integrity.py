"""分时复盘只使用实际归档轮次；不能伪造完整交易所分钟线或填补缺失VWAP。"""
from datetime import datetime, timedelta

import pandas as pd

from app.data.quote_round import QuoteRoundArchive, build_quote_round_record


def _round(at, price, vwap):
    records = [{
        "code": "600001", "name": "归档验证", "price": price, "avg_price": vwap,
        "volume": 100 + at.second, "amount": 100000 + at.second,
        "source_quote_at": at, "received_at": at,
    }]
    meta = build_quote_round_record(records, expected_count=1, committed_at=at)
    return meta, records


def _minute(root, at):
    path = root / "minute" / f"trade_date={at.date()}" / f"minute={at:%H%M}.parquet"
    return pd.read_parquet(path).iloc[0]


def test_more_than_three_rounds_out_of_order_and_repeated_tasks_preserve_samples(tmp_path):
    start = datetime(2026, 9, 8, 10)
    rounds = [_round(start + timedelta(seconds=i*10), price, 9+i/10)
              for i, price in enumerate([10, 13, 8, 11, 12])]
    archive = QuoteRoundArchive(tmp_path)
    for i in [3, 1, 4, 0, 2, 4]:
        archive.write_round(*rounds[i])
    result = _minute(tmp_path, start)
    assert (result["open"], result["high"], result["low"], result["close"]) == (10, 13, 8, 12)
    assert result["price_samples"] == 5
    assert result["avg_price"] == 9.4
    assert result["volume"] == 140
    assert result["price_basis"] == "sampled_quote_prices"
    assert result["volume_basis"] == "cumulative_session"


def test_restart_and_late_previous_minute_do_not_erase_either_minute(tmp_path):
    start = datetime(2026, 9, 8, 10)
    archive = QuoteRoundArchive(tmp_path)
    archive.write_round(*_round(start, 10, 9.8))
    archive.write_round(*_round(start + timedelta(minutes=1), 12, 10.2))
    restarted = QuoteRoundArchive(tmp_path)
    restarted.write_round(*_round(start + timedelta(seconds=30), 11, None))
    earlier, later = _minute(tmp_path, start), _minute(tmp_path, start + timedelta(minutes=1))
    assert earlier["open"] == 10 and earlier["close"] == 11 and earlier["price_samples"] == 2
    assert pd.isna(earlier["avg_price"])  # 最新缺失不能取较早的9.8
    assert later["open"] == 12 and later["close"] == 12 and later["price_samples"] == 1
    restored = restarted.restore_recent_batches(start.date())
    assert any(row["avg_price"] is None for batch in restored for row in batch)


def test_quote_metadata_supplies_commit_clock_even_when_source_rows_omit_it(tmp_path):
    at = datetime(2026, 9, 8, 10)
    archive = QuoteRoundArchive(tmp_path)
    result = archive.write_round(*_round(at, 10, None))
    compact = pd.read_parquet(result["archive_path"])
    assert compact.iloc[0]["committed_at"] == at.isoformat()
    assert _minute(tmp_path, at)["close"] == 10
