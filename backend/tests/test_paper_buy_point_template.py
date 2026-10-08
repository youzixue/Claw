"""买点卡片版式回归；只构建JSON，不连接飞书或交易接口。"""
import copy
import json
from datetime import datetime

import pytest

from app.paper.account_policy import ACCOUNT_NAMES
from app.push.channels.base import PushMessage
from app.push.channels.feishu import FeishuChannel
from app.push import paper_buy_points as points
from app.push.throttle import PushThrottle


def item(account="challenger_b", ident=1, **overrides):
    result = {
        "id": ident, "run_id": f"bp-test-{ident}", "account_id": ident,
        "code": "600001", "name": "模板测试", "price": 10.5,
        "created_at": datetime(2026, 9, 8, 9, 40, 10),
        "as_of_at": datetime(2026, 9, 8, 9, 40),
        "strategy_version": "test-version",
        "reason": "昨日首板，弱开后收复零轴；不可变confirmed事件09:40:00，执行轮次再次校验通过；现价10.50；VWAP10.40；量比2.80；盘口失衡0.00",
        "payload": {
            "account_name": account, "strategy_label": f"策略-{account}",
            "account_role": "次账户" if account.startswith("challenger_") else "主账户",
            "execution_status": "策略入场确认；资金/持仓另审，非下单或成交",
            "queue_order": False,
        },
    }
    result.update(overrides)
    return result


def test_single_card_has_readable_title_and_state_before_reason():
    sample = item()
    before = copy.deepcopy(sample)
    message = points.build_message([sample])
    card = FeishuChannel()._build_card(message)["card"]
    assert sample == before
    assert message.extra["template_version"] == "paper_buy_point_card_v6_execution_snapshot"
    title = card["header"]["title"]["content"]
    assert "B2" in title and "模板测试（600001）" in title
    assert "Claw 策略买点确认 · 1条 · 1" not in title
    assert card["header"]["template"] == "blue"
    assert message.content.index("非下单或成交") < message.content.index("📋 原确认依据")
    assert "信号参考价 ¥10.50" in message.content and "非成交价" in message.content
    assert "昨日首板，弱开后收复零轴" in message.content
    assert "信号确认于09:40:00" in message.content
    assert "买卖盘强弱指标 **0.00**" in message.content
    assert "分时均价(VWAP) **10.40**" in message.content
    assert "🔎 信号追溯" not in message.content and "执行边界" not in message.content
    assert "风险提醒" not in message.content and "获利保证" not in message.content
    assert "T+1" not in message.content
    assert "确认：2026-09-08 09:40:10（北京时间）" in message.content
    assert "行情轮次时点：09:40:00" in message.content
    assert len(card["elements"]) == 1
    assert "发送时间" not in json.dumps(card, ensure_ascii=False)
    assert "下单前核对" in message.content
    assert message.extra["signal_audits"][0]["run_id"] == sample["run_id"]
    assert all(e["tag"] == "markdown" for e in card["elements"])
    assert "紧急" not in card["elements"][-1]["content"]
    json.dumps(card, ensure_ascii=False)


@pytest.mark.parametrize("account,badge", list(zip(ACCOUNT_NAMES, (
    "A", "B", "C", "D", "E", "F", "A2", "B2", "C2", "D2", "E2", "F2",
))))
def test_account_badges_and_roles_are_unambiguous(account, badge):
    message = points.build_message([item(account)])
    role = "次账户" if account.startswith("challenger_") else "主账户"
    assert f"｜{badge} {role}" in message.content
    assert message.extra["display_title"].startswith(f"条件确认·执行未核实｜{badge} · ")


def test_six_item_batch_keeps_reasons_accounts_and_audits_separate():
    samples = [item(account, n) for n, account in enumerate(ACCOUNT_NAMES[6:], 1)]
    message = points.build_message(samples)
    card = FeishuChannel()._build_card(message)["card"]
    assert "6条" in card["header"]["title"]["content"]
    assert len(card["elements"]) == 6  # 只有六个股票块，不再附加固定风险或发送时间
    for n, sample in enumerate(samples):
        stock_block = card["elements"][n]["content"]
        assert sample["payload"]["strategy_label"] in stock_block
        assert "原确认依据" in stock_block
        assert sample["run_id"] not in stock_block  # 编号不抢占第一屏
        assert sample["run_id"] not in message.content
        assert sample["run_id"] == message.extra["signal_audits"][n]["run_id"]
    assert "非下单或成交" in message.content
    assert message.extra["signal_ids"] == [1, 2, 3, 4, 5, 6]


def test_title_cleanup_does_not_change_throttle_identity(monkeypatch):
    monkeypatch.setattr(points.settings, "PUSH_HOURLY_LIMIT", 30)
    first = points.build_message([item(ident=1)])
    second = points.build_message([item(ident=2)])
    assert first.extra["display_title"] == second.extra["display_title"]
    assert first.title != second.title
    throttle = PushThrottle()
    assert throttle.should_send(first)
    throttle.mark_sent(first)
    assert not throttle.should_send(first)
    assert throttle.should_send(second)


def test_unknown_state_never_infers_fill_from_reason_text():
    message = points.build_message([item(reason="策略说明中提及已成交，但没有执行审计")])
    assert "🔎 原条件曾确认 · 执行未核实 · 非下单或成交" in message.content
    assert "✅ 已有模拟成交记录" not in message.content


def test_missing_and_prose_metrics_do_not_invent_prices_or_trade_plans():
    message = points.build_message([item(reason="VWAP上方；量比健康；持续放量")])
    assert "• VWAP上方" in message.content and "• 量比健康" in message.content
    assert "📊 确认时指标" not in message.content
    for text in ("目标价格", "止损价格", "建议仓位", "胜率"):
        assert text not in message.content
    assert "未提供详细说明" in points.build_message([item(reason="")]).content
    with pytest.raises(ValueError):
        points.build_message([])


def test_untrusted_text_cannot_create_feishu_mentions_or_links():
    sample = item(name='<at id=all>所有人</at>', reason="[点击](https://example.invalid)；<at id=all>提示</at>")
    sample["payload"]["account_role"] = "<at id=all>次账户</at>"
    message = points.build_message([sample])
    encoded = json.dumps(FeishuChannel()._build_card(message), ensure_ascii=False)
    assert "<at" not in encoded and "[点击](" not in encoded


@pytest.mark.parametrize("state,label", [
    ("filled", "已有模拟成交记录"), ("pending", "已提交模拟委托"),
    ("waiting", "本轮未下单 · 等待条件"), ("blocked", "本轮执行已拦截"),
    ("expired", "原买点已失效"),
])
def test_footer_removal_keeps_real_execution_state_and_order_specific_note(state, label):
    sample = item(execution_state=state, execution_note="实际审计备注：保留原文，不当固定话术删除")
    sample["payload"]["queue_order"] = True
    message = points.build_message([sample])
    card = FeishuChannel()._build_card(message)["card"]
    assert label in message.content
    assert sample["execution_note"] in message.content
    assert "回封排队提示" in message.content
    assert "风险提醒" not in message.content
    assert len(card["elements"]) == 1
    assert "发送时间" not in json.dumps(card, ensure_ascii=False)
    assert message.extra["signal_audits"][0]["execution_state"] == state


def test_generic_anomaly_card_contract_is_unchanged():
    message = PushMessage(title="异动测试", content="**量价分析**\n原有信息", msg_type="signal",
                          category="anomaly", priority=8,
                          extra={"display_title": "不可串入", "feishu_sections": ["不可串入"]})
    card = FeishuChannel()._build_card(message)["card"]
    assert card["header"]["title"]["content"] == "📡 异动测试"
    assert len(card["elements"]) == 2
    assert card["elements"][0]["content"] == message.content
    assert "🔴 紧急" in card["elements"][1]["content"]


@pytest.mark.parametrize("sections", [None, [], "bad", [None]])
def test_older_or_invalid_paper_card_extras_fall_back_to_content(sections):
    message = PushMessage(title="旧版买点", content="原始正文", category="paper_buy_point",
                          extra={"feishu_sections": sections})
    card = FeishuChannel()._build_card(message)["card"]
    assert card["elements"][0]["content"] == "原始正文"
