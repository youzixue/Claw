"""Presentation scope only; no transport, DB or trading calls."""
import copy
import pytest
from test_paper_buy_point_template import item
from app.push import paper_buy_points as points

def portfolio(ident=2, origin="challenger_f2"):
    sample=item("shared_50k", ident, reason="原策略已提交共享组合确认；资金分配与下一轮模拟撮合另行审核，非成交",
        execution_state="blocked", execution_note="共享分配：max_symbol_ratio")
    sample["payload"].update(portfolio_notification_schema="portfolio_buy_point_ingress_v1",
        origin_account=origin, account_role="共享5万元组合", strategy_label="共享组合 · 来源 "+origin)
    return sample

def test_portfolio_scope_is_distinct_and_original_evidence_unchanged():
    sample=portfolio();before=copy.deepcopy(sample)
    message=points.build_message([sample])
    assert sample==before
    assert "组合实验 独立账本" in message.content
    assert "来源策略：F2；不合并原账户资金或成交" in message.content
    assert "本轮执行已拦截" in message.content and "max_symbol_ratio" in message.content
    assert "共享" not in message.content and "5万" not in message.content
    assert "原委托参考价" in message.content and "非成交价" in message.content
    assert message.extra["signal_audits"][0]["account"]=="shared_50k"
    assert message.extra["signal_audits"][0]["execution_note"]==sample["execution_note"]
    assert message.extra["template_version"]=="paper_buy_point_card_v5_portfolio_scope"
    assert message.extra["display_title"].startswith("组合实验通知｜")
    assert message.title=="Claw 策略买点确认 · 1条 · 2"

def test_mixed_card_does_not_merge_source_account_and_portfolio():
    source=item("default",1);combined=points.build_message([source,portfolio()])
    source_alone=points.build_message([source])
    assert combined.extra["feishu_sections"][0]==source_alone.extra["feishu_sections"][0]
    assert combined.extra["display_title"].startswith("策略与组合通知｜")
    assert combined.extra["signal_ids"]==[1,2]
    assert [r["account"] for r in combined.extra["signal_audits"]]==["default","shared_50k"]

@pytest.mark.parametrize("origin",["",None,"unknown","<at id=all>"])
def test_unknown_origin_never_impersonates_primary(origin):
    sample=portfolio(origin="");sample["payload"]["origin_account"]=origin
    message=points.build_message([sample])
    assert "来源策略：未知账户" in message.content and "<at" not in message.content

def test_nonportfolio_text_unchanged_even_if_name_contains_shared():
    sample=item("default", name="共享测试", reason="保留原说明共享")
    message=points.build_message([sample])
    assert "共享测试" in message.content and "保留原说明共享" in message.content
    assert "组合实验" not in message.content
    assert message.extra["template_version"]==points.TEMPLATE_VERSION
