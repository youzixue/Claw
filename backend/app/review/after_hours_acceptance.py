"""Pure checklist of ALREADY READ premarket sample evidence, not certification.

No DB/network/clock sampling, rebuilding snapshots, job execution or permissions.
A passing checklist cannot prove scheduled jobs ran, source first publication,
physical commit, all-market coverage or a deployed version. All authority flags
are permanently false. Existing reader output remains intact on missing inputs.
"""
from datetime import date, datetime, time
import json
import math
import re

from app.data.after_hours import SUPPLIER_VERSIONS

VERSION = "premarket_stored_evidence_checklist_v1_20261002"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_CODES = MAX_NEWS = 500


def _date(value):
    if not isinstance(value,str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",value) is None:
        raise ValueError("invalid_date")
    return date.fromisoformat(value)


def _clock(value):
    if not isinstance(value,str) or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,6})?",value) is None:
        raise ValueError("invalid_clock")
    at=datetime.fromisoformat(value)
    if at.tzinfo is not None:
        raise ValueError("invalid_clock")
    return at


def _positive_id(value):
    return type(value) is int and 0<value<=2**63-1


def _hash(value):
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{64}",value) is not None


def _metadata(value, maximum=80):
    return isinstance(value,str) and bool(value.strip()) and "\x00" not in value and len(value)<=maximum


def _observation(ref, *, stage, previous, cutoff, source_class="official"):
    if not isinstance(ref,dict):
        return False
    try:
        source=_clock(ref.get("source_quote_at"))
        received=_clock(ref.get("received_at"))
        recorded=_clock(ref.get("recorded_at"))
        available=_clock(ref.get("available_at"))
        legal=time(15)<=source.time()<time(15,5) if stage=="regular_close" else source.time()>=time(15,30)
        source_ok=ref.get("source")=="tencent_close" if stage=="regular_close" else (
            ref.get("source") in ("sse_fixed_price","szse_fixed_price") if source_class=="official"
            else ref.get("source") in SUPPLIER_VERSIONS
                 and ref.get("source_version")==SUPPLIER_VERSIONS[ref["source"]])
        return bool(_positive_id(ref.get("id")) and ref.get("stage")==stage
            and ref.get("quality_status")=="observed" and ref.get("integrity_verified") is True
            and source_ok and _metadata(ref.get("source_version"),64) and _hash(ref.get("content_hash"))
            and source.date()==received.date()==recorded.date()==available.date()==previous
            and source<=received<=recorded==available<=cutoff and legal
            and (stage!="after_hours" or received.time()>=time(15,30)))
    except (ValueError,TypeError):
        return False


def _stage_pair(item, *, previous, cutoff, source_class="official"):
    refs=item.get("source_refs")
    if not isinstance(refs,list) or len(refs)!=2 or any(not isinstance(ref,dict) for ref in refs):
        return False
    if {ref.get("stage") for ref in refs}!={"regular_close","after_hours"}:
        return False
    ids=[ref.get("id") for ref in refs]
    linked=item.get("observation_ids")
    if (not isinstance(linked,list) or len(linked)!=2 or not all(_positive_id(i) for i in linked)
            or len(set(ids))!=2 or linked!=ids):
        return False
    if not all(_observation(ref,stage=ref["stage"],previous=previous,cutoff=cutoff,
                            source_class=source_class) for ref in refs):
        return False
    def number(key, integer=False, positive=False):
        value=item.get(key)
        return (type(value) is int if integer else type(value) in (int,float)) and (
            0<value<=2**63-1 if positive else 0<=value<=2**63-1) and math.isfinite(value)
    return (item.get("status")=="observed" and item.get("missing")==[]
        and item.get("ratio_basis")==("observed_official_daily_total_after_closed_session"
            if source_class=="official" else "observed_supplier_native_daily_total_after_session")
        and number("close_price_1500",positive=True)
        and number("regular_volume_shares",integer=True,positive=True)
        and number("after_volume_shares",integer=True) and number("after_amount_yuan")
        and number("all_day_volume_shares",integer=True) and number("all_day_amount_yuan")
        and item["all_day_volume_shares"]>=item["regular_volume_shares"]+item["after_volume_shares"]
        and item["all_day_amount_yuan"]>=item["after_amount_yuan"]
        and (item["after_volume_shares"]==0)==(item["after_amount_yuan"]==0))


def _visible_news(news, *, previous, cutoff):
    items=news.get("items")
    if not isinstance(items,list) or len(items)>MAX_NEWS or news.get("status")!="available":
        return False
    if _clock(news.get("as_of_at"))!=cutoff or _clock(news.get("window_start"))!=datetime.combine(previous,time(15)):
        return False
    if not items:
        return (news.get("count")==0 and type(news.get("count")) is int
            and news.get("coverage",{}).get("known_zero_news") is True
            and news.get("coverage",{}).get("count_complete") is True)
    if type(news.get("count")) is not int or news["count"]<len(items):
        return False
    for item in items:
        if (not isinstance(item,dict) or item.get("origin")!="observed"
                or not _positive_id(item.get("content_version_id")) or not _hash(item.get("content_hash"))):
            return False
        clocks=[_clock(item.get(key)) for key in ("publish_time","first_received_at","received_at",
            "recorded_at","content_available_at","entity_verified_at","available_at")]
        publish,first,received,recorded,content,entity,available=clocks
        if (not datetime.combine(previous,time(15))<=publish<=cutoff or any(at>cutoff for at in clocks)
                or not first<=received<=recorded<=content or received>entity
                or available<max(content,entity)):
            return False
        if item.get("analysis_version_id") is not None:
            completed=_clock(item.get("analysis_completed_at"))
            analysis_available=_clock(item.get("analysis_available_at"))
            if (not _positive_id(item["analysis_version_id"]) or not _hash(item.get("analysis_result_hash"))
                    or not content<=completed<=analysis_available<=available<=cutoff):
                return False
    return True


def summarize_stored_acceptance(context):
    answer={"protocol_version":VERSION, "status":"unavailable", "checks":{}, "missing_checks":[],
        "sample_code_count":0, "sample_stage_pair_count":0, "sample_supplier_pair_count":0,
        "sample_observational_pair_count":0, "recorded_sample_checks_passed":False,
        "natural_acceptance_verified":False, "scheduled_job_execution_certified":False,
        "recheck_run_receipt_certified":False, "first_source_availability_certified":False,
        "individual_input_availability_certified":False, "historical_pit_certified":False,
        "complete_market_coverage":False, "all_news_coverage_certified":False,
        "runtime_loaded_version":None, "automatic_weight_update":False, "execution_authorized":False,
        "input_budget_scope":"serialized_already_returned_context_not_sql_or_heap_budget",
        "scheduled_slots_not_proved":["15:01_job","15:35_job","16:05_fundamental_capture",
            "20:10_recheck","20:35_freeze","08:00_report_delivery"],
        "claim_boundary":"bounded_returned_sample_structure_and_clocks_not_source_or_job_authentication"}
    try:
        if not isinstance(context,dict) or len(json.dumps(context,ensure_ascii=False,
                separators=(",",":"),allow_nan=False).encode())>MAX_INPUT_BYTES:
            raise ValueError("context_budget_or_shape")
        cutoff=_clock(context.get("as_of_at"))
        target=_date(context.get("trade_date"))
        calendar=context.get("calendar",{})
        previous=_date(calendar.get("expected_previous"))
        if previous>=target:
            raise ValueError("previous_date_not_before_target")
        checks=answer["checks"]
        checks["at_0800_target_cutoff"]=(cutoff.date()==target and cutoff.time()==time(8)
            and context.get("timezone")=="Asia/Shanghai" and context.get("clock_convention")=="naive")
        checks["stored_calendar_aligned"]=(calendar.get("target_status")=="trade_day"
            and calendar.get("coverage",{}).get("complete") is True
            and all(context.get("freshness",{}).get("alignment",{}).get(key)=="matched"
                    for key in ("kline","trusted_kline_anchor","postmarket")))
        post=context.get("previous_postmarket",{})
        frozen_at,created=_clock(post.get("as_of_at")),_clock(post.get("created_at"))
        checks["previous_evening_snapshot_visible"]=(post.get("status")=="available"
            and post.get("review_date")==post.get("analysis_trade_date")==previous.isoformat()
            and frozen_at.date()==created.date()==previous and frozen_at.time()>=time(20,35)
            and frozen_at<=created<=cutoff and _positive_id(post.get("snapshot_id"))
            and _hash(post.get("payload_hash"))
            and all(_metadata(post.get(key)) for key in ("data_version","schema_version","quality_status")))
        fusion=context.get("research_fusion",{})
        dimensions=fusion.get("statistics",{}).get("dimensions",{})
        checks["frozen_three_dimensions_present"]=all(
            dimensions.get(name,{}).get("status") in ("available","partial")
            and _positive_id(dimensions.get(name,{}).get("snapshot_id"))
            and dimensions.get(name,{}).get("snapshot_id")==post.get("snapshot_id")
            and dimensions.get(name,{}).get("payload_hash")==post.get("payload_hash")
            for name in ("fundamental","technical","capital"))
        checks["no_mutable_previous_recomputation"]=(fusion.get("recomputed_previous_day_from_current_spot") is False
            and fusion.get("automatic_weight_update") is False and fusion.get("execution_authorized") is False)
        after=context.get("after_hours",{})
        items=after.get("items")
        if not isinstance(items,list) or len(items)>MAX_CODES:
            raise ValueError("sample_code_budget_or_shape")
        answer["sample_code_count"]=len(items)
        code_ids=set()
        observation_ids=set()
        for item in items:
            if not isinstance(item,dict) or not isinstance(item.get("code"),str) or re.fullmatch(
                    r"[0-9]{6}",item["code"]) is None or item["code"] in code_ids:
                raise ValueError("sample_code_identity")
            code_ids.add(item["code"])
            refs=item.get("source_refs")
            if not isinstance(refs,list) or len(refs)>2:
                raise ValueError("sample_stage_budget_or_shape")
            official_pair = _stage_pair(item,previous=previous,cutoff=cutoff)
            supplier_pair = _stage_pair(item,previous=previous,cutoff=cutoff,source_class="supplier")
            if official_pair or supplier_pair:
                ids=set(item["observation_ids"])
                if observation_ids & ids:
                    raise ValueError("sample_observation_identity_reused")
                observation_ids.update(ids)
                answer["sample_observational_pair_count"]+=1
                answer["sample_stage_pair_count"]+=int(official_pair)
                answer["sample_supplier_pair_count"]+=int(supplier_pair)
        checks["same_day_baseline_and_official_closed_pairs"]=(bool(items)
            and after.get("trade_date")==previous.isoformat() and after.get("as_of_at")==cutoff.isoformat()
            and answer["sample_stage_pair_count"]==len(items))
        checks["same_day_baseline_and_closed_observational_pairs"]=(bool(items)
            and after.get("trade_date")==previous.isoformat() and after.get("as_of_at")==cutoff.isoformat()
            and answer["sample_observational_pair_count"]==len(items))
        answer["source_class_checks"]={
            "all_returned_pairs_official": checks["same_day_baseline_and_official_closed_pairs"],
            "all_returned_pairs_supplier": bool(items) and answer["sample_supplier_pair_count"]==len(items)}
        checks["returned_sample_not_truncated"]=(after.get("truncated") is False
            and type(after.get("returned_codes")) is int and after.get("returned_codes")==len(items)
            and type(after.get("stored_code_count")) is int and after.get("stored_code_count")==len(items))
        checks["selected_news_clocks_visible_or_known_zero"]=_visible_news(
            context.get("news",{}),previous=previous,cutoff=cutoff)
        # Official provenance remains a separate false flag for suppliers. Valid
        # supplier observations may open operator review, never certification.
        answer["missing_checks"]=[key for key,value in checks.items() if not value
            and key!="same_day_baseline_and_official_closed_pairs"]
        answer["recorded_sample_checks_passed"]=not answer["missing_checks"]
        answer["status"]="ready_for_operator_review" if answer["recorded_sample_checks_passed"] else "partial"
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        # Fail only this diagnostic; never rewrite the source components or facts.
        answer["status"]="unavailable"
        answer["recorded_sample_checks_passed"]=False
        answer["missing_checks"]=["bounded_sample_inputs_missing_or_invalid"]
    return answer
