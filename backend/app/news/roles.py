"""Conservative original-text attribution; no model, database, aliases or scores.

Entity presence is not economic benefit. These guards only veto unsupported
positive readings; unmatched wording is not a certification of a transaction.
"""
import re

NEWS_ROLE_PROTOCOL = "news_roles_v1"

# Sentence-wide scope deliberately errs on the side of research-only. In a
# mixed article we cannot safely attach a model's positive direction to a party.
_RULES = (
    ("risk_disclosure", r"风险提示|异常波动|终止收购|终止重组|终止重大资产重组"),
    ("denial_or_unconfirmed", r"否认|辟谣|不属实|不实信息|不实传闻|未(?:曾|有|取得|获得|通过|完成|开展|涉及|签订|签署|收到)|尚未|并未|不存在|不涉及|没有(?:取得|获得|通过|相关|订单)|不具备|无(?:产品供货|相关订单|相关业务|相关认证)|尚处于商业化起步阶段"),
    ("limited_business_exposure", r"(?:收入|营收|营业收入|业务)[^。；\n]{0,40}(?:不超过|不足|低于|小于|不到|仅占|仅为|仅有|仅[0-9.]+%|占比低|占比较小|<=|≤)|(?:占比|占)[^。；\n]{0,12}(?:不超过|不足|低于|小于|不到|<=|≤)|尚不构成|影响较小|未形成收入"),
    ("equity_transfer_roles", r"(?:转让|受让|出售|出让)[^。；\n]{0,80}(?:股份|股权)|(?:股份|股权)[^。；\n]{0,30}(?:转让|受让|出售|出让)"),
    ("affiliate_not_issuer", r"(?:董事长|实控人|实际控制人|控股股东|关联方|关联主体)[^。；\n]{0,100}(?:重整|收购|投资|受让)|为[^。；\n]{0,60}(?:董事长|实控人|实际控制人|控股股东)[^。；\n]{0,40}关联主体"),
    ("proposed_not_completed", r"(?:拟|计划|意向|筹划)[^。；\n]{0,50}(?:重整|收购|购买|签订|签署|出资|入主)|(?:重整|收购)[^。；\n]{0,35}(?:意向|不确定)"),
)
_PRICE_REPORT = re.compile(r"涨超|涨逾|涨幅|领涨|涨停|拉升|冲高|走强|表现活跃|上涨|大涨|连板|封板|[0-9]+天[0-9]+板")


def assess_news_roles(title, content, entities=()):
    """Return owned evidence with verbatim excerpts; never infer a fuzzy code."""
    title, content = str(title or ""), str(content or "")
    text = title + "\n" + content
    reasons, excerpts = [], []
    for reason, pattern in _RULES:
        match = re.search(pattern, text)
        if match:
            reasons.append(reason)
            excerpts.append({"reason": reason, "text": match.group(0),
                             "start": match.start(), "end": match.end()})
    match = _PRICE_REPORT.search(title)
    if match:
        reasons.append("price_action_retrospective")
        excerpts.append({"reason": reasons[-1], "text": match.group(0),
                         "start": match.start(), "end": match.end()})

    verified = []
    for entity in entities or ():
        if not isinstance(entity, dict):
            continue
        name, code = entity.get("name"), entity.get("code")
        if (not isinstance(name, str) or len(name) < 3 or name not in text
                or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                or entity.get("method") != "exact_unique_stock_name_v1"):
            continue
        if any(e["code"] == code for e in verified):
            continue
        start = text.index(name)
        role = "mentioned_entity"
        # Only a local title grammar establishes seller vs target; never copy
        # the same bullish direction to both participants.
        transfer = re.search(r"转让|出售|出让", title)
        if transfer and start < len(title):
            if start < transfer.start() and not title[start + len(name):transfer.start()].strip("：: 拟将"):
                role = "transaction_subject"
            elif (transfer.end() <= start
                  and not title[transfer.end():start].strip("其持有的所")
                  and re.match(r"(?:的)?[\d.\s%％]*(?:股份|股权)", title[start + len(name):])):
                role = "transaction_target"
        if "affiliate_not_issuer" in reasons and role == "mentioned_entity":
            role = "affiliate_context_not_direct_acquirer"
        verified.append({**entity, "role": role, "direction": "unconfirmed",
                         "source_field": "title" if start < len(title) else "content",
                         "mention": name, "start": start, "end": start + len(name)})
    if len(verified) > 1:
        reasons.append("multiple_entities_direction_unresolved")
    return {
        "protocol": NEWS_ROLE_PROTOCOL,
        "scope": "research_only" if reasons else "legacy_eligibility_unmodified",
        "block_positive_catalyst": bool(reasons),
        "reasons": reasons, "evidence": excerpts, "entities": verified,
        "candidate_codes": sorted({e["code"] for e in verified}),
        # This deterministic layer does not certify economic benefit, even for
        # a single-issuer announcement. Legacy eligibility is not confirmation.
        "positive_beneficiary_codes": [],
    }


def guard_news_analysis(title, content, result, entities=()):
    """Preserve hypotheses separately; blocked prose cannot masquerade as fact."""
    guarded = dict(result)
    attribution = assess_news_roles(title, content, entities)
    guarded["role_evidence"] = attribution
    codes = {code for code in result.get("related_codes", [])
             if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code)}
    guarded["related_codes"] = sorted(codes | set(attribution["candidate_codes"]))
    guarded["positive_beneficiary_codes"] = []
    if attribution["block_positive_catalyst"]:
        guarded["research_only"] = True
        guarded["summary"] = (str(title or "") + "\n" + str(content or "")).strip()[:500]
        guarded["key_points"] = [e["text"] for e in attribution["evidence"]]
        guarded["model_hypotheses"] = result.get("model_hypotheses", {
            "summary": result.get("summary"), "events": result.get("events", []),
            "sentiment": result.get("sentiment"),
        })
        guarded["events"] = [{
            "type": "research_context", "title": str(title or ""),
            "summary": guarded["summary"], "research_only": True,
            "verification_status": "unconfirmed", "related_codes": guarded["related_codes"],
            "related_sectors": [], "role_evidence": attribution,
        }]
        if guarded.get("sentiment") in {"bullish", "positive"}:
            guarded["sentiment"] = "neutral"
        # No broad sector direction can bypass the direct-stock semantic veto.
        guarded["related_sectors"] = []
    return guarded
