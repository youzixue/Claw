"""Intra-call numeric reuse: exact outputs, short-circuiting, ties and no stale cache."""
import datetime as dt, hashlib, inspect, json, math, pickle, random
from pathlib import Path
import pytest
from app.api.v1 import promotion as p
OUT=Path(__file__).resolve().parent
FIXTURE=OUT/"fixtures/burst_meta_v2.txt"
HELPERS=("_safe_float","_safe_percent_change","_clamp_score","_inverse_score","_range_score","_mean","_default_burst_pullback_restart_meta")
NAME="_build_burst_pullback_restart_meta"
observations=[]
def _source(mode):
    if mode=="old":
        raw=FIXTURE.read_bytes()
        assert hashlib.sha256(raw).hexdigest()=="302d7de90ff9aaccd29519ee9525831377c127460a65d06423c12ed202a990d8"
        return raw.decode()
    assert mode=="candidate"
    return inspect.getsource(p._build_burst_pullback_restart_meta)
def _scope():
    scope=dict(vars(p))
    for name in HELPERS:
        exec(compile(inspect.getsource(getattr(p,name)),str(Path(p.__file__)),"exec"),scope)
    return scope
def load(mode):
    scope=_scope()
    calls={"safe_float":0,"mean":0}
    for name,key in (("_safe_float","safe_float"),("_mean","mean")):
        real=scope[name]
        def counted(*args,_real=real,_key=key,**kwargs):
            calls[_key]+=1
            return _real(*args,**kwargs)
        scope[name]=counted
    exec(compile(_source(mode),"<burst-"+mode+">","exec"),scope)
    return scope[NAME],calls
def invoke(fn,bars):
    try:return ("value",json.dumps(fn(bars),sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=True))
    except Exception as e:return ("error",type(e).__name__,str(e))
def compare(bars,label=""):
    # Pickle snapshot retains NaN, signed zero, field order and immutable scalar types.
    snapshot=pickle.dumps(bars)
    old,oc=load("old");new,nc=load("candidate")
    a=invoke(old,bars);assert pickle.dumps(bars)==snapshot
    b=invoke(new,bars);assert pickle.dumps(bars)==snapshot
    assert a==b,(label,a,b)
    assert nc["safe_float"]<=oc["safe_float"],(label,oc,nc)
    observations.append({"label":label,"bars":len(bars or []),"old":oc.copy(),"candidate":nc.copy(),"outcome":a})
    return a,oc,nc

def base(n):
    return [{"trade_date":dt.date(2026,1,1)+dt.timedelta(days=i),"close":10.0,"high":10.2,"low":9.8,"volume":100.0,"prev_close":10.0,"change_pct":0.0} for i in range(n)]
def burst(n=55):
    bars=base(n)
    if n<18:return bars
    idx=max(10,n-22)
    bars[idx].update(close=10.8,high=11.5,low=9.9,volume=600.0,prev_close=10.,change_pct=8.0)
    for row in bars[idx+1:-4]:
        row.update(close=9.5,high=10.,low=9.,volume=80.)
    bars[-7]["low"]=8.8
    for row in bars[-4:]:row.update(close=10.5,high=10.7,low=10.2,volume=150.,change_pct=1.)
    return bars

@pytest.mark.parametrize("n",[0,17,18,55,260])
@pytest.mark.parametrize("builder",[base,burst])
def test_required_lengths(n,builder):
    compare(builder(n),f"length:{builder.__name__}:{n}")
def test_none():compare(None,"None")
@pytest.mark.parametrize("bad",[None,"", "bad", float("nan"),float("inf"),-float("inf"),-0.0,0.,-1.,{},[],True,False," 10.5 ","NaN","inf","-0"])
@pytest.mark.parametrize("field",["close","high","low","volume","prev_close","change_pct"])
def test_mixed_values(field,bad):
    bars=burst()
    for idx in (0,10,33,48,54):bars[idx][field]=bad
    compare(bars,f"mixed:{field}:{repr(bad)}")
def test_short_circuit_and_uncaught_overflow():
    huge=10**10000
    bars=burst()
    # Original never visits high when close is invalid.
    bars[2].update(close=0,high=huge)
    compare(bars,"shortcircuit")
    bars[2]["close"]=1.
    outcome,_,_=compare(bars,"uncaught_float_overflow")
    assert outcome[0:2]==("error","OverflowError")
@pytest.mark.parametrize("missing",["close","high","low","volume","prev_close","change_pct","trade_date"])
def test_missing(missing):
    bars=burst()
    for row in bars:row.pop(missing,None)
    compare(bars,"missing:"+missing)
@pytest.mark.parametrize("kind",["string","int","mixed"])
def test_numeric_encodings(kind):
    bars=burst()
    for i,row in enumerate(bars):
        for field in ("close","high","low","volume","prev_close","change_pct"):
            value=row[field]
            if kind=="string" or kind=="mixed" and i%2:row[field]=repr(value)
            elif kind=="int":row[field]=int(value)
    compare(bars,"encoding:"+kind)

# Deterministic grid at gates and one ULP each side; selected from source constants,
# not chosen by which output wins.
BOUNDARIES=[("volume",300.),("high",10.5),("change_pct",5.),("latest_change",9.2),
            ("low",11.5/1.10),("low",11.5/1.34),("low",11.5/1.12),
            ("low",11.5/1.28),("window_volume",348.),("latest_close",8.8*1.06),
            ("latest_close",11.5/1.24)]
@pytest.mark.parametrize("field,value",BOUNDARIES)
@pytest.mark.parametrize("direction",[-1,0,1])
def test_boundaries(field,value,direction):
    value=math.nextafter(value,-math.inf if direction<0 else math.inf) if direction else value
    bars=burst(55);idx=33
    if field=="latest_change":bars[-1]["change_pct"]=value
    elif field=="latest_close":bars[-1]["close"]=value
    elif field=="low":bars[-7]["low"]=value
    elif field=="window_volume":
        for row in bars[-8:-4]:row["volume"]=value
    else:bars[idx][field]=value
    compare(bars,f"boundary:{field}:{value.hex()}")
def test_low_tie_and_date_types():
    bars=burst();bars[-8]["low"]=bars[-7]["low"]
    out,_,_=compare(bars,"low_tie")
    result=json.loads(out[1]);assert result["burst_restart_days"]==7
    for value in (None,"",0,"2026-02-03",dt.datetime(2026,2,3,10,2)):
        bars[33]["trade_date"]=value
        compare(bars,"date:"+str(value))
def test_burst_score_tie():
    bars=base(55)
    for idx in (20,25):
        bars[idx].update(high=11.5,close=10.8,volume=600.,change_pct=8.)
    for row in bars[26:]:row.update(volume=80.,low=9.,close=10.)
    bars[45]["low"]=8.8
    bars[-1].update(close=10.5,volume=150.,change_pct=1.)
    # Capture real helper clamp outputs, never mock the score.
    for mode in ("old","candidate"):
        scope=_scope()
        scores=[];real=scope["_clamp_score"]
        def track(v):
            result=real(v);scores.append(result);return result
        scope["_clamp_score"]=track
        exec(_source(mode),scope)
        result=scope[NAME](bars)
        assert result["burst_date"]==bars[20]["trade_date"].isoformat()
        assert scores.count(result["burst_pullback_score"])>=2
    compare(bars,"burst_score_tie")
def test_fixed_seed_corpus():
    rng=random.Random(20260924)
    for case in range(160):
        n=rng.choice([17,18,19,55,56,120,260]);bars=base(n)
        for row in bars:
            close=rng.uniform(2.,40.)
            row.update(close=close,high=close*rng.uniform(1.,1.25),low=close*rng.uniform(.65,1.),
                       volume=rng.choice([20.,80.,100.,300.,600.,1500.]),
                       prev_close=close*rng.uniform(.8,1.1),change_pct=rng.choice([0.,4.99,5.,8.,9.2]))
        if case%3==0:bars[rng.randrange(n)][rng.choice(["close","high","low","volume"])]=rng.choice([None,"bad",float("nan"),float("inf"),-0.])
        compare(bars,f"seed:{case}")
def test_repeat_call_no_stale_state():
    old,oc=load("old");new,nc=load("candidate")
    bars=burst();first=invoke(new,bars)
    assert first==invoke(old,bars)
    for value in (30.,80.,300.,600.,1500.):
        bars[-1]["volume"]=value
        assert invoke(new,bars)==invoke(old,bars)
    assert invoke(new,burst())==first
def test_production_entrypoint_and_baseline_identity():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest()=="302d7de90ff9aaccd29519ee9525831377c127460a65d06423c12ed202a990d8"
    fn,_=load("candidate")
    for bars in (None,base(17),base(18),burst(),burst(260)):
        assert invoke(fn,bars)==invoke(p._build_burst_pullback_restart_meta,bars)
