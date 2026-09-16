"""Explicit, isolated auction provenance fixtures; never applied to real rows."""
from datetime import datetime, time


def verified_auction_fields(trade_date, auction_time):
    observed = datetime.combine(trade_date, time.fromisoformat(auction_time))
    final = observed.time() >= time(9, 25)
    return {
        "source": "test_fixture",
        "source_version": "test_auction_matched_v1",
        "source_quote_at": observed,
        "received_at": observed,
        "observed_at": observed,
        "price_basis": "auction_opening" if final else "indicative_match",
        "volume_basis": "auction_matched" if final else "indicative_matched",
        "volume_unit": "share",
        "amount_unit": "CNY",
    }
