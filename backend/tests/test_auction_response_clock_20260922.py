"""Per-response clocks: mocked transport, no real vendor calls or old frame repair."""
from datetime import datetime
import pytest
from sqlalchemy import select
from app.models.stock import AuctionData
from app.strategy import auction as module
from test_auction_source_frames_20260922 import maker, DAY

@pytest.mark.asyncio
async def test_eastmoney_early_response_survives_later_batch_window_overrun(maker, monkeypatch):
    at = [datetime(2026, 9, 22, 9, 25, 1)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at[0]
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_BATCH", 1)
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_PACE_SEC", 0)
    class Response:
        status_code = 200
        def __init__(self, code): self.code = code
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"diff": [{
                "f12": self.code, "f17": 10.5, "f18": 10, "f5": 200,
                "f6": 210000, "f10": 3,
                "f124": int(datetime(2026, 9, 22, 9, 25, 5).timestamp()),
            }]}}
    class Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def get(self, url, params=None, **kw):
            code = params["secids"].split(".")[1]
            at[0] = datetime(2026, 9, 22, 9, 25, 10 if code == "600001" else 32)
            return Response(code)
    monkeypatch.setattr("httpx.AsyncClient", Client)
    async with maker() as db:
        result = await module.AuctionCollector().collect_eastmoney_auction_evidence(
            db, DAY, codes=["600001", "600002"])
        rows = list((await db.scalars(select(AuctionData))).all())
        assert [row.code for row in rows] == ["600001"], "completed late batch must not erase early raw evidence"
        assert rows[0].received_at == datetime(2026, 9, 22, 9, 25, 10)
        assert rows[0].observed_at == datetime(2026, 9, 22, 9, 25, 10)
        assert rows[0].auction_time == "09:25:10"
        assert result["written"] == 1
        assert result["rejected"] == {"invalid_clock": 1}
