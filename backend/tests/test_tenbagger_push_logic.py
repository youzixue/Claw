from datetime import date, timedelta

import pytest

import app.api.v1.tenbagger as tenbagger_module
import app.api.v1.ws as ws_module
from app.push.channels.base import PushMessage
from app.api.v1.tenbagger import (
    _build_anomaly_push_message,
    _build_early_observation_push_message,
    _build_b1_state_push_message,
    _cap_automatic_push_messages,
    _build_push_message_candidates,
    _build_push_messages_for_anomalies,
    _enrich_anomaly_display,
    _resolve_anomaly_current_fund_context,
    _resolve_anomaly_trade_date,
    _resolve_b1_push_eligibility,
    _select_b1_state_changes,
    _select_pushworthy_anomalies,
    _select_unsent_observation_recovery_messages,
    _anomaly_signal_identity,
    refresh_and_push_anomaly_snapshot,
)


def _add_fund_provenance(anomaly: dict, *, provider: str = "eastmoney") -> dict:
    """Give synthetic signal fixtures explicit legacy-flat provider evidence."""
    clock = tenbagger_module.datetime.now().replace(microsecond=0)
    anomaly["detail"].update({
        "code": anomaly["code"],
        "trade_date": clock.date().isoformat(),
        "provider_source": provider,
        "source_version": (
            "tencent_hsfundtab_v1" if provider == "tencent" else "individual_fund_flow_v3_f124"
        ),
        "source_quote_at": clock.isoformat(),
        "received_at": clock.isoformat(),
        "observed_at": clock.isoformat(),
    })
    return anomaly


def _make_capital_anomaly(
    code: str,
    name: str,
    *,
    score: float = 88,
    source: str = "eastmoney_main_fund",
    is_stale: bool = False,
    change_pct: float = 3.8,
    volume_ratio: float = 2.4,
    support_strength_score: float = 72,
    main_net_inflow_pct: float = 9.2,
    main_net_inflow: float = 600_000_000,
) -> dict:
    anomaly = {
        "code": code,
        "name": name,
        "event_type": "capital",
        "level": "critical",
        "score": score,
        "description": f"资金净额 {main_net_inflow / 1e8:.1f}亿",
        "detail": {
            "price": 16.8,
            "avg_price": 16.7,
            "main_net_inflow": main_net_inflow,
            "main_net_inflow_pct": main_net_inflow_pct,
            "super_net_inflow": 230_000_000,
            "super_net_inflow_pct": 3.6,
            "big_net_inflow": 170_000_000,
            "big_net_inflow_pct": 2.8,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "source": source,
            "as_of": "2026-04-15 10:30:00",
            "is_stale": is_stale,
            "capital_anomaly_type": "main_inflow",
            "bid_depth_5": 52000,
            "ask_depth_5": 26000,
            "orderbook_imbalance": 0.33,
            "withdrawal_ratio": 0.04,
            "support_strength_score": support_strength_score,
            "turnover": 8.5,
            "amplitude": 4.2,
            "volume": 9_000_000,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 12.5, "change_pct": 1.8, "strength_score": 72}],
        },
    }

    return _add_fund_provenance(anomaly, provider="tencent" if source == "fund_flow" else "eastmoney")


def _make_breakthrough_anomaly(
    code: str,
    name: str,
    *,
    score: float = 83,
    quality_score: float = 85,
    change_pct: float = 4.8,
    volume_ratio: float = 2.0,
    support_strength_score: float = 62,
    days_near_pressure: int = 4,
    pullback_probability: str = "low",
    is_false_breakout: bool = False,
) -> dict:
    return {
        "code": code,
        "name": name,
        "event_type": "breakthrough",
        "level": "major",
        "score": score,
        "description": "突破平台",
        "detail": {
            "price": 16.8,
            "avg_price": 16.7,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "amplitude": 4.6,
            "quality_score": quality_score,
            "ma_status": "multi_long",
            "pullback_probability": pullback_probability,
            "days_near_pressure": days_near_pressure,
            "support_strength_score": support_strength_score,
            "orderbook_imbalance": 0.10,
            "bid_depth_5": 34000,
            "ask_depth_5": 24000,
            "is_false_breakout": is_false_breakout,
            "as_of": "2026-04-15 10:21:00",
            "turnover": 11.0,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 12.2, "change_pct": 1.8, "strength_score": 72}],
        },
    }


def _make_event_relay_anomaly() -> dict:
    return {
        "code": "000537",
        "name": "绿发电力",
        "event_type": "breakthrough",
        "level": "major",
        "score": 88,
        "description": "重大事件接力二次确认",
        "detail": {
            "signal_type": "event_relay_confirmation",
            "signal_label": "重大事件接力二次确认",
            "event_relay_confirmed": True,
            "event_score": 63,
            "event_title": "拟投资建设46万千瓦风电项目",
            "trend_setup_confirmed": True,
            "trend_driver_watchlist": True,
            "detection_pool_member": True,
            "quality_score": 88,
            "price": 8.50,
            "avg_price": 8.45,
            "vwap_reclaimed": True,
            "change_pct": 2.5,
            "volume_ratio": 1.6,
            "turnover": 4.5,
            "amplitude": 3.0,
            "support_strength_score": 72,
            "orderbook_imbalance": 0.15,
            "bid_depth_5": 180_000,
            "ask_depth_5": 100_000,
            "main_net_inflow": 100_000_000,
            "main_net_inflow_pct": 5.0,
            "ma_status": "multi_long",
            "pullback_probability": "low",
            "days_near_pressure": 2,
            "is_false_breakout": False,
            "as_of": "2026-08-11 09:36:00",
            "event_relay_confirmations": [
                "竞价涨幅处于0.5%~3.5%健康区间",
                "竞价成交量较近5日均值放大",
                "未跌破前一交易日涨停价",
                "价格站稳VWAP",
                "量比处于1.15~3.2健康区间",
                "电力板块维持前排共振",
            ],
            "sector_factors": [{
                "sector_name": "电力",
                "fund_flow": 8.0,
                "change_pct": 1.6,
                "strength_score": 75,
                "limit_up_count": 4,
                "peer_count": 3,
            }],
        },
    }


def test_event_relay_confirmation_reaches_a2_feishu_message_builder():
    anomaly = _make_event_relay_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    messages = _build_push_messages_for_anomalies(
        selected,
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[anomaly],
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(messages) == 1
    assert messages[0].extra["alert_tier"] == "light"
    assert messages[0].extra["signal_variant"] == "event_relay_confirmation"
    assert "重大事件接力二次确认" in messages[0].title
    assert "拟投资建设46万千瓦风电项目" in messages[0].content


def test_second_board_relay_confirmation_reaches_a2_feishu_message_builder():
    anomaly = _make_event_relay_anomaly()
    detail = anomaly["detail"]
    anomaly["description"] = "首板冲二板二次确认"
    detail.update({
        "signal_type": "second_board_relay_confirmation",
        "signal_label": "首板冲二板二次确认",
        "event_score": 0,
        "event_title": "",
        "relay_quality_score": 86,
        "event_relay_source": "second_board_relay",
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    messages = _build_push_messages_for_anomalies(
        selected,
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[anomaly],
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(messages) == 1
    assert messages[0].extra["signal_variant"] == "second_board_relay_confirmation"
    assert "首板冲二板二次确认" in messages[0].title


def test_leader_linkage_confirmation_reaches_a2_feishu_message_builder():
    anomaly = {
        "code": "002607",
        "name": "中公教育",
        "event_type": "breakthrough",
        "level": "major",
        "score": 88,
        "description": "龙头映射补涨二次确认",
        "detail": {
            "signal_type": "leader_linkage_confirmation",
            "signal_label": "龙头映射补涨二次确认",
            "leader_linkage_confirmed": True,
            "leader_code": "003032",
            "leader_name": "传智教育",
            "leader_near_high": True,
            "leader_at_limit": True,
            "leader_recognition_score": 86,
            "linkage_score": 72,
            "business_relevance_score": 92,
            "follower_shape_score": 82,
            "theme_alignment_score": 90,
            "leader_driver_reason": "AI教育",
            "trend_setup_confirmed": True,
            "trend_driver_watchlist": True,
            "detection_pool_member": True,
            "quality_score": 88,
            "price": 4.12,
            "avg_price": 4.08,
            "vwap_reclaimed": True,
            "change_pct": 2.5,
            "volume_ratio": 1.6,
            "turnover": 4.5,
            "amplitude": 3.7,
            "support_strength_score": 74,
            "orderbook_imbalance": 0.15,
            "bid_depth_5": 180_000,
            "ask_depth_5": 100_000,
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 4.0,
            "as_of": "2026-08-11 10:05:00",
            "leader_linkage_confirmations": [
                "龙头传智教育封板稳定",
                "龙头传智教育贴近日内高点",
                "关联板块涨幅和资金保持正向",
                "B站回VWAP",
                "B量比1.60温和放大",
                "B盘口承接74",
            ],
            "sector_factors": [{
                "sector_name": "AI教育",
                "fund_flow": 6.0,
                "change_pct": 1.5,
                "strength_score": 75,
                "limit_up_count": 1,
                "peer_count": 1,
            }],
        },
    }

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    messages = _build_push_messages_for_anomalies(
        selected,
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[anomaly],
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(messages) == 1
    assert messages[0].extra["signal_variant"] == "leader_linkage_confirmation"
    assert "看传智教育做B二次确认" in messages[0].title
    assert "龙头传智教育封板稳定" in messages[0].content


def _make_sector_repair_anomaly(*, change_pct: float = 4.2) -> dict:
    return {
        "code": "002920",
        "name": "修复测试股",
        "event_type": "breakthrough",
        "level": "major",
        "score": 90,
        "description": "强修复板块低位启动",
        "detail": {
            "signal_type": "sector_repair_reversal",
            "signal_label": "强修复板块低位启动",
            "sector_repair_confirmed": True,
            "detection_pool_member": True,
            "watchlist_member": True,
            "quality_score": 90,
            "ma_status": "repair_reversal",
            "pullback_probability": "medium",
            "days_near_pressure": 0,
            "is_false_breakout": False,
            "price": 10.42,
            "change_pct": change_pct,
            "volume_ratio": 1.5,
            "turnover": 3.2,
            "amplitude": 5.4,
            "return_20d": -24.0,
            "near_intraday_high": True,
            "vwap_reclaimed": True,
            "fundamental_confirmed": True,
            "sector_repair_confirmations": ["股价贴近日内高点", "重新站稳VWAP"],
            "sector_repair_driver": {
                "sector_name": "CPO",
                "change_pct": 4.8,
                "fund_flow": 42.0,
                "strength_score": 88,
                "limit_up_count": 7,
                "peer_count": 5,
            },
            "sector_factors": [{
                "sector_name": "CPO",
                "change_pct": 4.8,
                "fund_flow": 42.0,
                "strength_score": 88,
                "limit_up_count": 7,
                "peer_count": 5,
            }],
        },
    }


def test_sector_repair_detection_pool_reaches_a2_light_feishu_push():
    anomaly = _make_sector_repair_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    messages = _build_push_messages_for_anomalies(
        selected,
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[anomaly],
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(messages) == 1
    assert messages[0].extra["alert_tier"] == "light"
    assert messages[0].extra["signal_variant"] == "sector_repair_reversal"
    assert "不参与" not in messages[0].content


def test_sector_repair_push_stops_after_price_leaves_buyable_zone():
    anomaly = _make_sector_repair_anomaly(change_pct=6.8)

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def _make_old_hot_repair_anomaly(*, change_pct: float = 7.8) -> dict:
    return {
        "code": "002354",
        "name": "天娱数科",
        "event_type": "breakthrough",
        "level": "major",
        "score": 91,
        "description": "旧高标超跌首日强修复",
        "detail": {
            "signal_type": "old_hot_oversold_repair",
            "signal_label": "旧高标超跌首日强修复",
            "old_hot_repair_confirmed": True,
            "detection_pool_member": True,
            "watchlist_member": True,
            "quality_score": 91,
            "ma_status": "repair_reversal",
            "pullback_probability": "medium",
            "days_near_pressure": 0,
            "is_false_breakout": False,
            "price": 6.47,
            "change_pct": change_pct,
            "volume_ratio": 1.1,
            "turnover": 10.6,
            "amplitude": 8.8,
            "return_20d": -24.0,
            "position_120": 0.22,
            "historical_board_like_count": 4,
            "historical_max_board_streak": 3,
            "last_board_age": 35,
            "near_intraday_high": True,
            "vwap_reclaimed": True,
            "old_hot_repair_confirmations": ["强修复后贴近日内高点", "站稳VWAP"],
            "old_hot_repair_driver": {
                "sector_name": "AI应用",
                "change_pct": 1.8,
                "fund_flow": 8.0,
                "strength_score": 72,
                "limit_up_count": 3,
                "peer_count": 2,
            },
            "sector_factors": [{
                "sector_name": "AI应用",
                "change_pct": 1.8,
                "fund_flow": 8.0,
                "strength_score": 72,
                "limit_up_count": 3,
                "peer_count": 2,
            }],
        },
    }


def test_old_hot_repair_reaches_a2_light_feishu_push_but_never_a1():
    anomaly = _make_old_hot_repair_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    messages = _build_push_messages_for_anomalies(
        selected,
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[anomaly],
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(messages) == 1
    assert messages[0].extra["alert_tier"] == "light"
    assert messages[0].extra["signal_variant"] == "old_hot_oversold_repair"
    assert "盘口确认后轻仓" in messages[0].content
    assert "不参与" not in messages[0].content


def test_old_hot_repair_push_stops_after_limit_chasing_zone():
    anomaly = _make_old_hot_repair_anomaly(change_pct=9.1)

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def test_old_hot_strong_extension_keeps_a2_push_after_strict_driver_confirmation():
    anomaly = _make_old_hot_repair_anomaly(change_pct=8.9)
    anomaly["detail"].update({
        "amplitude": 10.4,
        "strong_extension_confirmed": True,
    })
    anomaly["detail"]["old_hot_repair_driver"].update({
        "change_pct": 3.2,
        "strength_score": 88,
        "limit_up_count": 6,
        "peer_count": 5,
    })
    anomaly["detail"]["sector_factors"][0].update({
        "change_pct": 3.2,
        "strength_score": 88,
        "limit_up_count": 6,
        "peer_count": 5,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True


def _make_low_absorb_anomaly(
    code: str = "000112",
    name: str = "低吸确认股",
    *,
    change_pct: float = -0.6,
    volume_ratio: float = 1.35,
    support_strength_score: float = 72,
) -> dict:
    anomaly = {
        "code": code,
        "name": name,
        "event_type": "low_absorb",
        "level": "major",
        "score": 82,
        "description": "绿盘弱转强低吸 + 回踩MA5低吸",
        "detail": {
            "signal_type": "low_absorb",
            "low_absorb_type": "green_reversal_ma5_pullback",
            "signal_label": "绿盘弱转强低吸 + 回踩MA5低吸",
            "price": 10.08,
            "prev_close": 10.14,
            "open": 9.92,
            "high": 10.18,
            "low": 9.78,
            "avg_price": 10.02,
            "change_pct": change_pct,
            "min5_change": 0.82,
            "volume_ratio": volume_ratio,
            "turnover": 6.4,
            "amplitude": 3.9,
            "amount": 520_000_000,
            "volume": 520_000,
            "ma5": 10.02,
            "ma10": 9.82,
            "ma20": 9.55,
            "distance_to_ma5_pct": 0.6,
            "intraday_rebound_pct": 3.07,
            "price_vs_avg_pct": 0.6,
            "main_net_inflow": 180_000_000,
            "main_net_inflow_pct": 5.2,
            "super_net_inflow": 90_000_000,
            "super_net_inflow_pct": 1.7,
            "big_net_inflow": 80_000_000,
            "big_net_inflow_pct": 2.2,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15 10:18:00",
            "is_stale": False,
            "bid_depth_5": 48000,
            "ask_depth_5": 26000,
            "orderbook_imbalance": 0.18,
            "support_strength_score": support_strength_score,
            "withdrawal_ratio": 0.03,
            "sector_factors": [{"sector_name": "机器人", "fund_flow": 8.2, "change_pct": 1.1}],
            "low_absorb_confirmations": ["上升通道回踩MA5不破", "重新站回VWAP附近"],
        },
    }

    return _add_fund_provenance(anomaly)


def _make_underwater_reversal_anomaly(
    *,
    change_pct: float = 3.2,
    source: str = "eastmoney_main_fund",
    fund_data_degraded: bool = False,
) -> dict:
    anomaly = {
        "code": "002580",
        "name": "水下快速启动股",
        "event_type": "low_absorb",
        "level": "major",
        "score": 90,
        "description": "水下翻红快速启动",
        "detail": {
            "signal_type": "underwater_reversal",
            "low_absorb_type": "underwater_reversal",
            "signal_label": "水下翻红快速启动",
            "underwater_reversal_confirmed": True,
            "detection_pool_member": True,
            "detection_pool_source": "intraday_underwater_reversal",
            "price": 10.32,
            "prev_close": 10.0,
            "open": 9.90,
            "high": 10.45,
            "low": 9.78,
            "avg_price": 10.12,
            "change_pct": change_pct,
            "min5_change": 1.15,
            "scan_change_pct": 0.9,
            "crossed_from_underwater": True,
            "intraday_amount_confirmed": True,
            "intraday_amount_delta": 8_000_000,
            "intraday_amount_interval_sec": 30.0,
            "intraday_amount_pace_ratio": 2.4,
            "volume_ratio": 1.35,
            "turnover": 8.2,
            "amplitude": 6.7,
            "amount": 730_000_000,
            "volume": 720_000,
            "low_drop_pct": -2.2,
            "intraday_rebound_pct": 5.52,
            "price_vs_avg_pct": 1.98,
            "close_position": 0.806,
            "pullback_from_high_pct": -1.24,
            "underwater_core_confirmation_count": 3,
            "main_net_inflow": 180_000_000,
            "main_net_inflow_pct": 5.2,
            "super_net_inflow": 80_000_000,
            "super_net_inflow_pct": 2.1,
            "big_net_inflow": 70_000_000,
            "big_net_inflow_pct": 2.0,
            "source": source,
            "as_of": "2026-08-10 13:35:00",
            "is_stale": False,
            "fund_data_degraded": fund_data_degraded,
            "bid_depth_5": 62000,
            "ask_depth_5": 31000,
            "orderbook_imbalance": 0.26,
            "support_strength_score": 76,
            "withdrawal_ratio": 0.03,
            "sector_factors": [{
                "sector_name": "储能",
                "fund_flow": 12.0,
                "change_pct": 1.8,
                "strength_score": 76,
                "limit_up_count": 4,
            }],
            "low_absorb_confirmations": [
                "日内最低-2.2%后拉回红盘",
                "站回VWAP上方",
                "盘口与板块共振",
            ],
        },
    }

    return _add_fund_provenance(anomaly, provider="tencent" if source == "fund_flow" else "eastmoney")


def _make_underwater_acceleration_anomaly(*, volume_ratio: float = 1.40) -> dict:
    anomaly = _make_underwater_reversal_anomaly(change_pct=-1.8)
    anomaly["description"] = "水下放量急拉预警"
    anomaly["detail"].update({
        "signal_type": "underwater_acceleration",
        "low_absorb_type": "underwater_acceleration",
        "signal_label": "水下放量急拉预警",
        "underwater_acceleration_confirmed": True,
        "underwater_reversal_confirmed": False,
        "pre_reversal_alert": True,
        "detection_pool_source": "intraday_underwater_acceleration",
        "price": 9.82,
        "open": 9.60,
        "high": 9.84,
        "low": 9.55,
        "avg_price": 9.78,
        "change_pct": -1.8,
        "min5_change": 1.10,
        "scan_change_pct": 1.70,
        "volume_ratio": volume_ratio,
        "turnover": 3.0,
        "amplitude": 3.0,
        "low_drop_pct": -4.5,
        "intraday_rebound_pct": 2.83,
        "price_vs_avg_pct": 0.41,
        "close_position": 0.931,
        "pullback_from_high_pct": -0.20,
        "underwater_core_confirmation_count": 3,
        "low_absorb_confirmations": [
            "仍在水下-1.8%，已从低点急拉2.8%",
            "量比1.40，短周期放量加速",
        ],
    })
    return anomaly


def _make_positive_acceleration_anomaly(*, volume_ratio: float = 1.55) -> dict:
    anomaly = {
        "code": "002580",
        "name": "红盘加速股",
        "event_type": "breakthrough",
        "level": "major",
        "score": 84,
        "description": "红盘放量二次加速预警",
        "detail": {
            "signal_type": "positive_acceleration",
            "signal_label": "红盘放量二次加速预警",
            "positive_acceleration_confirmed": True,
            "detection_pool_member": True,
            "detection_pool_source": "intraday_positive_acceleration",
            "quality_score": 84,
            "ma_status": "",
            "pullback_probability": "medium",
            "days_near_pressure": 0,
            "is_false_breakout": False,
            "price": 10.40,
            "prev_close": 10.0,
            "open": 10.05,
            "high": 10.42,
            "low": 10.0,
            "avg_price": 10.20,
            "change_pct": 4.0,
            "previous_change_pct": 1.78,
            "min5_change": 1.3,
            "scan_change_pct": 2.22,
            "acceleration_pct": 2.22,
            "intraday_amount_confirmed": True,
            "intraday_amount_delta": 10_000_000,
            "intraday_amount_interval_sec": 30.0,
            "intraday_amount_pace_ratio": 2.8,
            "near_high_ratio": 0.9981,
            "price_vs_avg_pct": 1.96,
            "volume_ratio": volume_ratio,
            "turnover": 6.0,
            "amplitude": 4.2,
            "amount": 480_000_000,
            "volume": 480_000,
            "main_net_inflow": 150_000_000,
            "main_net_inflow_pct": 5.0,
            "source": "eastmoney_main_fund",
            "as_of": "2026-08-10 13:31:00",
            "is_stale": False,
            "fund_data_degraded": False,
            "bid_depth_5": 60_000,
            "ask_depth_5": 30_000,
            "orderbook_imbalance": 0.20,
            "support_strength_score": 74,
            "withdrawal_ratio": 0.03,
            "positive_acceleration_core_confirmation_count": 3,
            "sector_factors": [{
                "sector_name": "储能",
                "fund_flow": 12.0,
                "change_pct": 1.8,
                "strength_score": 76,
                "limit_up_count": 3,
            }],
            "acceleration_confirmations": [
                "红盘+4.0%仍在二次加速窗口",
                "短周期加速+2.2% / 量比1.55",
            ],
        },
    }

    return _add_fund_provenance(anomaly)


def _make_trend_support_touch_anomaly() -> dict:
    anomaly = _make_low_absorb_anomaly(
        code="003032",
        name="趋势低吸股",
        change_pct=-1.76,
        volume_ratio=1.15,
        support_strength_score=72,
    )
    anomaly["score"] = 80
    anomaly["description"] = "趋势支撑到达预警"
    anomaly["detail"].update({
        "signal_type": "trend_support_touch",
        "signal_label": "趋势支撑到达预警",
        "low_absorb_type": "trend_support_touch",
        "trend_support_touch_confirmed": True,
        "trend_setup_confirmed": True,
        "trend_driver_watchlist": True,
        "watchlist_member": True,
        "detection_pool_member": True,
        "detection_pool_source": "dynamic_trend_support_touch",
        "trend_support": 10.0,
        "support_gap_pct": 0.2,
        "from_intraday_low_pct": 0.3,
        "near_intraday_low": True,
        "wait_reclaim_confirmation": True,
        "price": 10.02,
        "high": 10.18,
        "low": 9.99,
        "avg_price": 10.08,
        "min5_change": -0.2,
        "amplitude": 1.9,
        "turnover": 3.2,
        "main_net_inflow": 80_000_000,
        "main_net_inflow_pct": 4.0,
        "source": "eastmoney_main_fund",
        "as_of": "2026-08-10 10:05:00",
        "is_stale": False,
        "support_touch_core_confirmation_count": 3,
        "sector_factors": [{
            "sector_name": "教育",
            "fund_flow": 6.0,
            "change_pct": 1.2,
            "strength_score": 74,
            "limit_up_count": 1,
        }],
        "low_absorb_confirmations": [
            "日内最低9.99已触及趋势支撑10.00",
            "当前仅为到达预警，等待止跌回拉后再升级",
        ],
    })
    return anomaly


def _make_main_wave_green_open_reclaim_anomaly() -> dict:
    anomaly = _make_low_absorb_anomaly(
        code="000636",
        name="风华高科",
        change_pct=-1.97,
        volume_ratio=1.18,
        support_strength_score=74,
    )
    anomaly["score"] = 84
    anomaly["description"] = "主升浪绿开下杀回收"
    anomaly["detail"].update({
        "signal_type": "main_wave_green_open_reclaim",
        "signal_label": "主升浪绿开下杀回收",
        "low_absorb_type": "main_wave_green_reversal",
        "main_wave_green_open_reclaim_confirmed": True,
        "main_wave_green_open_reclaim_rolling60": True,
        "trend_setup_confirmed": True,
        "trend_driver_watchlist": True,
        "watchlist_member": True,
        "detection_pool_member": True,
        "detection_pool_source": "dynamic_main_wave_green_open_reclaim",
        "open_change_pct": -4.30,
        "low_drop_pct": -4.94,
        "intraday_rebound_pct": 3.12,
        "short_momentum_pct": 0.82,
        "close_position": 0.92,
        "pullback_from_high_pct": -0.26,
        "price_vs_avg_pct": 0.31,
        "price": 64.55,
        "open": 63.02,
        "high": 64.72,
        "low": 62.60,
        "avg_price": 64.35,
        "amplitude": 3.22,
        "turnover": 6.8,
        "main_net_inflow": 120_000_000,
        "main_net_inflow_pct": 3.2,
        "source": "eastmoney_main_fund",
        "as_of": "2026-08-12 09:42:00",
        "is_stale": False,
        "main_wave_core_confirmation_count": 3,
        "rolling_60s_confirmed": True,
        "rolling_60s_change_pct": 0.82,
        "rolling_60s_interval_sec": 60.0,
        "rolling_60s_amount_delta": 15_000_000,
        "rolling_60s_amount_pace_ratio": 1.7,
        "rolling_60s_tier": "medium",
        "sector_factors": [{
            "sector_name": "被动元件",
            "fund_flow": 18.0,
            "change_pct": 1.5,
            "strength_score": 76,
            "limit_up_count": 2,
        }],
        "low_absorb_confirmations": [
            "主升浪绿开-4.3%，最低下探-4.9%",
            "从日内低点回抽3.1%并有增量成交",
        ],
    })
    return anomaly


def _make_main_wave_shape_pullback_reclaim_anomaly() -> dict:
    anomaly = _make_low_absorb_anomaly(
        code="600001",
        name="主升首阴样本",
        change_pct=-0.70,
        volume_ratio=1.05,
        support_strength_score=73,
    )
    anomaly["score"] = 86
    anomaly["description"] = "主升浪首阴低点回收"
    anomaly["detail"].update({
        "signal_type": "main_wave_shape_pullback_reclaim",
        "signal_label": "主升浪首阴低点回收",
        "low_absorb_type": "main_wave_shape_pullback_reclaim",
        "main_wave_shape_pullback_confirmed": True,
        "main_wave_shape_pullback_rolling60": True,
        "main_wave_strict_pullback_confirmed": True,
        "main_wave_core_sector_confirmed": True,
        "main_wave_core_sector_driver": {"sector_name": "被动元件"},
        "large_order_inflow_confirmed": True,
        "large_order_net_inflow": 42_000_000,
        "trend_setup_confirmed": True,
        "detection_pool_member": True,
        "pullback_shape_type": "first_bearish",
        "pullback_shape_label": "主升浪首阴",
        "pullback_anchor_low": 12.70,
        "support_break_pct": -0.16,
        "intraday_rebound_pct": 1.18,
        "short_momentum_pct": 0.46,
        "close_position": 0.65,
        "price_vs_avg_pct": 0.23,
        "main_wave_core_confirmation_count": 3,
        "main_net_inflow": 80_000_000,
        "main_net_inflow_pct": 3.0,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "rolling_60s_confirmed": True,
        "rolling_60s_change_pct": 0.46,
        "rolling_60s_amount_delta": 5_000_000,
        "rolling_60s_amount_pace_ratio": 1.5,
        "rolling_60s_interval_sec": 60.0,
        "sector_factors": [{
            "sector_name": "示例板块",
            "fund_flow": 10.0,
            "change_pct": 1.2,
            "strength_score": 74,
            "limit_up_count": 1,
        }],
    })
    return anomaly


def _make_second_wave_restart_anomaly() -> dict:
    anomaly = _make_low_absorb_anomaly(
        code="000815",
        name="美利云",
        change_pct=2.9,
        volume_ratio=1.45,
        support_strength_score=74,
    )
    anomaly["score"] = 88
    anomaly["description"] = "高标下杀二波启动预警"
    anomaly["detail"].update({
        "signal_type": "second_wave_restart",
        "signal_label": "高标下杀二波启动预警",
        "low_absorb_type": "second_wave_restart",
        "second_wave_restart_confirmed": True,
        "trend_setup_confirmed": True,
        "trend_driver_watchlist": True,
        "watchlist_member": True,
        "detection_pool_member": True,
        "detection_pool_source": "second_wave_reset_watch",
        "trend_setup_source": "second_wave_reset_watch",
        "second_wave_reset_type": "high_board_reset",
        "second_wave_reset_stats": {
            "setup_state": "armed_second_wave_reset",
            "reset_type": "high_board_reset",
            "max_board_streak": 4,
            "days_since_peak": 6,
            "drawdown_pct": -22.77,
            "first_wave_gain_pct": 48.2,
        },
        "price": 14.20,
        "prev_close": 13.80,
        "open": 13.86,
        "high": 14.22,
        "low": 13.72,
        "avg_price": 14.05,
        "min5_change": 0.9,
        "scan_change_pct": 1.1,
        "acceleration_pct": 1.1,
        "intraday_amount_confirmed": True,
        "intraday_amount_delta": 6_000_000,
        "intraday_amount_interval_sec": 30.0,
        "intraday_amount_pace_ratio": 2.2,
        "intraday_rebound_pct": 3.5,
        "near_intraday_high": True,
        "price_vs_avg_pct": 1.07,
        "amplitude": 3.6,
        "turnover": 5.2,
        "main_net_inflow": 100_000_000,
        "main_net_inflow_pct": 4.2,
        "source": "eastmoney_main_fund",
        "as_of": "2026-07-31 10:05:00",
        "is_stale": False,
        "second_wave_core_confirmation_count": 3,
        "max_recent_consecutive_days": 4,
        "sector_factors": [{
            "sector_name": "云计算",
            "fund_flow": 8.0,
            "change_pct": 1.5,
            "strength_score": 76,
            "limit_up_count": 2,
        }],
        "low_absorb_confirmations": [
            "首波4连板后下杀22.8%",
            "从日内低点急拉3.5%并贴近日内高点",
        ],
    })
    return anomaly


def test_trend_driver_breakthrough_reaches_pushable_buy_point_without_legacy_pressure_days():
    anomaly = _make_breakthrough_anomaly(
        "002827",
        "高争民爆",
        score=90,
        quality_score=90,
        change_pct=2.85,
        volume_ratio=1.8,
        support_strength_score=74,
        days_near_pressure=0,
        pullback_probability="low",
    )
    anomaly["detail"].update({
        "trend_setup_confirmed": True,
        "trend_driver_watchlist": True,
        "watchlist_member": True,
        "signal_type": "trend_driver_breakthrough",
        "trend_setup_label": "连板前兆-试盘缩量洗盘",
        "trend_setup_source": "pre_board_probe_wash",
        "trend_setup_score": 88,
        "long_cycle_regime": "historical_board_reset",
        "long_cycle_shape_ready": True,
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.72,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
        "price_vs_avg_pct": 0.6,
    })

    enriched = _enrich_anomaly_display(anomaly, [])

    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert enriched["buy_point_grade"] == "A1 可直接执行"


def test_sector_core_laggard_ignition_reaches_a2_only_with_rolling_volume_and_sector_width():
    anomaly = _make_breakthrough_anomaly(
        "000998",
        "隆平高科",
        score=90,
        quality_score=90,
        change_pct=1.2,
        volume_ratio=1.8,
        support_strength_score=74,
        days_near_pressure=0,
    )
    anomaly["detail"].update({
        "signal_type": "sector_core_laggard_ignition",
        "signal_label": "主线核心补涨点火",
        "sector_core_laggard_confirmed": True,
        "trend_setup_confirmed": True,
        "trend_driver_watchlist": True,
        "watchlist_member": True,
        "detection_pool_member": True,
        "detection_pool_source": "sector_core_laggard",
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.65,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
        "price_vs_avg_pct": 0.2,
        "vwap_reclaimed": True,
        "main_net_inflow": 90_000_000,
        "main_net_inflow_pct": 4.5,
        "sector_core_confirmation_count": 3,
        "sector_core_driver": {
            "sector_name": "玉米",
            "strength_score": 90,
            "change_pct": 7.4,
            "fund_flow": 10.2,
            "limit_up_count": 12,
        },
        "sector_factors": [{
            "sector_name": "玉米",
            "fund_flow": 10.2,
            "change_pct": 7.4,
            "strength_score": 90,
            "limit_up_count": 12,
        }],
        "sector_core_laggard_confirmations": [
            "玉米强度90 / 涨停12家",
            "60秒上涨+0.65% / 成交速率1.8倍",
            "站回VWAP",
            "多重产业映射4项",
            "盘口承接74且买盘占优",
        ],
    })

    enriched = _enrich_anomaly_display(anomaly, [])

    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"

    anomaly["detail"]["rolling_60s_confirmed"] = False
    blocked = _enrich_anomaly_display(anomaly, [])
    assert blocked["buy_point_pushable"] is False
    assert blocked["feishu_pushable"] is False


def test_trend_driver_breakthrough_waits_when_vwap_is_not_reclaimed():
    anomaly = _make_breakthrough_anomaly(
        "002827",
        "高争民爆",
        score=90,
        quality_score=90,
        change_pct=2.85,
        volume_ratio=1.8,
        support_strength_score=74,
        days_near_pressure=0,
        pullback_probability="low",
    )
    anomaly["detail"].update({
        "trend_setup_confirmed": True,
        "trend_driver_watchlist": True,
        "watchlist_member": True,
        "signal_type": "trend_driver_breakthrough",
        "trend_setup_source": "pre_board_probe_wash",
        "long_cycle_regime": "historical_board_reset",
        "long_cycle_shape_ready": True,
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
        "avg_price": 17.1,
        "price": 16.8,
    })

    enriched = _enrich_anomaly_display(anomaly, [])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert any("VWAP" in reason for reason in enriched["buy_point_blockers"])


def test_trend_driver_support_reclaim_reaches_pushable_buy_point():
    anomaly = _make_low_absorb_anomaly(
        code="003032",
        name="传智教育",
        change_pct=-0.5,
        volume_ratio=1.3,
        support_strength_score=74,
    )
    anomaly["score"] = 90
    anomaly["detail"].update({
        "source": "eastmoney_main_fund",
        "trend_setup_confirmed": True,
        "trend_driver_watchlist": True,
        "watchlist_member": True,
        "signal_type": "trend_driver_low_absorb",
        "signal_label": "趋势驱动支撑回收",
        "low_absorb_type": "trend_driver_support_reclaim",
        "trend_support": 5.45,
        "support_gap_pct": 1.0,
        "distance_to_ma5_pct": 1.0,
        "intraday_rebound_pct": 1.2,
        "price_vs_avg_pct": 0.2,
        "min5_change": 0.7,
        "main_net_inflow": 100_000_000,
        "main_net_inflow_pct": 5.0,
        "orderbook_imbalance": 0.15,
        "bid_depth_5": 180_000,
        "ask_depth_5": 100_000,
        "amplitude": 3.2,
        "turnover": 5.0,
        "sector_factors": [{
            "sector_name": "教育",
            "fund_flow": 8.0,
            "change_pct": 1.6,
            "strength_score": 78,
        }],
    })

    enriched = _enrich_anomaly_display(anomaly, [])

    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert enriched["buy_point_grade"] == "A1 可直接执行"


@pytest.mark.asyncio
async def test_background_push_scan_restores_trend_pool_without_page_visit(monkeypatch):
    previous_pool = dict(tenbagger_module.anomaly_scanner.dynamic_trend_pool)
    tenbagger_module.anomaly_scanner.dynamic_trend_pool = {}
    calls = []

    async def fake_prewarm(db, trade_date, *, force_refresh, limit):
        calls.append((trade_date, force_refresh, limit))
        tenbagger_module.anomaly_scanner.update_dynamic_trend_pool([{
            "code": "003032",
            "name": "传智教育",
            "score": 88,
            "label": "趋势驱动-首波回踩",
            "setup_state": "armed_pullback",
            "support": 5.45,
            "resistance": 5.60,
        }])
        return {"trend_pool": []}

    monkeypatch.setattr(tenbagger_module, "prewarm_next_day_plan_snapshot", fake_prewarm)
    try:
        ready = await tenbagger_module._ensure_dynamic_trend_pool(
            None,
            date(2026, 7, 31),
        )
    finally:
        tenbagger_module.anomaly_scanner.dynamic_trend_pool = previous_pool

    assert ready is True
    assert calls == [(date(2026, 7, 31), False, 20)]


@pytest.mark.asyncio
async def test_intraday_sector_core_refresh_replaces_only_stale_sector_core_rows(monkeypatch):
    previous_pool = dict(tenbagger_module.anomaly_scanner.dynamic_trend_pool)
    previous_state = dict(tenbagger_module._DYNAMIC_SECTOR_CORE_REFRESH_STATE)

    async def fake_loader(_db, trade_date, *, limit):
        assert trade_date == date(2026, 8, 20)
        assert limit == 48
        return [{
            "code": "600200",
            "name": "当日主线补涨",
            "candidate_source": "sector_core_laggard",
            "candidate_source_label": "主线核心补涨-量价确认",
            "main_wave_score": 88,
            "main_wave_stats": {
                "setup_state": "armed_sector_core_laggard",
                "support": 10.0,
                "resistance": 10.5,
                "sector_context_trade_date": "2026-08-20",
            },
        }]

    monkeypatch.setattr(
        tenbagger_module,
        "_load_sector_core_laggard_plan_candidates",
        fake_loader,
    )
    tenbagger_module.anomaly_scanner.update_dynamic_trend_pool([
        {
            "code": "600100", "name": "昨日主线",
            "candidate_source": "sector_core_laggard", "main_wave_score": 80,
            "main_wave_stats": {"support": 9.0, "resistance": 9.5},
        },
        {
            "code": "600300", "name": "独立趋势",
            "candidate_source": "trend_driver_setup", "main_wave_score": 82,
            "main_wave_stats": {"support": 11.0, "resistance": 11.6},
        },
    ])
    tenbagger_module._DYNAMIC_SECTOR_CORE_REFRESH_STATE.update({
        "trade_date": "",
        "refreshed_monotonic": 0.0,
    })
    try:
        refreshed = await tenbagger_module._refresh_intraday_sector_core_pool(
            object(),
            date(2026, 8, 20),
            force=True,
        )
        assert refreshed is True
        assert set(tenbagger_module.anomaly_scanner.dynamic_trend_pool) == {
            "600200", "600300",
        }
    finally:
        tenbagger_module.anomaly_scanner.dynamic_trend_pool = previous_pool
        tenbagger_module._DYNAMIC_SECTOR_CORE_REFRESH_STATE.update(previous_state)


def _make_invalid_breakthrough_anomaly(code: str, score: float = 95) -> dict:
    return {
        "code": code,
        "name": f"占位股{code[-2:]}",
        "event_type": "breakthrough",
        "level": "major",
        "score": score,
        "description": "快速拉升，逼近日内新高",
        "detail": {
            "price": 12.6,
            "change_pct": 5.2,
            "volume_ratio": 1.4,
            "amplitude": 7.6,
            "quality_score": 62,
            "ma_status": "neutral",
            "pullback_probability": "high",
            "days_near_pressure": 1,
            "support_strength_score": 44,
            "orderbook_imbalance": -0.08,
            "bid_depth_5": 12000,
            "ask_depth_5": 18000,
            "is_false_breakout": True,
            "as_of": "2026-04-15 14:58:00",
            "turnover": 18.2,
        },
    }


def test_confirmed_capital_inflow_push_contains_source_and_confirmation():
    anomaly = {
        "code": "000001",
        "name": "测试股",
        "event_type": "capital",
        "level": "critical",
        "score": 88,
        "description": "资金净额 6.0亿",
        "detail": {
            "main_net_inflow": 600_000_000,
            "price": 12.3,
            "avg_price": 12.2,
            "main_net_inflow_pct": 9.2,
            "super_net_inflow": 230_000_000,
            "super_net_inflow_pct": 3.6,
            "big_net_inflow": 170_000_000,
            "big_net_inflow_pct": 2.8,
            "change_pct": 3.8,
            "volume_ratio": 2.4,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "bid_depth_5": 52000,
            "ask_depth_5": 26000,
            "orderbook_imbalance": 0.33,
            "withdrawal_ratio": 0.04,
            "support_strength_score": 72,
            "turnover": 8.5,
            "amplitude": 4.2,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 12.5, "change_pct": 1.8}],
        },
    }
    _add_fund_provenance(anomaly)
    peer_events = [
        anomaly,
        {
            "code": "000001",
            "name": "测试股",
            "event_type": "breakthrough",
            "score": 82,
            "detail": {"price": 12.3, "volume_ratio": 2.1},
            "description": "突破60日新高",
        },
    ]

    message = _build_anomaly_push_message(anomaly, peer_events)

    assert message is not None
    assert message.title == "📈 测试股 (000001) A1 可直接执行｜买入信号"
    assert "执行等级: A1 可直接执行" in message.content
    assert "📋 异动基本信息" in message.content
    assert "💰 交易预案" in message.content
    assert "📊 量价分析" in message.content
    assert "🧭 主力/盘口确认" in message.content
    assert "🎯 推荐理由" in message.content
    assert "叠加真实突破信号" in message.content
    assert "资金净额占成交额比 9.2%" in message.content
    assert "特大单: 2.3亿 ／ 3.60%" in message.content
    assert "大单: 1.7亿 ／ 2.80%" in message.content
    assert "买五总量: 5.20万手 ｜ 卖五总量: 2.60万手" in message.content
    assert "盘口失衡: 33.0% ｜ 承接强度: 72 ｜ 撤单比: 4.0%" in message.content


def test_unconfirmed_or_stale_capital_push_is_filtered():
    stale_anomaly = {
        "code": "000002",
        "name": "旧快照股",
        "event_type": "capital",
        "level": "major",
        "score": 72,
        "description": "资金净额 2.1亿",
        "detail": {
            "main_net_inflow": 210_000_000,
            "main_net_inflow_pct": 5.0,
            "change_pct": 1.2,
            "volume_ratio": 1.1,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-10",
            "is_stale": True,
            "capital_anomaly_type": "main_inflow",
        },
    }
    weak_anomaly = {
        "code": "000003",
        "name": "弱确认股",
        "event_type": "capital",
        "level": "major",
        "score": 75,
        "description": "资金净额 2.2亿",
        "detail": {
            "main_net_inflow": 220_000_000,
            "main_net_inflow_pct": 3.0,
            "change_pct": 0.5,
            "volume_ratio": 1.0,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
        },
    }

    assert _build_anomaly_push_message(stale_anomaly, [stale_anomaly]) is None
    assert _build_anomaly_push_message(weak_anomaly, [weak_anomaly]) is None


@pytest.mark.parametrize("source", ["fund_flow", "eastmoney_main_fund"])
def test_capital_source_label_without_provenance_is_rejected(source):
    anomaly = _make_capital_anomaly("000009", "缺证据资金股", source=source)
    for field in (
        "code", "trade_date", "provider_source", "source_version",
        "source_quote_at", "received_at", "observed_at",
    ):
        anomaly["detail"].pop(field)
    peer_events = [
        anomaly,
        _make_breakthrough_anomaly("000009", "缺证据资金股", score=82),
    ]

    assert _build_anomaly_push_message(anomaly, peer_events) is None
    assert _enrich_anomaly_display(anomaly, peer_events)["buy_point_pushable"] is False


@pytest.mark.parametrize("source", ["fund_flow", "eastmoney_main_fund"])
@pytest.mark.parametrize("support, grade", [
    (72, "A1 可直接执行"),
    (69, "A2 盘口确认后执行"),
])
def test_qualified_providers_share_capital_grades(source, support, grade):
    anomaly = _make_capital_anomaly(
        "000009", "合格资金股", source=source, support_strength_score=support,
    )
    peer_events = [
        anomaly,
        _make_breakthrough_anomaly("000009", "合格资金股", score=82),
    ]

    message = _build_anomaly_push_message(anomaly, peer_events)

    assert message is not None
    assert grade in message.title
    assert _enrich_anomaly_display(anomaly, peer_events)["buy_point_grade"] == grade
    assert "同花顺资金净额" not in message.content
    if source == "fund_flow":
        assert anomaly["detail"]["provider_source"] == "tencent"
        assert "统一资金数据（来源见证据）" in message.content

@pytest.mark.parametrize("field", [
    "code", "trade_date", "provider_source", "source_version",
    "source_quote_at", "received_at", "observed_at",
])
def test_capital_rejects_each_missing_provenance_field(field):
    anomaly = _make_capital_anomaly("000009", "证据缺项股", source="fund_flow")
    anomaly["detail"].pop(field)

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


@pytest.mark.parametrize("source", ["fund_flow", "eastmoney_main_fund"])
def test_capital_rejects_expired_source_clock_even_when_stale_flag_is_false(source, monkeypatch):
    # Source age uses trading time (lunch is paused). This expiry test must run
    # at a fixed continuous-auction clock, not depend on when pytest is invoked.
    at = tenbagger_module.datetime(2026, 9, 22, 10, 30)
    class Clock(tenbagger_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(tenbagger_module, "datetime", Clock)
    anomaly = _make_capital_anomaly("000009", "时钟过期股", source=source)
    expired = tenbagger_module.datetime.now() - timedelta(
        seconds=tenbagger_module.settings.FUND_FLOW_SOURCE_MAX_AGE_SEC + 1,
    )
    anomaly["detail"]["source_quote_at"] = expired.isoformat()

    assert anomaly["detail"]["is_stale"] is False
    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_stale_fund_flow_capital_push_is_filtered():
    stale_anomaly = _make_capital_anomaly("000010", "旧资金股", source="fund_flow", is_stale=True)

    assert _build_anomaly_push_message(stale_anomaly, [stale_anomaly]) is None


def test_orderbook_support_can_confirm_capital_inflow_push():
    anomaly = {
        "code": "000005",
        "name": "承接强股",
        "event_type": "capital",
        "level": "major",
        "score": 78,
        "description": "资金净额 2.8亿",
        "detail": {
            "main_net_inflow": 280_000_000,
            "main_net_inflow_pct": 7.5,
            "change_pct": 2.6,
            "volume_ratio": 1.7,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "bid_depth_5": 52000,
            "ask_depth_5": 26000,
            "orderbook_imbalance": 0.33,
            "support_strength_score": 72,
            "seal_quality_score": 0,
            "withdrawal_ratio": 0.0,
            "turnover": 9.2,
            "amplitude": 3.8,
            "volume": 9_000_000,
            "sector_factors": [{"sector_name": "东数西算", "fund_flow": 10.2, "change_pct": 1.3}],
        },
    }

    _add_fund_provenance(anomaly)

    message = _build_anomaly_push_message(anomaly, [anomaly])

    assert message is not None
    assert "盘口承接强" in message.content
    assert message.title == "📈 承接强股 (000005) B类观察候选｜买入信号"
    assert "成交量:" in message.content


def test_capital_outflow_push_is_filtered():
    anomaly = {
        "code": "000006",
        "name": "撤单风险股",
        "event_type": "capital",
        "level": "major",
        "score": 76,
        "description": "资金净额 -4.6亿",
        "detail": {
            "main_net_inflow": -460_000_000,
            "main_net_inflow_pct": -9.8,
            "change_pct": -2.7,
            "volume_ratio": 1.6,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "bid_depth_5": 18000,
            "ask_depth_5": 39000,
            "orderbook_imbalance": -0.37,
            "support_strength_score": 36,
            "seal_quality_score": 0,
            "withdrawal_ratio": 0.48,
        },
    }

    _add_fund_provenance(anomaly)

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_breakthrough_can_use_fund_flow_capital_confirmation():
    capital = _make_capital_anomaly("000011", "突破联动股", source="fund_flow", score=82)
    breakthrough = _make_breakthrough_anomaly("000011", "突破联动股", score=84)

    message = _build_anomaly_push_message(breakthrough, [capital, breakthrough])

    assert message is not None
    assert "伴随资金确认流入" in message.content
    assert "A2 盘口确认后执行" in message.title


def test_push_message_builder_prefers_stronger_event_over_capital():
    anomalies = [
        {
            "code": "000004",
            "name": "强势股",
            "event_type": "capital",
            "level": "critical",
            "score": 90,
            "description": "资金净额 8.0亿",
            "detail": {
                "main_net_inflow": 800_000_000,
                "main_net_inflow_pct": 11.0,
                "change_pct": 5.2,
                "volume_ratio": 2.8,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
            },
        },
        {
            "code": "000004",
            "name": "强势股",
            "event_type": "limit_up",
            "level": "critical",
            "score": 95,
            "description": "首板涨停",
            "detail": {
                "price": 12.8,
                "consecutive_days": 1,
                "volume_ratio": 1.9,
                "turnover": 13.5,
                "seal_quality_score": 82,
                "support_strength_score": 68,
                "orderbook_imbalance": 0.16,
                "bid_depth_5": 36000,
                "ask_depth_5": 22000,
            },
        },
    ]

    _add_fund_provenance(anomalies[0])

    messages = _build_push_messages_for_anomalies(anomalies)

    assert len(messages) == 1
    assert messages[0].stock_code == "000004"
    assert messages[0].title == "🧱 强势股 (000004) A1 可直接执行｜打板关注"


def test_push_message_candidates_keep_selected_anomaly_in_sync():
    anomalies = [
        {
            "code": "000101",
            "name": "观察股",
            "event_type": "breakthrough",
            "level": "major",
            "score": 92,
            "description": "快速拉升，逼近日内新高",
            "detail": {
                "price": 12.6,
                "change_pct": 5.2,
                "volume_ratio": 1.4,
                "amplitude": 7.6,
                "quality_score": 62,
                "ma_status": "neutral",
                "pullback_probability": "high",
                "days_near_pressure": 1,
                "support_strength_score": 44,
                "orderbook_imbalance": -0.08,
                "bid_depth_5": 12000,
                "ask_depth_5": 18000,
                "is_false_breakout": True,
                "as_of": "2026-04-15 14:58:00",
                "turnover": 18.2,
            },
        },
        {
            "code": "000102",
            "name": "普通股",
            "event_type": "breakthrough",
            "level": "major",
            "score": 91,
            "description": "快速拉升，逼近日内新高",
            "detail": {
                "price": 18.2,
                "change_pct": 4.8,
                "volume_ratio": 1.3,
                "amplitude": 6.9,
                "quality_score": 60,
                "ma_status": "neutral",
                "pullback_probability": "medium",
                "days_near_pressure": 1,
                "support_strength_score": 46,
                "orderbook_imbalance": -0.04,
                "bid_depth_5": 16000,
                "ask_depth_5": 21000,
                "is_false_breakout": True,
                "as_of": "2026-04-15 14:58:00",
                "turnover": 17.5,
            },
        },
        {
            "code": "000103",
            "name": "首板股",
            "event_type": "limit_up",
            "level": "critical",
            "score": 85,
            "description": "涨停",
            "detail": {
                "price": 10.8,
                "consecutive_days": 1,
                "volume_ratio": 1.9,
                "turnover": 11.2,
                "seal_quality_score": 82,
                "support_strength_score": 68,
                "orderbook_imbalance": 0.16,
                "bid_depth_5": 36000,
                "ask_depth_5": 22000,
                "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
            },
        },
        {
            "code": "000104",
            "name": "跟风首板",
            "event_type": "limit_up",
            "level": "critical",
            "score": 85,
            "description": "涨停",
            "detail": {
                "price": 13.2,
                "consecutive_days": 1,
                "volume_ratio": 1.8,
                "turnover": 10.4,
                "seal_quality_score": 84,
                "support_strength_score": 70,
                "orderbook_imbalance": 0.18,
                "bid_depth_5": 41000,
                "ask_depth_5": 20000,
                "sector_factors": [{"sector_name": "机器人", "fund_flow": 7.0, "change_pct": 1.1}],
            },
        },
    ]

    selections = _build_push_message_candidates(anomalies, min_grade="A1 可直接执行")

    assert [item["anomaly"]["code"] for item in selections] == ["000103", "000104"]
    assert [item["message"].stock_code for item in selections] == ["000103", "000104"]


def test_buypoint_candidates_filter_lonely_limit_up_without_continuity():
    anomalies = [
        {
            "code": "000109",
            "name": "孤板股",
            "event_type": "limit_up",
            "level": "critical",
            "score": 86,
            "description": "首板涨停",
            "detail": {
                "price": 11.8,
                "consecutive_days": 1,
                "volume_ratio": 1.9,
                "turnover": 10.2,
                "seal_quality_score": 84,
                "support_strength_score": 69,
                "orderbook_imbalance": 0.18,
                "bid_depth_5": 41000,
                "ask_depth_5": 21000,
            },
            "is_one_word_board": False,
        }
    ]

    selections = _build_push_message_candidates(anomalies, min_grade="A1 可直接执行")

    assert selections == []


def test_same_stock_light_push_merges_primary_and_secondary_signals():
    capital = {
        "code": "000105",
        "name": "联动股",
        "event_type": "capital",
        "level": "critical",
        "score": 86,
        "description": "资金净额 4.6亿",
        "detail": {
            "main_net_inflow": 460_000_000,
            "price": 16.8,
            "avg_price": 16.7,
            "main_net_inflow_pct": 8.8,
            "super_net_inflow": 220_000_000,
            "super_net_inflow_pct": 2.1,
            "big_net_inflow": 180_000_000,
            "big_net_inflow_pct": 2.6,
            "change_pct": 5.2,
            "volume_ratio": 2.0,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15 10:20:00",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "bid_depth_5": 42000,
            "ask_depth_5": 22000,
            "orderbook_imbalance": 0.18,
            "support_strength_score": 68,
            "turnover": 10.8,
            "amplitude": 4.9,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 12.2, "change_pct": 1.8}],
        },
    }
    limit_up = {
        "code": "000105",
        "name": "联动股",
        "event_type": "limit_up",
        "level": "critical",
        "score": 88,
        "description": "首板涨停",
        "detail": {
            "price": 16.8,
            "consecutive_days": 1,
            "volume_ratio": 1.9,
            "turnover": 11.2,
            "seal_quality_score": 82,
            "support_strength_score": 68,
            "orderbook_imbalance": 0.16,
            "bid_depth_5": 36000,
            "ask_depth_5": 22000,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
        },
        "is_one_word_board": False,
    }
    breakthrough = {
        "code": "000105",
        "name": "联动股",
        "event_type": "breakthrough",
        "level": "major",
        "score": 83,
        "description": "突破平台",
        "detail": {
            "price": 16.8,
            "avg_price": 16.7,
            "change_pct": 5.1,
            "volume_ratio": 2.0,
            "amplitude": 4.6,
            "quality_score": 85,
            "ma_status": "multi_long",
            "pullback_probability": "low",
            "days_near_pressure": 4,
            "support_strength_score": 62,
            "orderbook_imbalance": 0.10,
            "bid_depth_5": 34000,
            "ask_depth_5": 24000,
            "is_false_breakout": False,
            "as_of": "2026-04-15 10:21:00",
            "turnover": 11.0,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 12.2, "change_pct": 1.8}],
        },
    }

    _add_fund_provenance(capital)

    selections = _build_push_message_candidates(
        [capital],
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[capital, limit_up, breakthrough],
    )

    assert len(selections) == 1
    assert selections[0]["alert_tier"] == "light"
    assert selections[0]["message"].title.startswith("💡 轻提醒｜📈 联动股 (000105) A2 盘口确认后执行｜买入信号")
    assert "当前主信号: A1-打板型｜打板关注" in selections[0]["message"].content
    assert "同股副信号:" in selections[0]["message"].content
    assert "A2 盘口确认后执行｜突破买点" in selections[0]["message"].content


def test_a2_capital_light_push_cannot_bypass_weak_sector_with_breakthrough():
    capital = {
        "code": "000110",
        "name": "先手资金股",
        "event_type": "capital",
        "level": "critical",
        "score": 84,
        "description": "资金净额 5.2亿",
        "detail": {
            "main_net_inflow": 520_000_000,
            "main_net_inflow_pct": 5.3,
            "super_net_inflow": 180_000_000,
            "super_net_inflow_pct": 1.6,
            "big_net_inflow": 130_000_000,
            "big_net_inflow_pct": 2.1,
            "change_pct": 4.4,
            "volume_ratio": 1.4,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15 10:18:00",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "bid_depth_5": 32000,
            "ask_depth_5": 25000,
            "orderbook_imbalance": 0.11,
            "support_strength_score": 59,
            "turnover": 11.0,
            "amplitude": 4.8,
            "sector_factors": [{"sector_name": "机器人", "fund_flow": -1.2, "change_pct": -0.3}],
        },
    }
    breakthrough = {
        "code": "000110",
        "name": "先手资金股",
        "event_type": "breakthrough",
        "level": "major",
        "score": 83,
        "description": "突破平台",
        "detail": {
            "price": 18.3,
            "change_pct": 4.6,
            "volume_ratio": 1.6,
            "amplitude": 4.2,
            "quality_score": 82,
            "ma_status": "multi_long",
            "pullback_probability": "low",
            "days_near_pressure": 3,
            "support_strength_score": 62,
            "orderbook_imbalance": 0.10,
            "bid_depth_5": 34000,
            "ask_depth_5": 25000,
            "is_false_breakout": False,
            "as_of": "2026-04-15 10:19:00",
            "turnover": 11.3,
            "sector_factors": [{"sector_name": "机器人", "fund_flow": -1.2, "change_pct": -0.3}],
        },
    }

    _add_fund_provenance(capital)

    selections = _build_push_message_candidates(
        [capital],
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[capital, breakthrough],
    )

    assert selections == []


def test_a2_breakthrough_light_push_cannot_bypass_weak_sector_with_capital():
    breakthrough = {
        "code": "000111",
        "name": "先手突破股",
        "event_type": "breakthrough",
        "level": "major",
        "score": 83,
        "description": "突破平台",
        "detail": {
            "price": 13.6,
            "change_pct": 4.8,
            "volume_ratio": 1.6,
            "amplitude": 4.9,
            "quality_score": 85,
            "ma_status": "long",
            "pullback_probability": "low",
            "days_near_pressure": 2,
            "support_strength_score": 58,
            "orderbook_imbalance": 0.09,
            "bid_depth_5": 30000,
            "ask_depth_5": 25000,
            "is_false_breakout": False,
            "as_of": "2026-04-15 10:28:00",
            "turnover": 11.6,
            "sector_factors": [{"sector_name": "AI应用", "fund_flow": -1.5, "change_pct": -0.6}],
        },
    }
    capital = {
        "code": "000111",
        "name": "先手突破股",
        "event_type": "capital",
        "level": "critical",
        "score": 82,
        "description": "资金净额 3.4亿",
        "detail": {
            "main_net_inflow": 340_000_000,
            "main_net_inflow_pct": 7.2,
            "super_net_inflow": 140_000_000,
            "super_net_inflow_pct": 1.6,
            "big_net_inflow": 120_000_000,
            "big_net_inflow_pct": 2.2,
            "change_pct": 4.8,
            "volume_ratio": 1.6,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15 10:27:00",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "bid_depth_5": 33000,
            "ask_depth_5": 25000,
            "orderbook_imbalance": 0.12,
            "support_strength_score": 62,
            "turnover": 10.2,
            "amplitude": 4.6,
            "sector_factors": [{"sector_name": "AI应用", "fund_flow": -1.5, "change_pct": -0.6}],
        },
    }

    _add_fund_provenance(capital)

    selections = _build_push_message_candidates(
        [breakthrough],
        min_grade="A2 盘口确认后执行",
        peer_anomalies=[breakthrough, capital],
    )

    assert selections == []


def test_a2_upgrade_can_be_selected_for_light_push():
    previous = [
        {
            "code": "000106",
            "name": "轻提醒股",
            "event_type": "capital",
            "level": "major",
            "score": 76,
            "description": "资金净额 2.8亿",
            "detail": {
                "main_net_inflow": 280_000_000,
                "main_net_inflow_pct": 6.2,
                "change_pct": 2.2,
                "volume_ratio": 1.4,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15 10:00:00",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 22000,
                "ask_depth_5": 18000,
                "orderbook_imbalance": 0.08,
                "support_strength_score": 54,
                "turnover": 8.0,
                "amplitude": 4.3,
                "sector_factors": [{"sector_name": "机器人", "fund_flow": 6.2, "change_pct": 1.0}],
            },
        }
    ]
    current = [
        {
            "code": "000106",
            "name": "轻提醒股",
            "event_type": "capital",
            "level": "critical",
            "score": 84,
            "description": "资金净额 3.4亿",
            "detail": {
                "main_net_inflow": 340_000_000,
                "main_net_inflow_pct": 8.4,
                "super_net_inflow": 140_000_000,
                "super_net_inflow_pct": 1.7,
                "big_net_inflow": 120_000_000,
                "big_net_inflow_pct": 2.2,
                "change_pct": 5.1,
                "volume_ratio": 1.9,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15 10:30:00",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 36000,
                "ask_depth_5": 23000,
                "orderbook_imbalance": 0.14,
                "support_strength_score": 68,
                "turnover": 9.0,
                "amplitude": 4.5,
                "sector_factors": [{"sector_name": "机器人", "fund_flow": 8.2, "change_pct": 1.1}],
            },
        }
    ]

    for event in [*previous, *current]:
        if event["event_type"] == "capital":
            _add_fund_provenance(event)

    selected = _select_pushworthy_anomalies(current, previous, min_grade="A2 盘口确认后执行")

    assert len(selected) == 1
    assert selected[0]["code"] == "000106"


@pytest.mark.asyncio
async def test_replay_anomaly_push_dry_run_matches_message_selection_order(monkeypatch):
    snapshot = {
        "trade_date": "2026-04-20",
        "snapshot_time": "2026-04-20T21:52:12.107191",
        "anomalies": [
            {
                "code": "000101",
                "name": "观察股",
                "event_type": "breakthrough",
                "level": "major",
                "score": 92,
                "description": "快速拉升，逼近日内新高",
                "detail": {
                    "price": 12.6,
                    "change_pct": 5.2,
                    "volume_ratio": 1.4,
                    "amplitude": 7.6,
                    "quality_score": 62,
                    "ma_status": "neutral",
                    "pullback_probability": "high",
                    "days_near_pressure": 1,
                    "support_strength_score": 44,
                    "orderbook_imbalance": -0.08,
                    "bid_depth_5": 12000,
                    "ask_depth_5": 18000,
                    "is_false_breakout": True,
                    "as_of": "2026-04-15 14:58:00",
                    "turnover": 18.2,
                },
            },
            {
                "code": "000102",
                "name": "普通股",
                "event_type": "breakthrough",
                "level": "major",
                "score": 91,
                "description": "快速拉升，逼近日内新高",
                "detail": {
                    "price": 18.2,
                    "change_pct": 4.8,
                    "volume_ratio": 1.3,
                    "amplitude": 6.9,
                    "quality_score": 60,
                    "ma_status": "neutral",
                    "pullback_probability": "medium",
                    "days_near_pressure": 1,
                    "support_strength_score": 46,
                    "orderbook_imbalance": -0.04,
                    "bid_depth_5": 16000,
                    "ask_depth_5": 21000,
                    "is_false_breakout": True,
                    "as_of": "2026-04-15 14:58:00",
                    "turnover": 17.5,
                },
            },
            {
                "code": "000103",
                "name": "首板股",
                "event_type": "limit_up",
                "level": "critical",
                "score": 85,
                "description": "涨停",
                "detail": {
                    "price": 10.8,
                    "consecutive_days": 1,
                    "volume_ratio": 1.9,
                    "turnover": 11.2,
                    "seal_quality_score": 82,
                    "support_strength_score": 68,
                    "orderbook_imbalance": 0.16,
                    "bid_depth_5": 36000,
                    "ask_depth_5": 22000,
                    "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
                },
            },
            {
                "code": "000104",
                "name": "跟风首板",
                "event_type": "limit_up",
                "level": "critical",
                "score": 85,
                "description": "涨停",
                "detail": {
                    "price": 13.2,
                    "consecutive_days": 1,
                    "volume_ratio": 1.8,
                    "turnover": 10.4,
                    "seal_quality_score": 84,
                    "support_strength_score": 70,
                    "orderbook_imbalance": 0.18,
                    "bid_depth_5": 41000,
                    "ask_depth_5": 20000,
                    "sector_factors": [{"sector_name": "机器人", "fund_flow": 7.0, "change_pct": 1.1}],
                },
            },
        ],
    }

    async def fake_prewarm(db, trade_date=None, force_refresh=False):
        return snapshot

    monkeypatch.setattr(tenbagger_module, "prewarm_anomaly_snapshot", fake_prewarm)

    result = await tenbagger_module.replay_anomaly_push(
        limit=2,
        min_score=70,
        event_type="",
        channel="feishu",
        allow_limit_up=True,
        force_refresh=False,
        bypass_throttle=True,
        dry_run=True,
        db=None,
    )

    assert [item["code"] for item in result["selected"]] == ["000103", "000104"]
    assert [item["name"] for item in result["selected"]] == ["首板股", "跟风首板"]
    assert [item["event_type"] for item in result["selected"]] == ["limit_up", "limit_up"]
    assert [item["alert_tier"] for item in result["selected"]] == ["strong", "strong"]


@pytest.mark.asyncio
async def test_replay_anomaly_push_defaults_to_skip_limit_up(monkeypatch):
    snapshot = {
        "trade_date": "2026-04-20",
        "snapshot_time": "2026-04-20T21:52:12.107191",
        "anomalies": [
            {
                "code": "000103",
                "name": "首板股",
                "event_type": "limit_up",
                "level": "critical",
                "score": 85,
                "description": "涨停",
                "detail": {
                    "price": 10.8,
                    "consecutive_days": 1,
                    "volume_ratio": 1.9,
                    "turnover": 11.2,
                    "seal_quality_score": 82,
                    "support_strength_score": 68,
                    "orderbook_imbalance": 0.16,
                    "bid_depth_5": 36000,
                    "ask_depth_5": 22000,
                    "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
                },
            }
        ],
    }

    async def fake_prewarm(db, trade_date=None, force_refresh=False):
        return snapshot

    monkeypatch.setattr(tenbagger_module, "prewarm_anomaly_snapshot", fake_prewarm)

    result = await tenbagger_module.replay_anomaly_push(
        limit=2,
        min_score=70,
        event_type="",
        channel="feishu",
        force_refresh=False,
        bypass_throttle=True,
        dry_run=True,
        db=None,
    )

    assert result["allow_limit_up"] is False
    assert result["selected"] == []


@pytest.mark.asyncio
async def test_refresh_and_push_uses_latest_persisted_snapshot_as_recovery_baseline(monkeypatch):
    trade_day = date(2026, 4, 23)
    previous_snapshot = {
        "trade_date": str(trade_day),
        "snapshot_time": "2026-04-23T09:40:00",
        "anomalies": [],
        "summary": {"total": 0},
        "b1_states": [],
        "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
    }
    current_snapshot = {
        "trade_date": str(trade_day),
        "snapshot_time": "2026-04-23T10:05:00",
        "anomalies": [_make_capital_anomaly("000201", "补发股", score=84)],
        "summary": {"total": 1},
        "b1_states": [],
        "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
    }
    pushed_messages = []
    ws_events = []

    async def fake_resolve_latest_trade_date(db, model_field, trade_date=None):
        return trade_day

    async def fake_get_latest_persisted_snapshot(db, target_date):
        assert target_date == trade_day
        return previous_snapshot

    async def fake_prewarm(db, trade_date=None, force_refresh=False):
        assert force_refresh is True
        return current_snapshot

    async def fake_push_batch(messages):
        pushed_messages.extend(messages)
        return [{"sent": True, "throttled": False, "status": "sent"} for _ in messages]

    async def fake_is_trading_hours():
        return True

    class DummyWsManager:
        async def push_anomaly(self, payload):
            ws_events.append(payload)

    monkeypatch.setattr(tenbagger_module, "resolve_latest_trade_date", fake_resolve_latest_trade_date)
    monkeypatch.setattr(tenbagger_module, "_get_cached_anomaly_snapshot", lambda target_date: None)
    monkeypatch.setattr(tenbagger_module, "_get_latest_persisted_anomaly_snapshot", fake_get_latest_persisted_snapshot)
    monkeypatch.setattr(tenbagger_module, "prewarm_anomaly_snapshot", fake_prewarm)
    monkeypatch.setattr(tenbagger_module.trade_calendar, "is_trading_hours", fake_is_trading_hours)
    monkeypatch.setattr(tenbagger_module.push_scheduler, "push_batch", fake_push_batch)
    monkeypatch.setattr(ws_module, "ws_manager", DummyWsManager())

    payload = await refresh_and_push_anomaly_snapshot(db=None, trade_date=trade_day)

    assert payload.pop("candidate_evidence_capture") == {
        "status": "not_recorded", "reason": "no_database_or_candidates"}
    assert "candidate_evidence_capture" not in current_snapshot  # cache remains unchanged
    assert payload == current_snapshot
    assert [message.stock_code for message in pushed_messages] == ["000201"]
    assert len(ws_events) == 1


@pytest.mark.asyncio
async def test_refresh_and_push_skips_feishu_outside_trading_hours(monkeypatch):
    trade_day = date(2026, 4, 23)
    previous_snapshot = {
        "trade_date": str(trade_day),
        "snapshot_time": "2026-04-23T09:40:00",
        "anomalies": [],
        "summary": {"total": 0},
        "b1_states": [],
        "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
    }
    current_snapshot = {
        "trade_date": str(trade_day),
        "snapshot_time": "2026-04-23T20:05:00",
        "anomalies": [_make_capital_anomaly("000202", "盘外股", score=84)],
        "summary": {"total": 1},
        "b1_states": [],
        "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
    }
    pushed_messages = []
    ws_events = []

    async def fake_resolve_latest_trade_date(db, model_field, trade_date=None):
        return trade_day

    async def fake_get_latest_persisted_snapshot(db, target_date):
        return previous_snapshot

    async def fake_prewarm(db, trade_date=None, force_refresh=False):
        return current_snapshot

    async def fake_push_batch(messages):
        pushed_messages.extend(messages)
        return [{"sent": True, "throttled": False, "status": "sent"} for _ in messages]

    async def fake_is_trading_hours():
        return False

    class DummyWsManager:
        async def push_anomaly(self, payload):
            ws_events.append(payload)

    monkeypatch.setattr(tenbagger_module, "resolve_latest_trade_date", fake_resolve_latest_trade_date)
    monkeypatch.setattr(tenbagger_module, "_get_cached_anomaly_snapshot", lambda target_date: None)
    monkeypatch.setattr(tenbagger_module, "_get_latest_persisted_anomaly_snapshot", fake_get_latest_persisted_snapshot)
    monkeypatch.setattr(tenbagger_module, "prewarm_anomaly_snapshot", fake_prewarm)
    monkeypatch.setattr(tenbagger_module.trade_calendar, "is_trading_hours", fake_is_trading_hours)
    monkeypatch.setattr(tenbagger_module.push_scheduler, "push_batch", fake_push_batch)
    monkeypatch.setattr(ws_module, "ws_manager", DummyWsManager())

    payload = await refresh_and_push_anomaly_snapshot(db=None, trade_date=trade_day)

    assert payload.pop("candidate_evidence_capture") == {
        "status": "not_recorded", "reason": "no_database_or_candidates"}
    assert "candidate_evidence_capture" not in current_snapshot  # cache remains unchanged
    assert payload == current_snapshot
    assert pushed_messages == []
    assert len(ws_events) == 1


@pytest.mark.asyncio
async def test_refresh_and_push_does_not_drop_candidates_after_top_200(monkeypatch):
    trade_day = date(2026, 4, 23)
    filler_anomalies = [
        _make_invalid_breakthrough_anomaly(f"{index:06d}", score=1000 - index)
        for index in range(1, 201)
    ]
    tail_candidate = _make_capital_anomaly("600999", "尾部候选", score=84)
    current_snapshot = {
        "trade_date": str(trade_day),
        "snapshot_time": "2026-04-23T10:20:00",
        "anomalies": filler_anomalies + [tail_candidate],
        "summary": {"total": 201},
        "b1_states": [],
        "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
    }
    pushed_messages = []

    async def fake_resolve_latest_trade_date(db, model_field, trade_date=None):
        return trade_day

    async def fake_get_latest_persisted_snapshot(db, target_date):
        return {
            "trade_date": str(trade_day),
            "snapshot_time": "2026-04-23T09:35:00",
            "anomalies": [],
            "summary": {"total": 0},
            "b1_states": [],
            "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
        }

    async def fake_prewarm(db, trade_date=None, force_refresh=False):
        return current_snapshot

    async def fake_push_batch(messages):
        pushed_messages.extend(messages)
        return [{"sent": True, "throttled": False, "status": "sent"} for _ in messages]

    async def fake_is_trading_hours():
        return True

    class DummyWsManager:
        async def push_anomaly(self, payload):
            return None

    monkeypatch.setattr(tenbagger_module, "resolve_latest_trade_date", fake_resolve_latest_trade_date)
    monkeypatch.setattr(tenbagger_module, "_get_cached_anomaly_snapshot", lambda target_date: None)
    monkeypatch.setattr(tenbagger_module, "_get_latest_persisted_anomaly_snapshot", fake_get_latest_persisted_snapshot)
    monkeypatch.setattr(tenbagger_module, "prewarm_anomaly_snapshot", fake_prewarm)
    monkeypatch.setattr(tenbagger_module.trade_calendar, "is_trading_hours", fake_is_trading_hours)
    monkeypatch.setattr(tenbagger_module.push_scheduler, "push_batch", fake_push_batch)
    monkeypatch.setattr(ws_module, "ws_manager", DummyWsManager())

    await refresh_and_push_anomaly_snapshot(db=None, trade_date=trade_day)

    assert [message.stock_code for message in pushed_messages] == ["600999"]


@pytest.mark.asyncio
async def test_refresh_and_push_sends_confirmed_limit_up_when_enabled(monkeypatch):
    trade_day = date(2026, 4, 23)
    limit_up = {
        "code": "000103",
        "name": "首板确认股",
        "event_type": "limit_up",
        "level": "critical",
        "score": 92,
        "description": "首板涨停",
        "detail": {
            "price": 10.8,
            "consecutive_days": 1,
            "volume_ratio": 1.9,
            "turnover": 11.2,
            "seal_quality_score": 82,
            "support_strength_score": 68,
            "orderbook_imbalance": 0.16,
            "bid_depth_5": 36000,
            "ask_depth_5": 22000,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
        },
        "is_one_word_board": False,
    }
    current_snapshot = {
        "trade_date": str(trade_day),
        "snapshot_time": "2026-04-23T10:20:00",
        "anomalies": [limit_up],
        "summary": {"total": 1},
        "b1_states": [],
        "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
    }
    pushed_messages = []

    async def fake_resolve_latest_trade_date(db, model_field, trade_date=None):
        return trade_day

    async def fake_get_latest_persisted_snapshot(db, target_date):
        return {
            "trade_date": str(trade_day),
            "snapshot_time": "2026-04-23T10:19:00",
            "anomalies": [],
            "summary": {"total": 0},
            "b1_states": [],
            "b1_summary": {"total": 0, "intraday_preview": 0, "close_confirmed": 0, "pushable": 0},
        }

    async def fake_prewarm(db, trade_date=None, force_refresh=False):
        return current_snapshot

    async def fake_push_batch(messages):
        pushed_messages.extend(messages)
        return [{"sent": True, "throttled": False, "status": "sent"} for _ in messages]

    async def fake_is_trading_hours():
        return True

    class DummyWsManager:
        async def push_anomaly(self, payload):
            return None

    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_ALLOW_LIMIT_UP", True)
    monkeypatch.setattr(tenbagger_module, "resolve_latest_trade_date", fake_resolve_latest_trade_date)
    monkeypatch.setattr(tenbagger_module, "_get_cached_anomaly_snapshot", lambda target_date: None)
    monkeypatch.setattr(tenbagger_module, "_get_latest_persisted_anomaly_snapshot", fake_get_latest_persisted_snapshot)
    monkeypatch.setattr(tenbagger_module, "prewarm_anomaly_snapshot", fake_prewarm)
    monkeypatch.setattr(tenbagger_module.trade_calendar, "is_trading_hours", fake_is_trading_hours)
    monkeypatch.setattr(tenbagger_module.push_scheduler, "push_batch", fake_push_batch)
    monkeypatch.setattr(ws_module, "ws_manager", DummyWsManager())

    await refresh_and_push_anomaly_snapshot(db=None, trade_date=trade_day)

    assert [message.stock_code for message in pushed_messages] == ["000103"]


def test_anomaly_current_fund_context_rejects_old_eastmoney_cache():
    current_fund_map, fund_source = _resolve_anomaly_current_fund_context({
        "source": "eastmoney_main_fund_stale",
        "items": {
            "000001": {"main_net_inflow": 100_000_000},
        },
    })

    assert current_fund_map == {}
    assert fund_source == "unavailable"


def test_anomaly_current_fund_context_uses_fresh_eastmoney_snapshot():
    items = {
        "000001": {"main_net_inflow": 100_000_000},
    }
    current_fund_map, fund_source = _resolve_anomaly_current_fund_context({
        "source": "eastmoney_main_fund",
        "items": items,
    })

    assert current_fund_map == items
    assert fund_source == "eastmoney_main_fund"


def test_generic_limit_up_without_entry_confirmation_is_filtered():
    anomaly = {
        "code": "000007",
        "name": "普通板",
        "event_type": "limit_up",
        "level": "major",
        "score": 72,
        "description": "涨停",
        "detail": {
            "price": 10.2,
            "consecutive_days": 1,
            "volume_ratio": 1.0,
            "turnover": 2.1,
            "seal_quality_score": 20,
            "support_strength_score": 35,
            "orderbook_imbalance": 0.02,
            "bid_depth_5": 12000,
            "ask_depth_5": 16000,
        },
        "is_one_word_board": False,
    }

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_limit_down_without_low_absorb_confirmation_is_filtered():
    anomaly = {
        "code": "000008",
        "name": "普通跌停",
        "event_type": "limit_down",
        "level": "critical",
        "score": 80,
        "description": "跌停",
        "detail": {
            "price": 6.5,
            "consecutive_days": 1,
            "volume_ratio": 1.6,
            "turnover": 6.5,
            "support_strength_score": 40,
            "orderbook_imbalance": -0.20,
            "bid_depth_5": 9000,
            "ask_depth_5": 22000,
            "withdrawal_ratio": 0.22,
        },
    }

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_pump_dump_push_is_filtered():
    anomaly = {
        "code": "000009",
        "name": "冲高回落股",
        "event_type": "pump_dump",
        "level": "major",
        "score": 82,
        "description": "冲高回落，疑似诱多",
        "detail": {
            "main_net_inflow": 180_000_000,
            "main_net_inflow_pct": 5.2,
            "change_pct": 4.8,
            "volume_ratio": 2.5,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15",
            "is_stale": False,
            "capital_anomaly_type": "pump_dump",
            "confidence": 0.82,
        },
    }

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_only_strong_breakthrough_buy_setup_is_pushed():
    anomaly = {
        "code": "000010",
        "name": "突破强股",
        "event_type": "breakthrough",
        "level": "major",
        "score": 84,
        "description": "突破60日新高，放量确认",
        "detail": {
            "price": 18.8,
            "avg_price": 18.7,
            "change_pct": 4.2,
            "volume_ratio": 2.1,
            "amplitude": 4.6,
            "quality_score": 86,
            "ma_status": "multi_long",
            "pullback_probability": "low",
            "days_near_pressure": 4,
            "support_strength_score": 68,
            "orderbook_imbalance": 0.16,
            "bid_depth_5": 36000,
            "ask_depth_5": 22000,
            "is_false_breakout": False,
            "as_of": "2026-04-15 14:58:30",
            "turnover": 9.6,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 22.0, "change_pct": 2.1, "strength_score": 82}],
        },
    }
    peer_events = [
        anomaly,
        {
            "code": "000010",
            "name": "突破强股",
            "event_type": "capital",
            "score": 82,
            "description": "资金净额 3.8亿",
            "detail": {
                "main_net_inflow": 380_000_000,
                "main_net_inflow_pct": 8.8,
                "change_pct": 4.2,
                "volume_ratio": 2.1,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 36000,
                "ask_depth_5": 22000,
                "orderbook_imbalance": 0.16,
                "support_strength_score": 68,
            },
        },
    ]

    _add_fund_provenance(peer_events[1])

    message = _build_anomaly_push_message(anomaly, peer_events)

    assert message is not None
    assert message.title == "🚀 突破强股 (000010) A1 可直接执行｜突破买点"
    assert "建议入场价" in message.content
    assert "止损价格" in message.content
    assert "伴随资金确认流入" in message.content
    assert "执行等级: A1 可直接执行" in message.content


def test_low_absorb_push_accepts_green_reversal_ma5_pullback():
    anomaly = _make_low_absorb_anomaly()

    message = _build_anomaly_push_message(anomaly, [anomaly])

    assert message is not None
    assert message.title == "🟢 低吸确认股 (000112) A1 可直接执行｜绿盘弱转强低吸 + 回踩MA5低吸"
    assert "异动类型: 绿盘弱转强低吸 + 回踩MA5低吸" in message.content
    assert "上升通道回踩MA5" in message.content
    assert "绿盘下探后回抽" in message.content
    assert "重新站回VWAP附近" in message.content


def test_low_absorb_push_rejects_hot_chase_shape():
    anomaly = _make_low_absorb_anomaly(change_pct=4.2, volume_ratio=3.1)

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_underwater_reversal_reaches_a1_and_feishu_before_chase_zone():
    anomaly = _make_underwater_reversal_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )

    assert enriched["buy_point_grade"] == "A1 可直接执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]


def test_underwater_acceleration_pushes_a2_before_turning_red():
    anomaly = _make_underwater_acceleration_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(selections) == 1
    assert "水下放量急拉预警" in selections[0]["message"].title
    assert "尚未翻红" in selections[0]["message"].content


def test_underwater_acceleration_rejects_without_volume_expansion():
    anomaly = _make_underwater_acceleration_anomaly(volume_ratio=0.95)

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def test_underwater_acceleration_rejects_high_daily_ratio_without_recent_amount():
    anomaly = _make_underwater_acceleration_anomaly(volume_ratio=1.40)
    anomaly["detail"].update({
        "intraday_amount_confirmed": False,
        "intraday_amount_delta": 200_000,
        "intraday_amount_pace_ratio": 0.3,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def test_positive_acceleration_from_two_to_four_pct_is_observation_not_chase_buy():
    anomaly = _make_positive_acceleration_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["setup_grade"] == "B类观察候选"
    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert selected == []
    assert selections == []
    observation = _build_early_observation_push_message(anomaly, [anomaly])
    assert observation is not None
    assert "不是追涨买点" in observation.content


def test_rolling_60s_acceleration_is_volume_confirmed_observation():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.10)
    anomaly["description"] = "60秒放量急拉预警"
    anomaly["detail"].update({
        "signal_label": "60秒放量急拉预警",
        "positive_acceleration_rolling60": True,
        "rolling_60s_alert_pushable": True,
        "price": 10.16,
        "high": 10.17,
        "avg_price": 10.10,
        "change_pct": 1.6,
        "previous_change_pct": 1.1,
        "min5_change": 0.2,
        "scan_change_pct": 0.5,
        "acceleration_pct": 0.62,
        "rolling_60s_confirmed": True,
        "rolling_60s_change_pct": 0.62,
        "rolling_60s_interval_sec": 60.0,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 2.1,
        "rolling_60s_tier": "medium",
        "positive_acceleration_core_confirmation_count": 2,
        "near_high_ratio": 0.999,
        "price_vs_avg_pct": 0.59,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["setup_grade"] == "B类观察候选"
    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert selected == []
    assert selections == []
    observation = _build_early_observation_push_message(anomaly, [anomaly])
    assert observation is not None
    assert observation.extra["observation_only"] is True


def test_pre_limit_acceleration_pushes_observation_before_board_not_buy():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.8)
    anomaly["description"] = "涨停前放量加速预警"
    anomaly["detail"].update({
        "signal_label": "涨停前放量加速预警",
        "positive_acceleration_pre_limit_up": True,
        "price": 10.85,
        "high": 10.87,
        "avg_price": 10.30,
        "change_pct": 8.5,
        "previous_change_pct": 7.8,
        "scan_change_pct": 0.7,
        "acceleration_pct": 0.7,
        "near_high_ratio": 0.9982,
        "turnover": 19.0,
        "amplitude": 11.2,
    })

    observation = _build_early_observation_push_message(anomaly, [anomaly])

    assert observation is not None
    assert observation.extra["observation_only"] is True
    assert observation.extra["buypoint_pushable"] is False
    assert observation.extra["signal_variant"] == "positive_acceleration_pre_limit"
    assert observation.extra["pre_limit_up_confirmed"] is True
    assert "不是追涨买点" in observation.content


def test_rolling_60s_acceleration_rejects_mismatched_amount_confirmation():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.10)
    anomaly["detail"].update({
        "positive_acceleration_rolling60": True,
        "rolling_60s_alert_pushable": True,
        "change_pct": 1.6,
        "acceleration_pct": 0.65,
        "rolling_60s_confirmed": False,
        "rolling_60s_change_pct": 0.65,
        "rolling_60s_interval_sec": 60.0,
        "rolling_60s_amount_delta": 500_000,
        "rolling_60s_amount_pace_ratio": 0.8,
        "rolling_60s_tier": "medium",
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def test_positive_acceleration_catchup_is_observation_when_snapshot_jumps_to_eight_pct():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.8)
    anomaly["description"] = "盘中爆拉追赶预警"
    anomaly["detail"].update({
        "signal_label": "盘中爆拉追赶预警",
        "positive_acceleration_catchup": True,
        "price": 10.78,
        "high": 10.80,
        "change_pct": 7.8,
        "previous_change_pct": 4.8,
        "min5_change": 2.0,
        "scan_change_pct": 3.0,
        "acceleration_pct": 3.0,
        "near_high_ratio": 0.9981,
        "turnover": 19.0,
        "amplitude": 10.8,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["setup_grade"] == "B类观察候选"
    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert selected == []
    assert selections == []
    observation = _build_early_observation_push_message(anomaly, [anomaly])
    assert observation is not None
    assert "已有持仓" in observation.content


def test_positive_acceleration_catchup_is_new_phase_after_regular_alert():
    regular = _make_positive_acceleration_anomaly()
    catchup = _make_positive_acceleration_anomaly(volume_ratio=1.8)
    catchup["description"] = "盘中爆拉追赶预警"
    catchup["detail"].update({
        "signal_label": "盘中爆拉追赶预警",
        "positive_acceleration_catchup": True,
        "price": 10.78,
        "high": 10.80,
        "change_pct": 7.8,
        "previous_change_pct": 4.8,
        "min5_change": 2.0,
        "scan_change_pct": 3.0,
        "acceleration_pct": 3.0,
        "near_high_ratio": 0.9981,
        "turnover": 19.0,
        "amplitude": 10.8,
    })

    selected = _select_pushworthy_anomalies(
        [catchup],
        [regular],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )

    assert selected == []


def test_rolling_acceleration_strong_tier_is_new_phase_after_medium_alert():
    medium = _make_positive_acceleration_anomaly(volume_ratio=1.1)
    strong = _make_positive_acceleration_anomaly(volume_ratio=1.2)
    for anomaly, tier, rise in (
        (medium, "medium", 0.62),
        (strong, "strong", 1.05),
    ):
        anomaly["detail"].update({
            "positive_acceleration_rolling60": True,
            "rolling_60s_alert_pushable": True,
            "rolling_60s_confirmed": True,
            "rolling_60s_path_confirmed": True,
            "rolling_60s_change_pct": rise,
            "rolling_60s_interval_sec": 60.0,
            "rolling_60s_amount_delta": 8_000_000,
            "rolling_60s_amount_pace_ratio": 2.0,
            "rolling_60s_tier": tier,
            "change_pct": 2.0,
            "scan_change_pct": rise,
            "acceleration_pct": rise,
        })

    assert _anomaly_signal_identity(medium) != _anomaly_signal_identity(strong)
    selected = _select_pushworthy_anomalies(
        [strong],
        [medium],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    assert selected == []


def test_positive_acceleration_limit_up_is_observation_without_chase_instruction():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.8)
    anomaly["description"] = "极速封板确认"
    anomaly["detail"].update({
        "signal_label": "极速封板确认",
        "positive_acceleration_limit_up": True,
        "price": 11.0,
        "high": 11.0,
        "change_pct": 10.0,
        "previous_change_pct": 7.8,
        "min5_change": 2.0,
        "scan_change_pct": 2.2,
        "acceleration_pct": 2.2,
        "near_high_ratio": 1.0,
        "turnover": 19.5,
        "amplitude": 11.7,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["setup_grade"] == "B类观察候选"
    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert selected == []
    assert selections == []
    observation = _build_early_observation_push_message(anomaly, [anomaly])
    assert observation is not None
    assert "不是追涨买点" in observation.content


def test_positive_acceleration_rejects_without_volume_expansion():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.0)

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def test_positive_acceleration_rejects_distribution_during_volume_spike():
    anomaly = _make_positive_acceleration_anomaly()
    anomaly["detail"].update({
        "withdrawal_ratio": 0.22,
        "orderbook_imbalance": -0.20,
        "bid_depth_5": 30_000,
        "ask_depth_5": 60_000,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def test_market_wide_strong_acceleration_is_observation_not_buy_point():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.2)
    anomaly["detail"].update({
        "detection_pool_member": False,
        "market_wide_detection": True,
        "positive_acceleration_rolling60": True,
        "rolling_60s_alert_pushable": True,
        "rolling_60s_confirmed": True,
        "rolling_60s_change_pct": 1.08,
        "rolling_60s_interval_sec": 58.0,
        "rolling_60s_amount_delta": 12_000_000,
        "rolling_60s_amount_pace_ratio": 2.6,
        "rolling_60s_tier": "strong",
        "rolling_60s_path_confirmed": True,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    message = _build_early_observation_push_message(anomaly, [anomaly])

    assert enriched["setup_grade"] == "B类观察候选"
    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert message is not None
    assert message.extra["observation_only"] is True
    assert message.extra["buypoint_pushable"] is False
    assert message.extra["signal_variant"] == "positive_acceleration_strong"
    assert message.extra["rapid_rise_strong_confirmed"] is True
    assert "不是追涨买点" in message.content
    assert "执行等级: B类观察候选" in message.content
    assert "执行等级: A2 盘口确认后执行" not in message.content


@pytest.mark.asyncio
async def test_unsent_observation_remains_retryable_while_signal_is_valid():
    anomaly = _make_positive_acceleration_anomaly(volume_ratio=1.2)
    anomaly["detail"].update({
        "market_wide_detection": True,
        "positive_acceleration_rolling60": True,
        "rolling_60s_alert_pushable": True,
        "rolling_60s_confirmed": True,
        "rolling_60s_change_pct": 1.08,
        "rolling_60s_interval_sec": 60.0,
        "rolling_60s_amount_delta": 12_000_000,
        "rolling_60s_amount_pace_ratio": 2.6,
        "rolling_60s_tier": "strong",
        "rolling_60s_path_confirmed": True,
    })

    class EmptyScalars:
        def all(self):
            return []

    class EmptyResult:
        def scalars(self):
            return EmptyScalars()

    class FakeDb:
        async def execute(self, statement):
            return EmptyResult()

    messages = await _select_unsent_observation_recovery_messages(
        FakeDb(),
        date(2026, 8, 14),
        [anomaly],
        [],
    )

    assert len(messages) == 1
    assert messages[0].extra["observation_only"] is True


@pytest.mark.asyncio
async def test_anomaly_trade_date_prefers_fresh_quote_over_stale_sector_table(monkeypatch):
    current_day = date.today()

    class FakeDb:
        async def scalar(self, statement):
            return tenbagger_module.datetime.combine(
                current_day,
                tenbagger_module.datetime.min.time(),
            )

    async def fake_is_trade_day(value):
        return value == current_day

    monkeypatch.setattr(tenbagger_module.trade_calendar, "is_trade_day", fake_is_trade_day)

    assert await _resolve_anomaly_trade_date(FakeDb()) == current_day


def test_trend_support_touch_stays_observation_before_reclaim_confirmation():
    anomaly = _make_trend_support_touch_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["buy_point_grade"] == "B类观察候选"
    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert selected == []
    assert selections == []


def test_main_wave_green_open_reclaim_pushes_a2_with_incremental_volume():
    anomaly = _make_main_wave_green_open_reclaim_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(selections) == 1
    assert "主升浪绿开下杀回收" in selections[0]["message"].title
    assert "滚动60秒增量成交" in selections[0]["message"].content


def test_main_wave_green_open_reclaim_rejects_outflow_or_missing_volume():
    anomaly = _make_main_wave_green_open_reclaim_anomaly()
    anomaly["detail"].update({
        "rolling_60s_confirmed": False,
        "rolling_60s_amount_delta": 0,
        "rolling_60s_amount_pace_ratio": 0,
        "main_net_inflow": -300_000_000,
        "main_net_inflow_pct": -5.5,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert selected == []


def test_main_wave_shape_pullback_reclaim_pushes_only_after_rolling_volume():
    anomaly = _make_main_wave_shape_pullback_reclaim_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(selections) == 1
    assert "主升浪首阴低点回收" in selections[0]["message"].title

    anomaly["detail"].update({
        "rolling_60s_confirmed": False,
        "rolling_60s_amount_delta": 0,
        "rolling_60s_amount_pace_ratio": 0,
    })
    assert _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    ) == []


def test_tenbagger_pullback_is_pushable_only_with_non_chasing_rolling_volume():
    anomaly = _make_low_absorb_anomaly(
        code="603162",
        name="海通发展",
        change_pct=-0.5,
        volume_ratio=1.2,
        support_strength_score=73,
    )
    anomaly["score"] = 84
    anomaly["description"] = "牛股潜质支撑回收"
    anomaly["detail"].update({
        "signal_type": "trend_driver_low_absorb",
        "signal_label": "牛股潜质支撑回收",
        "low_absorb_type": "trend_driver_support_reclaim",
        "trend_setup_confirmed": True,
        "tenbagger_pullback_confirmed": True,
        "tenbagger_score": 78.5,
        "potential_quality_score": 72.0,
        "detection_pool_member": True,
        "support_gap_pct": 0.7,
        "intraday_rebound_pct": 1.1,
        "price_vs_avg_pct": 0.1,
        "main_net_inflow": 80_000_000,
        "main_net_inflow_pct": 3.2,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.46,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.6,
        "sector_factors": [{
            "sector_name": "航运",
            "fund_flow": 12.0,
            "change_pct": 1.3,
            "strength_score": 74,
            "limit_up_count": 1,
        }],
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]

    anomaly["detail"]["change_pct"] = 2.1
    assert _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    ) == []


def test_main_wave_shape_pullback_does_not_push_without_core_sector_or_large_order():
    anomaly = _make_main_wave_shape_pullback_reclaim_anomaly()
    anomaly["detail"]["large_order_inflow_confirmed"] = False
    assert _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    ) == []

    anomaly = _make_main_wave_shape_pullback_reclaim_anomaly()
    anomaly["detail"]["main_wave_core_sector_confirmed"] = False
    assert _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    ) == []


def test_main_wave_intraday_support_reclaim_pushes_a2_only_after_rolling_volume():
    anomaly = _make_main_wave_shape_pullback_reclaim_anomaly()
    anomaly["description"] = "主升浪支撑回收"
    anomaly["detail"].update({
        "signal_type": "main_wave_support_reclaim",
        "signal_label": "主升浪支撑回收",
        "low_absorb_type": "main_wave_support_reclaim",
        "main_wave_shape_pullback_confirmed": False,
        "main_wave_shape_pullback_rolling60": False,
        "main_wave_support_reclaim_confirmed": True,
        "main_wave_support_reclaim_rolling60": True,
        "pullback_shape_type": "",
        "pullback_shape_label": "主升浪趋势支撑",
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]

    anomaly["detail"].update({
        "rolling_60s_confirmed": False,
        "rolling_60s_amount_delta": 0,
        "rolling_60s_amount_pace_ratio": 0,
    })
    assert _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    ) == []


def test_second_wave_restart_pushes_a2_after_high_board_selloff():
    anomaly = _make_second_wave_restart_anomaly()

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selected = _select_pushworthy_anomalies(
        [anomaly],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )
    selections = _build_push_message_candidates(
        selected,
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert selected == [anomaly]
    assert len(selections) == 1
    assert "高标下杀二波启动预警" in selections[0]["message"].title
    assert "不在连续下跌途中直接抄底" in selections[0]["message"].content


def test_second_wave_restart_rejects_without_recent_amount_confirmation():
    anomaly = _make_second_wave_restart_anomaly()
    anomaly["detail"].update({
        "intraday_amount_confirmed": False,
        "intraday_amount_delta": 0,
        "intraday_amount_pace_ratio": 0,
    })

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False


def test_underwater_reversal_fund_degraded_keeps_a2_light_alert():
    anomaly = _make_underwater_reversal_anomaly(
        source="tencent_realtime",
        fund_data_degraded=True,
    )

    enriched = _enrich_anomaly_display(anomaly, [anomaly])
    selections = _build_push_message_candidates(
        [anomaly],
        min_grade="A2 盘口确认后执行",
    )

    assert enriched["buy_point_grade"] == "A2 盘口确认后执行"
    assert enriched["buy_point_pushable"] is True
    assert enriched["feishu_pushable"] is True
    assert len(selections) == 1
    assert selections[0]["message"].extra["alert_tier"] == "light"


def test_underwater_reversal_rejects_after_fast_chase_window():
    anomaly = _make_underwater_reversal_anomaly(change_pct=6.2)
    anomaly["detail"]["price"] = 10.62

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["buy_point_pushable"] is False
    assert enriched["feishu_pushable"] is False
    assert any("提醒窗口" in reason for reason in enriched["buy_point_blockers"])


def test_low_absorb_push_rejects_heavy_outflow_without_strong_sector():
    anomaly = _make_low_absorb_anomaly()
    anomaly["detail"]["main_net_inflow"] = -240_000_000
    anomaly["detail"]["main_net_inflow_pct"] = -9.8
    anomaly["detail"]["sector_factors"] = [
        {"sector_name": "弱板块", "fund_flow": 9.4, "change_pct": 0.01, "strength_score": 35, "limit_up_count": 1}
    ]

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_low_absorb_generic_fund_label_without_proof_is_not_push_candidate():
    anomaly = _make_low_absorb_anomaly()
    anomaly["detail"]["source"] = "fund_flow"
    for field in (
        "code", "trade_date", "provider_source", "source_version",
        "source_quote_at", "received_at", "observed_at",
    ):
        anomaly["detail"].pop(field)

    selections = _build_push_message_candidates(
        [anomaly],
        min_grade="A2 盘口确认后执行",
    )

    assert selections == []


def test_b_grade_upgrade_to_a_is_selected_for_push():
    previous = [
        {
            "code": "000011",
            "name": "升级股",
            "event_type": "capital",
            "level": "major",
            "score": 78,
            "description": "资金净额 3.0亿",
            "detail": {
                    "main_net_inflow": 300_000_000,
                    "price": 14.8,
                    "avg_price": 14.7,
                    "main_net_inflow_pct": 7.2,
                "change_pct": 2.6,
                "volume_ratio": 1.6,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 26000,
                "ask_depth_5": 19000,
                "orderbook_imbalance": 0.11,
                "support_strength_score": 60,
                "turnover": 9.5,
                "amplitude": 4.5,
                "sector_factors": [{"sector_name": "机器人", "fund_flow": 8.2, "change_pct": 1.1}],
            },
        }
    ]
    current = [
        {
            "code": "000011",
            "name": "升级股",
            "event_type": "capital",
            "level": "critical",
            "score": 88,
            "description": "资金净额 3.0亿",
            "detail": {
                    "main_net_inflow": 300_000_000,
                    "price": 15.2,
                    "avg_price": 15.1,
                    "main_net_inflow_pct": 7.2,
                "change_pct": 4.1,
                "volume_ratio": 2.2,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 42000,
                "ask_depth_5": 22000,
                "orderbook_imbalance": 0.22,
                "support_strength_score": 71,
                "turnover": 9.5,
                "amplitude": 4.5,
                "sector_factors": [{"sector_name": "机器人", "fund_flow": 8.2, "change_pct": 1.1}],
            },
        },
        {
            "code": "000011",
            "name": "升级股",
            "event_type": "breakthrough",
            "level": "major",
            "score": 80,
            "description": "突破60日新高",
            "detail": {"price": 15.2, "volume_ratio": 2.0},
        },
    ]

    for event in [*previous, *current]:
        if event["event_type"] == "capital":
            _add_fund_provenance(event)

    selected = _select_pushworthy_anomalies(current, previous)

    assert len(selected) == 1
    assert selected[0]["code"] == "000011"


def test_a2_upgrade_to_a1_is_selected_for_push():
    previous = [
        {
            "code": "000012",
            "name": "进阶股",
            "event_type": "breakthrough",
            "level": "major",
            "score": 82,
            "description": "突破60日新高，放量确认",
                "detail": {
                    "price": 20.1,
                    "avg_price": 20.0,
                "change_pct": 4.9,
                "volume_ratio": 2.0,
                "amplitude": 4.8,
                "quality_score": 86,
                "ma_status": "multi_long",
                "pullback_probability": "low",
                "days_near_pressure": 4,
                "support_strength_score": 68,
                "orderbook_imbalance": 0.16,
                "bid_depth_5": 36000,
                "ask_depth_5": 22000,
                "is_false_breakout": False,
                "as_of": "2026-04-15 14:40:00",
                "turnover": 10.2,
                "sector_factors": [{"sector_name": "算力", "fund_flow": 15.0, "change_pct": 1.5}],
            },
        },
        {
            "code": "000012",
            "name": "进阶股",
            "event_type": "capital",
            "score": 80,
            "description": "资金净额 3.5亿",
            "detail": {
                    "main_net_inflow": 350_000_000,
                    "price": 20.1,
                    "avg_price": 20.0,
                    "main_net_inflow_pct": 8.2,
                "change_pct": 4.9,
                "volume_ratio": 2.0,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 36000,
                "ask_depth_5": 22000,
                "orderbook_imbalance": 0.16,
                "support_strength_score": 68,
                "turnover": 10.2,
                "amplitude": 4.8,
                "sector_factors": [{"sector_name": "算力", "fund_flow": 15.0, "change_pct": 1.5}],
            },
        },
    ]
    current = [
        {
            "code": "000012",
            "name": "进阶股",
            "event_type": "breakthrough",
            "level": "major",
            "score": 86,
            "description": "突破60日新高，放量确认",
                "detail": {
                    "price": 20.1,
                    "avg_price": 20.0,
                "change_pct": 4.4,
                "volume_ratio": 2.1,
                "amplitude": 4.4,
                "quality_score": 86,
                "ma_status": "multi_long",
                "pullback_probability": "low",
                "days_near_pressure": 4,
                "support_strength_score": 68,
                "orderbook_imbalance": 0.16,
                "bid_depth_5": 36000,
                "ask_depth_5": 22000,
                "is_false_breakout": False,
                "as_of": "2026-04-15 14:58:00",
                "turnover": 10.2,
                "sector_factors": [{"sector_name": "算力", "fund_flow": 15.0, "change_pct": 1.5}],
            },
        },
        {
            "code": "000012",
            "name": "进阶股",
            "event_type": "capital",
            "score": 82,
            "description": "资金净额 3.5亿",
            "detail": {
                    "main_net_inflow": 350_000_000,
                    "price": 20.1,
                    "avg_price": 20.0,
                    "main_net_inflow_pct": 8.2,
                "change_pct": 4.4,
                "volume_ratio": 2.1,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 36000,
                "ask_depth_5": 22000,
                "orderbook_imbalance": 0.16,
                "support_strength_score": 68,
                "turnover": 10.2,
                "amplitude": 4.4,
                "sector_factors": [{"sector_name": "算力", "fund_flow": 15.0, "change_pct": 1.5}],
            },
        },
    ]

    for event in [*previous, *current]:
        if event["event_type"] == "capital":
            _add_fund_provenance(event)

    selected = _select_pushworthy_anomalies(current, previous)
    messages = _build_push_messages_for_anomalies(selected, min_grade="A1 可直接执行")

    assert len(messages) == 1
    assert messages[0].stock_code == "000012"
    assert messages[0].title == "🚀 进阶股 (000012) A1 可直接执行｜突破买点"


def test_same_signal_amount_change_does_not_repeat_push():
    previous = [
        {
            "code": "000015",
            "name": "不应重复股",
            "event_type": "capital",
            "level": "critical",
            "score": 88,
            "description": "资金净额 3.0亿",
            "detail": {
                "main_net_inflow": 300_000_000,
                "main_net_inflow_pct": 9.0,
                "change_pct": 3.6,
                "volume_ratio": 2.1,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15 10:00:00",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 30000,
                "ask_depth_5": 18000,
                "orderbook_imbalance": 0.20,
                "support_strength_score": 72,
                "turnover": 8.2,
                "amplitude": 4.3,
                "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
            },
        }
    ]
    current = [
        {
            "code": "000015",
            "name": "不应重复股",
            "event_type": "capital",
            "level": "critical",
            "score": 90,
            "description": "资金净额 3.4亿",
            "detail": {
                "main_net_inflow": 340_000_000,
                "main_net_inflow_pct": 10.1,
                "change_pct": 3.7,
                "volume_ratio": 2.2,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15 10:30:00",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 32000,
                "ask_depth_5": 17000,
                "orderbook_imbalance": 0.23,
                "support_strength_score": 73,
                "turnover": 8.2,
                "amplitude": 4.3,
                "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
            },
        }
    ]

    _add_fund_provenance(previous[0])
    _add_fund_provenance(current[0])

    assert _anomaly_signal_identity(previous[0]) == _anomaly_signal_identity(current[0])
    selected = _select_pushworthy_anomalies(current, previous)
    assert selected == []


def test_grade_upgrade_still_pushes_when_description_changes():
    previous = [
        {
            "code": "000016",
            "name": "升级不漏股",
            "event_type": "capital",
            "level": "major",
            "score": 78,
            "description": "资金净额 2.8亿",
            "detail": {
                    "main_net_inflow": 280_000_000,
                    "price": 12.0,
                    "avg_price": 11.9,
                    "main_net_inflow_pct": 7.0,
                "change_pct": 2.4,
                "volume_ratio": 1.7,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15 10:00:00",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 22000,
                "ask_depth_5": 18000,
                "orderbook_imbalance": 0.12,
                "support_strength_score": 60,
                "turnover": 9.0,
                "amplitude": 4.4,
                "sector_factors": [{"sector_name": "机器人", "fund_flow": 8.2, "change_pct": 1.1}],
            },
        }
    ]
    current = [
        {
            "code": "000016",
            "name": "升级不漏股",
            "event_type": "capital",
            "level": "critical",
            "score": 88,
            "description": "资金净额 3.2亿",
            "detail": {
                    "main_net_inflow": 320_000_000,
                    "price": 12.3,
                    "avg_price": 12.2,
                    "main_net_inflow_pct": 8.6,
                "change_pct": 4.0,
                "volume_ratio": 2.2,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-15 10:30:00",
                "is_stale": False,
                "capital_anomaly_type": "main_inflow",
                "bid_depth_5": 42000,
                "ask_depth_5": 22000,
                "orderbook_imbalance": 0.22,
                "support_strength_score": 71,
                "turnover": 9.0,
                "amplitude": 4.4,
                "sector_factors": [{"sector_name": "机器人", "fund_flow": 8.2, "change_pct": 1.1}],
            },
        },
        {
            "code": "000016",
            "name": "升级不漏股",
            "event_type": "breakthrough",
            "level": "major",
            "score": 80,
            "description": "突破60日新高",
            "detail": {"price": 12.3, "volume_ratio": 2.1},
        },
    ]

    for event in [*previous, *current]:
        if event["event_type"] == "capital":
            _add_fund_provenance(event)

    selected = _select_pushworthy_anomalies(current, previous)
    assert len(selected) == 1
    assert selected[0]["code"] == "000016"


def test_high_board_break_and_heavy_volume_is_filtered_from_buy_push():
    anomaly = {
        "code": "002364",
        "name": "中恒电气",
        "event_type": "capital",
        "level": "critical",
        "score": 90,
        "description": "资金净额 6.9亿",
        "detail": {
            "main_net_inflow": 687_000_000,
            "main_net_inflow_pct": 10.56,
            "change_pct": 2.14,
            "volume_ratio": 5.37,
            "turnover": 27.79,
            "amplitude": 5.3,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "support_strength_score": 48.13,
            "orderbook_imbalance": -0.2556,
            "bid_depth_5": 466,
            "ask_depth_5": 786,
            "max_recent_consecutive_days": 5,
            "last_break_count": 1,
            "sector_factors": [{"sector_name": "风电", "fund_flow": -131.02, "change_pct": -0.27}],
        },
    }

    _add_fund_provenance(anomaly)

    assert _build_anomaly_push_message(anomaly, [anomaly]) is None


def test_display_grade_marks_a1_trend_capital_style():
    anomaly = {
        "code": "000013",
        "name": "趋势资金股",
        "event_type": "capital",
        "level": "critical",
        "score": 88,
        "description": "资金净额 6.0亿",
        "detail": {
            "main_net_inflow": 600_000_000,
            "price": 12.3,
            "avg_price": 12.2,
            "main_net_inflow_pct": 9.2,
            "change_pct": 3.8,
            "volume_ratio": 2.4,
            "source": "eastmoney_main_fund",
            "as_of": "2026-04-15",
            "is_stale": False,
            "capital_anomaly_type": "main_inflow",
            "support_strength_score": 72,
            "orderbook_imbalance": 0.18,
            "bid_depth_5": 52000,
            "ask_depth_5": 26000,
            "turnover": 8.5,
            "amplitude": 4.2,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 12.5, "change_pct": 1.8}],
        },
    }
    _add_fund_provenance(anomaly)
    peer_events = [
        anomaly,
        {
            "code": "000013",
            "name": "趋势资金股",
            "event_type": "breakthrough",
            "score": 82,
            "detail": {"price": 12.3, "volume_ratio": 2.1},
            "description": "突破60日新高",
        },
    ]

    enriched = _enrich_anomaly_display(anomaly, peer_events)

    assert enriched["setup_grade"] == "A1 可直接执行"
    assert enriched["setup_track"] == "趋势/资金型"
    assert enriched["setup_grade_display"] == "A1-趋势/资金型"
    assert enriched["buy_point_reached"] is True
    assert enriched["buy_point_pushable"] is True
    assert enriched["buy_point_label"] == "资金确认流入"


def test_display_grade_marks_a1_limit_up_style():
    anomaly = {
        "code": "000014",
        "name": "打板确认股",
        "event_type": "limit_up",
        "level": "critical",
        "score": 92,
        "description": "2连板涨停",
        "detail": {
            "price": 12.8,
            "consecutive_days": 1,
            "volume_ratio": 1.9,
            "turnover": 13.5,
            "seal_quality_score": 82,
            "support_strength_score": 68,
            "orderbook_imbalance": 0.16,
            "bid_depth_5": 36000,
            "ask_depth_5": 22000,
        },
        "is_one_word_board": False,
    }

    enriched = _enrich_anomaly_display(anomaly, [anomaly])

    assert enriched["setup_grade"] == "A1 可直接执行"
    assert enriched["setup_track"] == "打板型"
    assert enriched["setup_grade_display"] == "A1-打板型"


def test_display_feishu_pushable_matches_limit_up_policy(monkeypatch):
    anomaly = {
        "code": "000014",
        "name": "打板确认股",
        "event_type": "limit_up",
        "level": "critical",
        "score": 92,
        "description": "首板涨停",
        "detail": {
            "price": 12.8,
            "consecutive_days": 1,
            "volume_ratio": 1.9,
            "turnover": 13.5,
            "seal_quality_score": 82,
            "support_strength_score": 68,
            "orderbook_imbalance": 0.16,
            "bid_depth_5": 36000,
            "ask_depth_5": 22000,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2, "strength_score": 70}],
        },
        "is_one_word_board": False,
    }

    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_ALLOW_LIMIT_UP", False)
    assert _enrich_anomaly_display(anomaly, [anomaly])["feishu_pushable"] is False

    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_ALLOW_LIMIT_UP", True)
    assert _enrich_anomaly_display(anomaly, [anomaly])["feishu_pushable"] is True


@pytest.mark.asyncio
async def test_unsent_push_recovery_only_backfills_once(monkeypatch):
    trade_day = date(2026, 4, 23)
    anomaly = {
        "code": "000014",
        "name": "漏推恢复股",
        "event_type": "limit_up",
        "level": "critical",
        "score": 92,
        "description": "首板涨停",
        "detail": {
            "price": 12.8,
            "consecutive_days": 1,
            "volume_ratio": 1.9,
            "turnover": 13.5,
            "seal_quality_score": 82,
            "support_strength_score": 68,
            "orderbook_imbalance": 0.16,
            "bid_depth_5": 36000,
            "ask_depth_5": 22000,
            "sector_factors": [{"sector_name": "算力", "fund_flow": 8.0, "change_pct": 1.2}],
        },
        "is_one_word_board": False,
    }

    class DummyScalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class DummyResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return DummyScalars(self.values)

    class DummyDb:
        def __init__(self, values):
            self.values = values

        async def execute(self, statement):
            return DummyResult(self.values)

    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_ALLOW_LIMIT_UP", True)
    unsent = await tenbagger_module._select_unsent_push_recovery_candidates(
        DummyDb([]),
        trade_day,
        [anomaly],
        [],
    )
    identity = tenbagger_module._anomaly_signal_identity(anomaly)
    signal_id = tenbagger_module._anomaly_signal_record_id(trade_day, "000014", identity)
    sent = await tenbagger_module._select_unsent_push_recovery_candidates(
        DummyDb([signal_id]),
        trade_day,
        [anomaly],
        [],
    )

    assert [item["code"] for item in unsent] == ["000014"]
    assert sent == []


def test_select_pushworthy_anomalies_detects_buypoint_arrival_without_grade_upgrade():
    previous_capital = _make_capital_anomaly(
        "000221",
        "买点到达股",
        source="fund_flow",
        score=84,
        change_pct=4.4,
        volume_ratio=1.6,
        support_strength_score=59,
        main_net_inflow_pct=5.3,
        main_net_inflow=520_000_000,
    )
    previous_capital["detail"]["sector_factors"] = [
        {"sector_name": "机器人", "fund_flow": -1.2, "change_pct": -0.3}
    ]
    current_capital = _make_capital_anomaly(
        "000221",
        "买点到达股",
        source="fund_flow",
        score=84,
        change_pct=4.4,
        volume_ratio=1.6,
        support_strength_score=59,
        main_net_inflow_pct=5.3,
        main_net_inflow=520_000_000,
    )
    current_capital["detail"]["sector_factors"] = [
        {"sector_name": "机器人", "fund_flow": -1.2, "change_pct": -0.3}
    ]
    breakthrough = _make_breakthrough_anomaly(
        "000221",
        "买点到达股",
        score=83,
        quality_score=82,
        change_pct=4.6,
        volume_ratio=1.6,
        support_strength_score=62,
        days_near_pressure=3,
    )
    breakthrough["detail"]["sector_factors"] = [
        {"sector_name": "机器人", "fund_flow": -1.2, "change_pct": -0.3}
    ]

    selected = _select_pushworthy_anomalies(
        [current_capital, breakthrough],
        [previous_capital],
        min_grade="A2 盘口确认后执行",
    )

    assert any(item["event_type"] == "capital" for item in selected)


def test_live_push_gate_keeps_a2_only_for_low_base_watchlist():
    normal = _make_capital_anomaly("000301", "普通A2", source="fund_flow", score=84, support_strength_score=69)
    watched_member = _make_capital_anomaly("600351", "仅观察池A2", source="fund_flow", score=84, support_strength_score=69)
    watched_member["detail"]["watchlist_member"] = True
    watched = _make_capital_anomaly("603998", "形态确认A2", source="fund_flow", score=84, support_strength_score=69)
    watched["detail"].update({
        "watchlist_member": True,
        "low_base_watchlist": True,
        "low_base_setup_confirmed": True,
        "signal_type": "low_base_breakthrough",
    })

    selected = _select_pushworthy_anomalies(
        [normal, watched_member, watched],
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=True,
    )

    assert [item["code"] for item in selected] == ["603998"]


def test_breakthrough_signal_identity_uses_stable_platform_anchor():
    first = _make_breakthrough_anomaly("600351", "稳定身份")
    first["detail"].update({
        "signal_type": "low_base_breakthrough",
        "high": 10.28,
        "high_20d": 10.0,
    })
    second = _make_breakthrough_anomaly("600351", "稳定身份")
    second["detail"].update({
        "signal_type": "low_base_breakthrough",
        "high": 10.45,
        "high_20d": 10.0,
    })

    assert _anomaly_signal_identity(first) == _anomaly_signal_identity(second)


def test_automatic_push_cap_is_global_and_dedupes_stock(monkeypatch):
    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_MAX_PER_SCAN", 2)
    primary = PushMessage(
        title="主异动",
        content="内容",
        stock_code="600351",
        priority=9,
        category="anomaly",
        extra={"watchlist_member": True},
    )
    duplicate_b1 = PushMessage(
        title="同股B1",
        content="内容",
        stock_code="600351",
        priority=8,
        category="anomaly",
        extra={},
    )
    other_b1 = PushMessage(
        title="其他B1",
        content="内容",
        stock_code="000001",
        priority=7,
        category="anomaly",
        extra={},
    )
    third = PushMessage(
        title="第三条",
        content="内容",
        stock_code="000002",
        priority=6,
        category="anomaly",
        extra={},
    )

    selected = _cap_automatic_push_messages([primary], [duplicate_b1, other_b1, third])

    assert [message.stock_code for message in selected] == ["600351", "000001"]


def test_automatic_push_cap_reserves_scan_slots_for_a1_before_a2(monkeypatch):
    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_MAX_PER_SCAN", 1)
    a2_watch = PushMessage(
        title="A2观察池",
        content="内容",
        stock_code="600351",
        priority=9,
        category="anomaly",
        extra={"watchlist_member": True, "setup_grade": "A2 盘口确认后执行"},
    )
    a1_buy = PushMessage(
        title="A1买点",
        content="内容",
        stock_code="000001",
        priority=8,
        category="anomaly",
        extra={"setup_grade": "A1 可直接执行"},
    )

    selected = _cap_automatic_push_messages([a2_watch, a1_buy], [])

    assert [message.stock_code for message in selected] == ["000001"]


def test_automatic_push_focus_marks_only_best_non_chasing_buy_as_core(monkeypatch):
    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_CORE_MAX_PER_SCAN", 1)
    messages = [
        PushMessage(
            title="主升回踩A",
            content="原始内容A",
            stock_code="000201",
            priority=8,
            category="anomaly",
            extra={
                "setup_grade": "A1 可直接执行",
                "event_type": "low_absorb",
                "signal_variant": "main_wave_shape_pullback_reclaim",
                "buypoint_pushable": True,
                "detection_pool_member": True,
                "change_pct": 0.8,
                "signal_score": 92,
                "speculation_type": "趋势回踩",
                "speculation_logic": "主升结构支撑回收",
                "continuation_condition": "支撑和VWAP继续有效",
                "invalidation_condition": "放量跌破支撑",
                "performance_evidence": {"sample_count": 12, "shadow_mode": True},
            },
        ),
        PushMessage(
            title="主升回踩B",
            content="原始内容B",
            stock_code="000202",
            priority=8,
            category="anomaly",
            extra={
                "setup_grade": "A2 盘口确认后执行",
                "event_type": "low_absorb",
                "signal_variant": "main_wave_support_reclaim",
                "buypoint_pushable": True,
                "detection_pool_member": True,
                "change_pct": 1.6,
                "signal_score": 84,
                "speculation_type": "趋势回踩",
                "speculation_logic": "趋势支撑回收",
                "continuation_condition": "量价继续确认",
                "invalidation_condition": "跌破支撑",
                "performance_evidence": {"sample_count": 8, "shadow_mode": True},
            },
        ),
        PushMessage(
            title="强急拉观察",
            content="观察内容",
            stock_code="000203",
            priority=9,
            category="anomaly",
            extra={
                "setup_grade": "B类观察候选",
                "event_type": "breakthrough",
                "signal_variant": "positive_acceleration_strong",
                "buypoint_pushable": False,
                "observation_only": True,
                "change_pct": 5.2,
                "signal_score": 96,
                "speculation_type": "盘中动能观察",
            },
        ),
    ]

    annotated = tenbagger_module._annotate_automatic_push_focus(messages)

    assert [item.extra["focus_tier"] for item in annotated] == [
        "core",
        "conditional",
        "observation",
    ]
    assert annotated[0].title.startswith("🔥 重点买点｜")
    assert annotated[1].title.startswith("🟡 条件买点｜")
    assert annotated[2].title.startswith("⚡ 异动观察｜")
    assert "本次按结构优先级区分，不虚报胜率" in annotated[0].content
    assert "**🧭 炒作预期**" in annotated[0].content
    assert "失效条件: 放量跌破支撑" in annotated[0].content


def test_mature_route_below_core_win_rate_stays_conditional(monkeypatch):
    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_EVAL_MIN_SAMPLES", 30)
    monkeypatch.setattr(tenbagger_module.settings, "ANOMALY_PUSH_CORE_MIN_WIN_RATE", 0.62)
    message = PushMessage(
        title="成熟但非最优",
        content="原始内容",
        stock_code="000204",
        priority=8,
        category="anomaly",
        extra={
            "setup_grade": "A1 可直接执行",
            "event_type": "low_absorb",
            "signal_variant": "main_wave_shape_pullback_reclaim",
            "buypoint_pushable": True,
            "detection_pool_member": True,
            "change_pct": 0.5,
            "signal_score": 95,
            "performance_evidence": {
                "sample_count": 30,
                "win_rate_3d": 0.60,
                "avg_net_return_3d": 1.2,
                "avg_excess_return_3d": 0.8,
            },
        },
    )

    annotated = tenbagger_module._annotate_automatic_push_focus([message])

    assert annotated[0].extra["focus_tier"] == "conditional"
    assert annotated[0].title.startswith("🟡 条件买点｜")
    assert "3日净胜率60%" in annotated[0].content


def test_speculation_expectation_distinguishes_event_relay_and_trend_pullback():
    event_context = tenbagger_module._resolve_speculation_expectation_context({
        "event_type": "breakthrough",
        "detail": {
            "signal_type": "event_relay_confirmation",
            "event_title": "重大资产重组获审核通过",
        },
    })
    trend_context = tenbagger_module._resolve_speculation_expectation_context({
        "event_type": "low_absorb",
        "detail": {
            "signal_type": "main_wave_shape_pullback_reclaim",
            "sector_factors": [{
                "sector_name": "PCB",
                "sector_type": "concept",
                "source": "pywencai",
            }],
        },
    })

    assert event_context["speculation_type"] == "事件接力"
    assert "重大资产重组" in event_context["speculation_logic"]
    assert trend_context["speculation_type"] == "趋势回踩"
    assert "PCB" in trend_context["speculation_logic"]


def test_b1_push_eligibility_requires_trend_or_history_support():
    pushable, blockers = _resolve_b1_push_eligibility(
        {
            "b1_signal_key": "volume_b1",
            "b1_signal_status": "intraday_preview",
            "b1_hold_score": 4,
            "b1_break_trend": False,
            "b1_success_rate_3d": 0.6,
            "b1_success_samples": 5,
        }
    )

    assert pushable is True
    assert blockers == []

    pushable, blockers = _resolve_b1_push_eligibility(
        {
            "b1_signal_key": "volume_b1",
            "b1_signal_status": "intraday_preview",
            "b1_hold_score": 2,
            "b1_break_trend": True,
            "b1_success_rate_3d": 0.48,
            "b1_success_samples": 5,
        }
    )

    assert pushable is False
    assert "持股分数仅 2 分" in blockers
    assert "已跌破趋势白线" in blockers
    assert "3日修复率不足 55%" in blockers


def test_b1_state_changes_capture_confirm_and_invalidation():
    previous_states = [
        {
            "code": "000001",
            "name": "测试股",
            "signal_key": "volume_b1",
            "signal_label": "缩量B1",
            "signal_status": "intraday_preview",
            "pushable": True,
            "priority_score": 122.0,
            "latest_as_of": "2026-04-21T10:02:00",
        },
        {
            "code": "000002",
            "name": "失效股",
            "signal_key": "white_line_retest_b1",
            "signal_label": "回踩白线B1",
            "signal_status": "intraday_preview",
            "pushable": True,
            "priority_score": 118.0,
            "latest_as_of": "2026-04-21T10:01:00",
            "push_blockers": [],
        },
    ]
    current_states = [
        {
            "code": "000001",
            "name": "测试股",
            "signal_key": "volume_b1",
            "signal_label": "缩量B1",
            "signal_status": "close_confirmed",
            "pushable": True,
            "priority_score": 125.0,
            "latest_as_of": "2026-04-21T15:00:00",
        },
        {
            "code": "000003",
            "name": "新触发股",
            "signal_key": "super_volume_b1",
            "signal_label": "超级缩量B1",
            "signal_status": "intraday_preview",
            "pushable": True,
            "priority_score": 119.0,
            "latest_as_of": "2026-04-21T14:12:00",
        },
    ]

    changes = _select_b1_state_changes(current_states, previous_states)

    assert [item["transition"] for item in changes] == ["confirmed", "appeared", "invalidated"]
    assert changes[0]["current"]["code"] == "000001"
    assert changes[1]["current"]["code"] == "000003"
    assert changes[2]["previous"]["code"] == "000002"


def test_b1_state_push_message_marks_confirmed_and_invalidated():
    confirmed_message = _build_b1_state_push_message(
        {
            "transition": "confirmed",
            "signal_status": "close_confirmed",
            "current": {
                "code": "000001",
                "name": "测试股",
                "signal_key": "volume_b1",
                "signal_label": "缩量B1",
                "signal_status": "close_confirmed",
                "current_price": 12.34,
                "day_change_pct": 2.18,
                "turnover_rate": 6.2,
                "main_net_inflow": 180_000_000,
                "j": 12.3,
                "rsi": 19.6,
                "short_score": 14.0,
                "long_score": 82.0,
                "hold_score": 4,
                "success_rate_3d": 0.67,
                "success_rate_5d": 0.83,
                "success_samples": 6,
                "trend_white_price": 12.05,
                "break_trend": False,
                "setup_grade": "A2 盘口确认后执行",
                "setup_track": "趋势/资金型",
                "primary_reason": "快速拉升叠加缩量回踩",
                "push_blockers": [],
            },
            "previous": {
                "code": "000001",
                "name": "测试股",
                "signal_key": "volume_b1",
                "signal_label": "缩量B1",
                "signal_status": "intraday_preview",
                "pushable": True,
            },
        }
    )
    invalidated_message = _build_b1_state_push_message(
        {
            "transition": "invalidated",
            "signal_status": "invalidated",
            "current": None,
            "previous": {
                "code": "000002",
                "name": "失效股",
                "signal_key": "white_line_retest_b1",
                "signal_label": "回踩白线B1",
                "signal_status": "intraday_preview",
                "current_price": 10.88,
                "day_change_pct": -1.75,
                "trend_white_price": 11.02,
                "primary_reason": "盘中回踩趋势白线",
                "push_blockers": ["已跌破趋势白线"],
            },
        }
    )

    assert confirmed_message is not None
    assert "收盘确认" in confirmed_message.title
    assert "3日修复率" in confirmed_message.content
    assert invalidated_message is None
