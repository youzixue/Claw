"""Direct production regressions; fixed output hashes captured before the merge."""
import asyncio
import hashlib
import json
from datetime import date,timedelta
import pytest
import app.signal.anomaly_scanner as scanner
import app.api.v1.tenbagger as radar
import app.data.main_fund as main_fund

def history():
    return scanner.AnomalyScanner._load_leader_history_features
def b1():
    return radar._enrich_stock_rows_with_b1
def encoded(value):return json.dumps(value,sort_keys=True,default=str,separators=(",",":")).encode()
class Rows:
    def __init__(self,rows=(),fail=False):self.rows=rows;self.fail=fail;self.closed=0;self.started=asyncio.Event()
    def all(self):return self.rows
    def scalars(self):return self
    async def partitions(self,size):
        assert size==2048
        for n in range(0,len(self.rows),size):
            self.started.set()
            if self.fail:raise ValueError("fetch failure")
            yield self.rows[n:n+size]
    async def close(self):self.closed+=1
class DB:
    def __init__(self,result,fail=False):self.result=result;self.calls=[];self.fail=fail
    async def execute(self,q):self.calls.append(q);return self.result
    async def stream(self,q):
        self.calls.append(q)
        assert q.get_execution_options()["yield_per"]==2048
        if self.fail:raise ValueError("query failure")
        return self.result
def bars():
    return [(str(code),date(2026,8,1)+timedelta(days=n),float(n+1) if n%7 else None,n+2.,max(0,n-.5)) for code in range(201) for n in range(26)]
@pytest.mark.asyncio
async def test_history_matches_all_fields_and_order():
    data=bars(); codes=list(dict.fromkeys(r[0] for r in data))
    rows=Rows(data);after=await history()(None,DB(rows),codes+codes[:3],date(2026,9,21))
    assert hashlib.sha256(encoded(after)).hexdigest()=="24f4a6ab5ee30b9b8006b3f2a5daeccd882ad8b8c13889e5a89564acc2e0cf76"
    assert list(after)==codes
    assert rows.closed==1
@pytest.mark.asyncio
@pytest.mark.parametrize("codes", [[],["a"]])
async def test_history_empty_cleanup(codes):
    result=Rows();db=DB(result)
    assert await history()(None,db,codes,date(2026,9,21))=={}
    assert result.closed==bool(codes)
    assert len(db.calls)==bool(codes)
@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["query","fetch","transform"])
async def test_history_errors_propagate_and_close(failure):
    rows=Rows([("x",)] if failure=="transform" else bars(),fail=failure=="fetch")
    db=DB(rows,fail=failure=="query")
    with pytest.raises(ValueError):
        await history()(None,db,["a"],date(2026,9,21))
    assert rows.closed==(failure!="query")
@pytest.mark.asyncio
async def test_history_cancel_closes_stream():
    rows=Rows(bars());task=asyncio.create_task(history()(None,DB(rows),["a"],date(2026,9,21)))
    await rows.started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert rows.closed==1
@pytest.mark.asyncio
async def test_history_allows_competing_task_between_batches():
    rows=Rows(bars());ticks=[];done=False
    async def observer():
        while not done:
            ticks.append(1);await asyncio.sleep(0)
    watcher=asyncio.create_task(observer())
    try:await history()(None,DB(rows),["a"],date(2026,9,21))
    finally:done=True;await watcher
    assert len(ticks)>=3

class B1DB:
    def __init__(self,codes,fail=False):
        days=[date(2026,6,1)+timedelta(days=i) for i in range(65)]
        self.outputs=[Rows([(d,) for d in days]),Rows([(code,d,10.,11.,9.,10.,1000.,1.,0.) for code in codes for d in days]),Rows(),Rows()]
        self.fail=fail
    async def execute(self,q):
        if self.fail:raise ValueError("read failure")
        return self.outputs.pop(0)
@pytest.fixture
def b1_inputs(monkeypatch):
    async def funds(*args,**kwargs):return {}
    monkeypatch.setattr(main_fund,"load_current_main_fund_map",funds)
    monkeypatch.setattr(radar,"_cached_b1_analysis",lambda code,bars:{})
    codes=[str(n) for n in range(33)]
    return codes,[dict(code=c,detail={}) for c in codes]
@pytest.mark.asyncio
async def test_b1_exact_fields_order_and_input_untouched(b1_inputs):
    codes,rows=b1_inputs;original=encoded(rows)
    after=await b1()(rows,B1DB(codes),date(2026,9,21))
    assert hashlib.sha256(encoded(after)).hexdigest()=="616e76498fc09818bf94a08ae3af088f5d9f06d59c91c60ccff404b2d2a4c0c5"
    assert [row["code"] for row in after]==codes
    assert original==encoded(rows)
@pytest.mark.asyncio
@pytest.mark.parametrize("rows",[[],[{"code":""}]])
async def test_b1_empty_no_db(rows):
    assert await b1()(rows,None,date(2026,9,21)) is rows
@pytest.mark.asyncio
async def test_b1_error_no_swallow(b1_inputs):
    codes,rows=b1_inputs
    with pytest.raises(ValueError,match="read failure"):await b1()(rows,B1DB(codes,True),date(2026,9,21))
@pytest.mark.asyncio
async def test_b1_cancel_cooperatively_propagates(b1_inputs):
    codes,rows=b1_inputs
    task=asyncio.create_task(b1()(rows,B1DB(codes),date(2026,9,21)))
    await asyncio.sleep(0);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
@pytest.mark.asyncio
async def test_b1_allows_competing_task_between_batches(b1_inputs):
    codes,rows=b1_inputs;ticks=[];done=False
    async def observer():
        while not done:ticks.append(1);await asyncio.sleep(0)
    watcher=asyncio.create_task(observer())
    try:await b1()(rows,B1DB(codes),date(2026,9,21))
    finally:done=True;await watcher
    assert len(ticks)>=5
