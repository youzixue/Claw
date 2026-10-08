"""Offline field replay; synthetic receipts/history are NOT natural acceptance."""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.stock import AuctionData, StockKline, StockTag
from app.strategy.auction import AuctionCollector
from app.data.auction_evidence import auction_evidence_status, volume_in_shares
from app.data.sources.tencent_source import TencentSource

FIXTURE = json.loads((Path(__file__).parent / "fixtures/auction_parsed_fields_20260929.json").read_text())
DAY = date(2026, 9, 29)
CODE = FIXTURE["code"]


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'offline.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def freeze_response_clock(monkeypatch, observed):
    import app.strategy.auction as auction
    import app.data.sources.tencent_source as tencent

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed

    monkeypatch.setattr(auction, "datetime", Clock)
    monkeypatch.setattr(tencent, "datetime", Clock)


def patch_frozen_transport(monkeypatch, sample, source, received):
    freeze_response_clock(monkeypatch, received)
    if source == "tencent":
        fields = ["0"] * 88
        fields[1], fields[2] = "浦发银行", CODE
        for index, value in sample[source].items():
            fields[int(index)] = value

        async def fetch(self, codes, *, client=None):
            return {CODE: fields}

        monkeypatch.setattr(TencentSource, "_fetch_batch", fetch)
        return

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"diff": [sample["eastmoney"]]}}

        @property
        def text(self):
            fields = ["0"] * 34
            fields[0] = "浦发银行"
            for index, value in sample["sina"].items():
                fields[int(index)] = value
            return 'var hq_str_sh600000="' + ",".join(fields) + '";'

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("httpx.AsyncClient", Client)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tencent", "sina"])
async def test_frozen_early_fields_build_real_collector_path_only_offline(maker, monkeypatch, source):
    collector = AuctionCollector()
    method = (collector.collect_tencent_auction_early_evidence if source == "tencent"
              else collector.collect_sina_auction_evidence)
    async with maker() as db:
        # Deliberately synthetic; this is no claim about this stock's actual daily history.
        db.add(StockTag(code=CODE, name="浦发银行", board_type="main_sh", board_tag="tradeable"))
        db.add_all([StockKline(code=CODE, trade_date=DAY-timedelta(days=i), volume=12_000_000)
                    for i in range(1, 6)])
        await db.commit()
        for sample in FIXTURE["samples"][:3]:
            observed = datetime.fromisoformat(sample["poll_start"]) + timedelta(seconds=1)
            patch_frozen_transport(monkeypatch, sample, source, observed)
            result = await method(db, DAY, now=observed, codes=[CODE])
            assert result["written"] == 1
            row = (await db.scalars(select(AuctionData).order_by(AuctionData.id.desc()))).first()
            shares = float(sample["sina"]["10"])
            assert row.auction_price == 9.13
            assert volume_in_shares(row.auction_volume, row.volume_unit) == shares
            assert row.auction_amount == round(9.13 * shares, 2)
            assert row.volume_ratio == round(shares / 12_000_000 * 20, 3)
            assert row.observed_at == row.received_at == observed
            assert auction_evidence_status(row, decision_at=observed) == "ok"
        rows = (await db.scalars(select(AuctionData))).all()
        assert sum(r.source_quote_at.time().isoformat() < "09:20:00" for r in rows) == 1
        cancel = [r for r in rows if "09:20:00" <= r.source_quote_at.time().isoformat() < "09:25:00"]
        assert len({r.source_quote_at for r in cancel}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tencent", "eastmoney"])
async def test_frozen_final_units_ratio_and_late_response_rejection(maker, monkeypatch, source):
    collector = AuctionCollector()
    method = (collector.collect_tencent_auction_evidence if source == "tencent"
              else collector.collect_eastmoney_auction_evidence)
    async with maker() as db:
        db.add(StockKline(code=CODE, trade_date=DAY-timedelta(days=1), volume=12_000_000))
        await db.commit()
        sample = FIXTURE["samples"][3]
        observed = datetime.fromisoformat(sample["poll_start"]) + timedelta(seconds=1)
        patch_frozen_transport(monkeypatch, sample, source, observed)
        result = await method(db, DAY, codes=[CODE])
        assert result["written"] == 1
        row = (await db.scalars(select(AuctionData))).one()
        assert row.auction_price == 9.15 and row.prev_close == 9.16
        assert volume_in_shares(row.auction_volume, row.volume_unit) == 214200
        assert row.auction_amount == 1959930.0
        assert row.volume_ratio == (0.357 if source == "tencent" else 0.75)
        assert row.source_quote_at == datetime(2026, 9, 29, 9, 25)
        # The next frozen sample was requested AFTER the deadline. It cannot
        # establish a timely second frame, even though its provider clock is 09:25:27.
        late = FIXTURE["samples"][4]
        late_observed = datetime.fromisoformat(late["poll_start"]) + timedelta(seconds=1)
        patch_frozen_transport(monkeypatch, late, source, late_observed)
        result = await method(db, DAY, codes=[CODE])
        assert result["status"] == "outside_evidence_window"
        assert (await db.scalars(select(AuctionData))).all() == [row]
