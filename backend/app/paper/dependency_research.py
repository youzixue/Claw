"""A/B dependency diagnostic ablation on existing frozen paper log projections.

No selection, ranking, risk, execution, live settings, metrics reconstruction or
DB calls. Supplemental evidence must already be frozen and precisely log-bound.
"""
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from types import SimpleNamespace
import hashlib
import json
import math
import re

from app.paper.confirmation_evidence import confirmation_evidence
from app.paper.signal_research import signal_record
from app.data.fund_flow_clock import fund_clock_status, main_fund_values_valid
from app.data.auction_evidence import auction_context_complete

DEPENDENCIES = ("funds", "sentiment", "auction")
LAYERS = ("historical_quote_path_confirmed", "current_setup_valid")
LOG_FIELDS = ("id", "account_id", "account_name", "strategy_version", "run_id", "trade_date",
    "quote_round_id", "as_of_at", "created_at", "code", "action", "decision", "source",
    "price", "candidate_score", "reason_code", "stage_code", "executed_trade_id")
PAYLOAD_FIELDS = ("account_name", "signal_key", "decision_run_id", "signal_observed_at",
    "notification_schema", "research_capture_schema", "signal_labels", "market_context",
    "confirmation_evidence", "deferred_order_observation", "confirmation_version", "confirmation_sample_at",
    "confirmation_sample_count", "confirmation_persistence_sec", "prediction_run_key",
    "prediction_run_id", "snapshot_context", "trade_gate_passed", "snapshot_actionable",
    "execution_confirmation", "actionable", "watch_only", "probability", "total_score",
    "run_key", "blocking_datasets", "component_watermarks", "main_net_inflow",
    "main_net_inflow_pct", "fund_data_degraded", "source", "as_of",
    "auction_feed_complete", "auction_evidence_status", "auction_evidence_contract")
ATTACHMENT_FIELDS = ("log_id", "account_id", "account_name", "strategy_version", "decision_run_id",
    "quote_round_id", "code", "evidence_ref", "source", "source_version", "trade_date",
    "source_quote_at", "received_at", "observed_at", "main_net_inflow", "main_net_inflow_pct",
    "quality_status", "score", "phase", "auction_feed_complete", "auction_evidence_status",
    "auction_evidence_contract")


def _owned(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if type(value) is dict:
        return {str(k): _owned(v) for k, v in value.items()}
    if type(value) in (list, tuple):
        return [_owned(v) for v in value]
    if type(value) is float and not math.isfinite(value):
        return {"invalid_number": "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"}
    if value is None or type(value) in (str, int, float, bool):
        return value
    return {"invalid_type": type(value).__name__}


def _hash(value):
    return hashlib.sha256(json.dumps(_owned(value), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _projection(row, fields):
    return {k:_owned(row[k]) for k in fields if k in row} if type(row) is dict else _owned(row)


def _object(value):
    if type(value) is dict:
        return value
    if type(value) is str:
        try:
            result = json.loads(value)
            return result if type(result) is dict else {}
        except (TypeError, ValueError):
            pass
    return {}


def _ref(value):
    return type(value) is str and bool(value.strip())


def _clock(value):
    if type(value) is str:
        if len(value) < 19:
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def _date(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _number(value):
    if type(value) not in (int,float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _and(states):
    return "false" if "false" in states else "unknown" if "unknown" in states else "true"


@dataclass(frozen=True)
class DependencyScenario:
    version: str
    required_dependencies: tuple
    required_confirmation_layers: tuple = ("historical_quote_path_confirmed", "current_setup_valid")
    mask_unavailable: tuple = ()
    max_evidence_age_sec: int = 900

    def __post_init__(self):
        if not _ref(self.version) or not self.version.startswith("research:"):
            raise ValueError("independent research: scenario required")
        for field, allowed in (("required_dependencies", DEPENDENCIES),
                               ("mask_unavailable", DEPENDENCIES),
                               ("required_confirmation_layers", LAYERS)):
            value = getattr(self,field)
            if type(value) is not tuple or any(type(v) is not str or v not in allowed for v in value) or len(set(value)) != len(value):
                raise ValueError("explicit unique supported condition tuple required")
        if type(self.max_evidence_age_sec) is not int or self.max_evidence_age_sec <= 0:
            raise ValueError("positive explicit evidence age required")


def _batch_error(batch, as_of):
    if type(batch) is not dict:
        return "batch_not_owned_object"
    if (type(batch.get("account_id")) is not int or batch["account_id"] <= 0
            or batch.get("account_name") not in ("default","promotion")
            or not all(_ref(batch.get(k)) for k in ("batch_id","account_ref","strategy_version",
                "decision_run_id","quote_round_id","universe_ref"))):
        return "batch_identity_unproven"
    cutoff = _clock(batch.get("frozen_at"))
    if cutoff is None or cutoff > as_of:
        return "batch_cutoff_invalid_or_future"
    ids = batch.get("expected_log_ids")
    if type(ids) is not list or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        return "batch_denominator_invalid"
    return ""


def _deferred_buy_evidence_error(record, payload, batch, raw, created, cutoff, day):
    """Recognize only the new owned response contract, not a generic late clock."""
    observed = payload.get("deferred_order_observation")
    if (type(observed) is not dict
            or observed.get("schema_version") != "confirmation:deferred_buy_v1"
            or observed.get("original_decision_scope") != "frozen_order_candidate_not_current_setup"
            or observed.get("current_setup_basis") != "not_rechecked_by_deferred_reconciliation"
            or observed.get("replay_ready") is not False
            or observed.get("commit_known_at") is not None):
        return "deferred_observation_contract_unproven"
    response_at = _clock(observed.get("response_observed_at"))
    if (response_at is None or response_at.date() != day
            or not created <= response_at <= cutoff
            or _clock(observed.get("strategy_evaluated_at")) != created
            or _clock(raw.get("observed_at")) != response_at):
        return "deferred_response_clock_invalid_or_not_visible"
    order = observed.get("order_response")
    prior = observed.get("original_decision_confirmation")
    if type(order) is not dict or type(prior) is not dict:
        return "deferred_order_or_original_evidence_missing"
    version = order.get("strategy_version")
    if (version is not None and type(version) is not str
            or (version or "legacy_unversioned") != record["strategy_version"]
            or order.get("account_id") != batch["account_name"]
            or order.get("code") != record["code"] or order.get("side") != "buy"
            or not _ref(order.get("order_id")) or not _ref(order.get("decision_round_id"))
            or order["decision_round_id"] == record["quote_round_id"]):
        return "deferred_order_identity_conflict"
    decision_at, order_day = _clock(order.get("decision_at")), _date(order.get("trade_date"))
    event, status = observed.get("outcome_event"), order.get("status")
    if (decision_at is None or order_day is None or decision_at.date() != order_day
            or order_day > day or decision_at > created
            or (order_day < day) != (event == "canceled")):
        return "deferred_original_order_clock_unproven"
    quantity = observed.get("reported_filled_quantity_this_round")
    matched = observed.get("reported_fill_order_identity_matches")
    if event in ("filled", "partial"):
        shape = ("buy", "executed", "fill")
        permission, basis = "true", "reported_execution_this_round_not_reusable_permission"
        valid_result = (status == event and type(quantity) is int and quantity >= 100
            and quantity % 100 == 0 and matched is True
            and order.get("last_fill_round_id") == record["quote_round_id"])
    elif event == "waiting":
        shape = ("deferred_buy", "wait", "execution")
        permission, basis = "unknown", "not_proven_this_round"
        valid_result = status in ("submitted", "partial") and type(quantity) is int and quantity == 0 and matched is None
    elif event in ("risk_blocked", "rejected", "canceled"):
        shape = ("skip_buy", "skipped" if event == "canceled" else "blocked", "execution")
        permission, basis = "false", "reported_block_or_terminal_response_this_round"
        valid_result = status == event and type(quantity) is int and quantity == 0 and matched is None
    else:
        return "deferred_response_event_unproven"
    evidence = payload.get("confirmation_evidence")
    if (not valid_result or type(evidence) is not dict
            or tuple(record.get(k) for k in ("action", "decision", "stage_code")) != shape
            or record.get("reason_code") != "deferred_" + event
            or raw["order_result"] != status or evidence.get("execution_permitted") != permission
            or evidence.get("current_setup_valid") != "unknown"
            or observed.get("execution_permission_basis") != basis):
        return "deferred_response_and_log_conflict"
    previous = confirmation_evidence({"confirmation_evidence": prior})
    if any(raw[k] != previous[k] for k in (
            "historical_quote_path_confirmed", "historical_confirmed_at", "historical_log_id")):
        return "deferred_original_history_conflict"
    if raw["historical_quote_path_confirmed"] != "unknown":
        prior_at = _clock(previous.get("observed_at"))
        if prior_at is None or prior_at.date() != order_day or prior_at > decision_at:
            return "deferred_original_history_clock_unproven"
        if raw["historical_quote_path_confirmed"] == "true":
            historical = _clock(previous.get("historical_confirmed_at"))
            if (historical is None or historical.date() != order_day or historical > prior_at
                    or type(previous.get("historical_log_id")) is not int or previous["historical_log_id"] <= 0):
                return "deferred_original_history_clock_unproven"
    return ""


def adapt_paper_record(record, batch):
    """Consume a PaperAutoTradeLog owned projection, not an ORM/live object."""
    if type(record) is not dict:
        return {"status":"unknown","reason":"log_not_owned_object"}
    payload = _object(record.get("candidate_json"))
    source, created, cutoff = _clock(record.get("as_of_at")), _clock(record.get("created_at")), _clock(batch["frozen_at"])
    day = _date(record.get("trade_date"))
    run_id = payload.get("decision_run_id") if record.get("action") == "buy_signal" else record.get("run_id")
    identity = (type(record.get("id")) is int and record["id"] > 0
        and _ref(record.get("source")) and _ref(record.get("action")) and _ref(record.get("decision"))
        and type(record.get("account_id")) is int and record["account_id"] == batch["account_id"]
        and record.get("strategy_version") == batch["strategy_version"]
        and record.get("quote_round_id") == batch["quote_round_id"]
        and run_id == batch["decision_run_id"]
        and record.get("account_name",batch["account_name"]) == batch["account_name"]
        and payload.get("account_name",batch["account_name"]) == batch["account_name"]
        and _ref(record.get("code")) and re.fullmatch("[0-9]{6}",record["code"]))
    if not identity:
        return {"status":"unknown","reason":"log_instance_version_batch_conflict"}
    if source is None or created is None or day is None or not source <= created <= cutoff or source.date() != day or created.date() != day or cutoff.date() != day:
        return {"status":"unknown","reason":"log_clock_invalid_or_not_visible"}
    raw = confirmation_evidence(payload)
    evidence_at = _clock(raw.get("observed_at"))
    deferred_response = "deferred_order_observation" in payload
    if deferred_response:
        error = _deferred_buy_evidence_error(record, payload, batch, raw, created, cutoff, day)
        if error:
            return {"status":"unknown", "reason":error}
        evidence_valid = True
    else:
        # Legacy evidence cannot acquire the new physical-observation exception.
        evidence_valid = evidence_at is not None and source <= evidence_at <= created and evidence_at.date() == day
    layers = {k:raw[k] if evidence_valid else "unknown" for k in (*LAYERS,"execution_permitted")}
    if layers["historical_quote_path_confirmed"] == "true":
        historical = _clock(raw.get("historical_confirmed_at"))
        if historical is None or historical.date() != day or historical > evidence_at or type(raw.get("historical_log_id")) is not int or raw["historical_log_id"] <= 0:
            layers["historical_quote_path_confirmed"] = "unknown"
    signal = None
    if record.get("action") == "buy_signal":
        # Existing exact v2/v1 clock contract, with strict numeric price prevalidation.
        if _number(record.get("price")) is not None:
            signal = signal_record(SimpleNamespace(**{**{k:None for k in LOG_FIELDS}, **record, "trade_date":day,
                "candidate_json":json.dumps(_owned(payload),ensure_ascii=False)}), batch["account_name"])
    decision_at = (created if deferred_response else
        _clock(signal.get("observed_at")) if signal and signal["evidence_status"] == "valid"
        else evidence_at if evidence_valid else created)
    # Later response visibility must NOT extend the decision cutoff for funds,
    # sentiment or auction attachments. Observation time is a separate output.
    response_clock = ({"response_available_at":evidence_at,
        "decision_clock_basis":"original_round_parameter_not_later_response_time",
        "response_contract":"confirmation:deferred_buy_v1_owned_projection_not_archive_authentication"}
        if deferred_response else {})
    market = payload.get("market_context") if type(payload.get("market_context")) is dict else {}
    return {"status":"adapted", "reason":"", "decision_at":decision_at, **response_clock,
        "confirmation":layers, "signal":signal,
        "production_facts":{"log_action":record.get("action"),"log_decision":record.get("decision"),
            "risk":_projection(_object(record.get("risk_json")),("final_level","decisions","block_reasons","warnings")),
            "original_signal_confirmed":"true" if signal and signal["evidence_status"] == "valid"
                and record.get("decision") == "confirmed" else "unknown",
            "confirmation":layers, "order_result":raw["order_result"] if evidence_valid else "unknown",
            "executed_trade_id":record.get("executed_trade_id"),
            "ranking":{k:payload.get(k) for k in ("probability","total_score","snapshot_actionable","watch_only")},
            "candidate_score":record.get("candidate_score"),"reason_code":record.get("reason_code"),
            "blocking_datasets":payload.get("blocking_datasets"),
            "prediction_run_key":payload.get("prediction_run_key") or payload.get("run_key")},
        "observed_payload":{"fund_values":{k:payload.get(k) for k in ("main_net_inflow","main_net_inflow_pct","fund_data_degraded")},
            "auction_context_stamped":auction_context_complete(payload),
            "component_watermarks":payload.get("component_watermarks"),
            "confirmation_sample_count":payload.get("confirmation_sample_count",market.get("confirmation_sample_count")),
            "confirmation_persistence_sec":payload.get("confirmation_persistence_sec",market.get("confirmation_persistence_sec"))}}


def _dependency(name, attachment, record, batch, decision, max_age):
    result = {"availability":"unknown","reason":"frozen_source_evidence_missing","values":None}
    if type(attachment) is not dict:
        return result
    result["evidence"] = _projection(attachment,ATTACHMENT_FIELDS)
    if (type(attachment.get("log_id")) is not int or attachment["log_id"] != record["id"]
            or type(attachment.get("account_id")) is not int or attachment["account_id"] != batch["account_id"]
            or any(attachment.get(k) != batch[k] for k in ("account_name","strategy_version","decision_run_id","quote_round_id"))
            or attachment.get("code") != record["code"] or not _ref(attachment.get("evidence_ref"))):
        return {**result,"reason":"attachment_identity_unproven"}
    day = _date(record["trade_date"])
    if _date(attachment.get("trade_date")) != day or not _ref(attachment.get("source")) or not _ref(attachment.get("source_version")):
        return {**result,"reason":"attachment_source_or_day_unproven"}
    if name == "auction":
        # Reuse stamped DERIVED context test only; do not call live-setting raw gate.
        # Even a valid stamp is not independent proof of raw auction provenance.
        return {**result,"reason":"derived_stamp_not_independent_auction_material",
                "context_stamped":auction_context_complete(attachment)}
    status = fund_clock_status(attachment.get("source_quote_at"),attachment.get("received_at"),
        attachment.get("observed_at"),day,decision,max_age)
    if status != "ok":
        return {**result,"reason":"source_clock_"+status}
    if name == "funds":
        values = [attachment.get(k) for k in ("main_net_inflow","main_net_inflow_pct")]
        if any(_number(v) is None for v in values) or not main_fund_values_valid(*values):
            return {**result,"reason":"fund_values_missing_or_invalid"}
        if attachment["source"] not in ("eastmoney_main_fund","fund_flow"):
            return {**result,"reason":"fund_source_unverified"}
        return {**result,"availability":"true","reason":"known_values_not_positive_signal",
                "values":{"main_net_inflow":values[0],"main_net_inflow_pct":values[1]}}
    if attachment.get("quality_status") != "ok" or _number(attachment.get("score")) is None or not _ref(attachment.get("phase")):
        return {**result,"reason":"sentiment_quality_unproven"}
    return {**result,"availability":"true","reason":"frozen_quality_not_risk_permission",
            "values":{"score":attachment["score"],"phase":attachment["phase"]}}


def build_dependency_research_report(records, batches, *, as_of, scenarios):
    """Pair EVERY declared log slot under identical account/version/batch/cutoff."""
    as_of = _clock(as_of)
    if as_of is None:
        raise ValueError("explicit naive local as_of required")
    scenarios = tuple(scenarios)
    if not scenarios or any(not isinstance(s,DependencyScenario) for s in scenarios) or len({s.version for s in scenarios}) != len(scenarios):
        raise ValueError("unique explicit research scenarios required")
    records, batches = list(records), list(batches)
    by_id = {}
    for row in records:
        if type(row) is dict and type(row.get("id")) is int:
            by_id.setdefault(row["id"],[]).append(row)
    batch_ids = Counter(b.get("batch_id") for b in batches if type(b) is dict and type(b.get("batch_id")) is str)
    output = []
    used = set()
    for batch in batches:
        error = _batch_error(batch,as_of)
        if not error and batch_ids[batch["batch_id"]] != 1:
            error = "duplicate_batch_identity"
        item = {"batch":_owned(batch),"status":"unknown" if error else "paired", "reason":error,"cases":[]}
        if not error:
            for log_id in batch["expected_log_ids"]:
                used.add(log_id)
                matches = by_id.get(log_id,[])
                adapted = adapt_paper_record(matches[0],batch) if len(matches)==1 else {"status":"unknown","reason":"log_missing_or_duplicate"}
                case = {"log_id":log_id,"adaptation":adapted,"scenarios":[],
                    "frozen_logs":[{"log":_projection(r,LOG_FIELDS),
                        "payload":_projection(_object(r.get("candidate_json")),PAYLOAD_FIELDS),
                        "risk":_projection(_object(r.get("risk_json")),("final_level","decisions","block_reasons","warnings"))}
                        for r in matches]}
                for scenario in scenarios:
                    dependencies = {k:{"availability":"unknown","reason":"log_unproven"} for k in DEPENDENCIES}
                    confirmation = {k:"unknown" for k in LAYERS}
                    if adapted["status"] == "adapted":
                        record = matches[0]
                        attachments = _object(record.get("dependency_evidence"))
                        dependencies = {k:_dependency(k,attachments.get(k),record,batch,adapted["decision_at"],
                            scenario.max_evidence_age_sec) for k in DEPENDENCIES}
                        confirmation = adapted["confirmation"]
                    selected = ["unknown" if k in scenario.mask_unavailable else dependencies[k]["availability"]
                                for k in scenario.required_dependencies]
                    selected += [confirmation[k] for k in scenario.required_confirmation_layers]
                    gate = _and(selected) if selected and adapted["status"] == "adapted" else "unknown"
                    case["scenarios"].append({"scenario":{**asdict(scenario),"scenario_hash":_hash(asdict(scenario))},
                        "dependency_facts":dependencies,"hypothesis_condition":gate,
                        "masked_as_unavailable":list(scenario.mask_unavailable),
                        "omitted_requirements":[k for k in DEPENDENCIES if k not in scenario.required_dependencies],
                        "execution_permission":None,"fillable_return":None,
                        "interpretation":"diagnostic_requirements_only_not_original_strategy_or_execution"})
                item["cases"].append(case)
            item["paired_log_slots"] = len(batch["expected_log_ids"])
            item["declared_candidate_codes"] = sorted({by_id[i][0]["code"] for i in batch["expected_log_ids"]
                if len(by_id.get(i,[]))==1 and _ref(by_id[i][0].get("code"))})
        output.append(item)
    report = _owned({"schema_version":"ab_dependency_research_v2","as_of":as_of,"read_only":True,
        "batches":output,"input_log_count":len(records),
        "unassigned_log_count":sum(not (type(r) is dict and type(r.get("id")) is int and r["id"] in used) for r in records),
        "input_evidence_hash":_hash([{"log":_projection(r,LOG_FIELDS),
            "raw_candidate_json_hash":_hash(r.get("candidate_json")) if type(r) is dict else None,
            "raw_risk_json_hash":_hash(r.get("risk_json")) if type(r) is dict else None,
            "payload":_projection(_object(r.get("candidate_json")),PAYLOAD_FIELDS) if type(r) is dict else None,
            "risk":_owned(_object(r.get("risk_json"))) if type(r) is dict else None,
            "supplemental":{k:_projection(_object(r.get("dependency_evidence")).get(k),ATTACHMENT_FIELDS) for k in DEPENDENCIES}
                if type(r) is dict else None} for r in records]),
        "limitations":["frozen_input_log_denominator_not_all_market_candidates",
            "no_raw_archive_authentication_or_historical_backfill","auction_material_not_implemented_stays_unknown",
            "availability_not_positive_funds_or_buyability","A_and_B_not_identical_dependency_boolean_formulas",
            "B2_price_path_not_B_counterfactual","no_production_ranking_risk_order_or_position_changes"]})
    report["data_hash"] = _hash(report)
    return report
