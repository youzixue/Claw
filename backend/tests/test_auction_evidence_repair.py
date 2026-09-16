"""Pure malformed-input regression tests; no real source or production session."""
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from app.data.auction_evidence import (
    auction_evidence_status, positive_number, volume_in_shares,
)
from auction_test_evidence import verified_auction_fields

DAY = date(2026, 9, 15)
DECISION = datetime(2026, 9, 15, 9, 26)


def evidence_row(**changes):
    values = dict(
        code="000001", trade_date=DAY, auction_time="09:25:00",
        auction_price=10.5, prev_close=10, auction_volume=100_000,
        auction_amount=1_050_000, volume_ratio=2,
        **verified_auction_fields(DAY, "09:25:00"),
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("value", [True, np.bool_(True), np.bool_(False)])
def test_boolean_is_never_matched_quantity(value):
    assert not positive_number(value)
    assert volume_in_shares(value, "share") is None
    assert auction_evidence_status(
        evidence_row(auction_volume=value), decision_at=DECISION,
    ) == "incomplete_values"


@pytest.mark.parametrize("unit", [[], {}, ["share"], True, None, "hands"])
def test_bad_unit_degrades_instead_of_crashing_audit(unit):
    assert volume_in_shares(100, unit) is None
    assert auction_evidence_status(
        evidence_row(volume_unit=unit), decision_at=DECISION,
    ) == "unknown_unit"


def test_lot_conversion_overflow_cannot_pass_single_frame_gate():
    assert volume_in_shares(1e308, "lot100") is None
    assert auction_evidence_status(
        evidence_row(auction_volume=1e308, volume_unit="lot100"),
        decision_at=DECISION,
    ) == "incomplete_values"


@pytest.mark.parametrize("unit,quantity", [("share", 10_000), ("lot100", 100)])
def test_equivalent_declared_quantities_stay_valid(unit, quantity):
    assert volume_in_shares(quantity, unit) == 10_000
    assert auction_evidence_status(
        evidence_row(auction_volume=quantity, volume_unit=unit),
        decision_at=DECISION,
    ) == "ok"


def test_unknown_spot_basis_and_missing_clock_are_not_repaired_to_ok():
    assert auction_evidence_status(evidence_row(
        source_quote_at=None, price_basis="spot_open_unverified",
        volume_basis="intraday_cumulative",
    ), decision_at=DECISION) == "unknown"
    assert auction_evidence_status(evidence_row(
        price_basis="spot_open_unverified", volume_basis="intraday_cumulative",
    ), decision_at=DECISION) == "unverified_basis"
