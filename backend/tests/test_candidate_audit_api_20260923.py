"""Bounded keyset pagination and exact old-reference lookup on existing API."""
from datetime import timedelta
import pytest
from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog
from test_paper_api import paper_client
from test_promotion_candidate_trace_20260923 import candidate_clock, AT, DAY


@pytest.mark.asyncio
async def test_more_than_500_rows_old_ids_and_account_isolation(paper_client,candidate_clock):
    client,maker=paper_client
    async with maker() as db:
        account=await paper._get_or_create_account(db,"promotion")
        other=await paper._get_or_create_account(db,"mainline")
        rows=[PaperAutoTradeLog(account_id=account.id,run_id="big-scan",trade_date=DAY,
            created_at=AT-timedelta(seconds=i//3),code=f"600{i%10:03d}",name="isolated",
            action="candidate_audit",decision="rejected",reason_code="candidate_watch_only",reason="frozen reason")
            for i in range(603)]
        foreign=PaperAutoTradeLog(account_id=other.id,run_id="other-scan",trade_date=DAY,
            created_at=AT,code="600999",action="candidate_audit",decision="rejected",reason="other account")
        db.add_all([*rows,foreign]);await db.commit()
        ids={r.id for r in rows}; oldest=rows[-1].id
    params={"account_name":"promotion","limit":500}
    first=(await client.get("/paper/auto/logs",params=params)).json()
    assert len(first["logs"])==500 and first["has_more"] is True
    second=(await client.get("/paper/auto/logs",params={**params,**first["next_cursor"]})).json()
    assert len(second["logs"])==103 and second["has_more"] is False and second["next_cursor"] is None
    assert {r["id"] for r in first["logs"]}.isdisjoint({r["id"] for r in second["logs"]})
    assert {r["id"] for page in (first,second) for r in page["logs"]}==ids
    exact=(await client.get("/paper/auto/logs",params={"account_name":"promotion","log_ids":f"{oldest},{foreign.id}"})).json()
    assert [r["id"] for r in exact["logs"]]==[oldest]
    filtered=(await client.get("/paper/auto/logs",params={"account_name":"promotion","code":"600002","run_id":"big-scan"})).json()
    assert len(filtered["logs"])==61 and all(r["code"]=="600002" for r in filtered["logs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"before_id":1}, {"before_created_at":AT.isoformat()},
    {"before_id":1,"before_created_at":AT.isoformat()+"+08:00"},
    {"log_ids":"-1"},{"log_ids":"bad"},{"log_ids":",".join(str(i) for i in range(1,502))},
])
async def test_invalid_cursor_or_id_lookup_is_not_silently_ignored(paper_client,params):
    client,_=paper_client
    response=await client.get("/paper/auto/logs",params=params)
    assert response.status_code in {400,422}
