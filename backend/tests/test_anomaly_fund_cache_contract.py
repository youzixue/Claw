"""Cached classifications cannot resurrect expired or legacy unproven funds."""
from copy import deepcopy
from datetime import datetime

from app.api.v1 import tenbagger as api
from test_anomaly_fund_execution_contract import event, NOW, isolate


def test_cached_a1_is_revalidated_without_mutating_original_or_loading_later_funds(monkeypatch):
    anomaly = event()
    original = api._enrich_anomaly_display(anomaly, [anomaly])
    assert original["buy_point_pushable"]
    payload = {"anomalies": [original], "trade_date": str(NOW.date()),
               "snapshot_time": NOW.isoformat()}
    before = deepcopy(payload)
    class AfterClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(hour=18)
    monkeypatch.setattr(api, "datetime", AfterClose)
    projected = api._enrich_snapshot_payload(payload)
    invalid = projected["anomalies"][0]
    assert not invalid["buy_point_pushable"]
    assert not invalid["feishu_pushable"]
    assert invalid["setup_track"] == ""
    assert projected["summary"]["capital_count"] == 1  # an event is not a current buy
    assert payload == before
    assert invalid["detail"]["fund_signal_evidence"] == original["detail"]["fund_signal_evidence"]


def test_legacy_cached_a1_cannot_skip_evidence_gate():
    anomaly = event()
    del anomaly["detail"]["fund_signal_evidence"]
    anomaly["detail"].update(source="eastmoney_main_fund", is_stale=False,
                             main_net_inflow=600_000_000, main_net_inflow_pct=10)
    anomaly.update(setup_grade="A1 可直接执行", buy_point_reached=True,
                   buy_point_pushable=True, feishu_pushable=True, setup_track="趋势/资金型")
    before = deepcopy(anomaly)
    result = api._enrich_snapshot_payload({"anomalies": [anomaly]})["anomalies"][0]
    assert result["buy_point_reached"] is False
    assert result["feishu_pushable"] is False
    assert anomaly == before
