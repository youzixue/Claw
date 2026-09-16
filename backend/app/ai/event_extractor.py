"""事件提取 — AI 增强 + 正则降级

从新闻中提取结构化事件:
- 事件类型(政策/业绩/并购/股权/违规...)
- 关联个股/板块
- 影响程度
"""

import re
from typing import Optional
from loguru import logger

from app.ai.provider import ai_provider


# ========== AI 事件提取 ==========

EVENT_SYSTEM_PROMPT = """你是一个专业的A股事件提取引擎。
从新闻中提取结构化事件信息，返回 JSON:

{
  "events": [
    {
      "type": "policy" | "earnings" | "m&a" | "equity" | "violation" | "product" | "rating" | "other",
      "title": "事件标题(简短)",
      "description": "事件描述",
      "impact": "high" | "medium" | "low",
      "direction": "bullish" | "bearish" | "neutral",
      "related_codes": ["600xxx", "000xxx"],
      "related_sectors": ["板块1"],
      "timeline": "即时" | "短期(1-5天)" | "中期(1-4周)" | "长期(1月+)"
    }
  ]
}

事件类型说明:
- policy: 政策法规变化
- earnings: 业绩/财报相关
- m&a: 并购重组
- equity: 增减持/回购/股权变动
- violation: 违规处罚
- product: 产品/技术突破
- rating: 机构评级调整
关联个股规则:
- related_codes 只能填写新闻标题或正文明确出现的股票代码/公司简称对应代码；不能因为行业受益自行联想龙头股。
- 如果新闻只描述行业、商品价格、宏观政策或海外市场，related_codes 必须返回空数组，把方向放到 related_sectors。
- description 要说明利好/利空传导逻辑，例如“油价上行预期改善上游油气盈利”，不要只复述标题。

只返回 JSON，不要其他文字。"""


async def extract_events_ai(title: str, content: str = "") -> Optional[dict]:
    """AI 事件提取"""
    text = f"标题: {title}"
    if content:
        text += f"\n内容: {content[:1500]}"

    result = await ai_provider.chat_json(
        prompt=text,
        system=EVENT_SYSTEM_PROMPT,
    )
    if (not isinstance(result, dict) or not isinstance(result.get("events"), list)
            or not all(isinstance(event, dict) for event in result["events"])):
        return None
    logger.debug(f"AI 事件提取: {title[:30]} → {len(result['events'])} 个事件")
    return result


# ========== 正则降级事件提取 ==========

EVENT_PATTERNS = [
    # 政策
    (r"(国务院|发改委|央行|证监会|银保监|工信部|商务部).*(发布|印发|出台|通知|意见|规划)", "policy"),
    (r"(政策|补贴|扶持|鼓励|支持|规范|监管|审查)", "policy"),
    # 业绩
    (r"(业绩|财报|年报|季报|半年报|净利润|营收|毛利率|ROE|EPS)", "earnings"),
    (r"(预增|预减|预亏|扭亏|业绩快报|业绩预告)", "earnings"),
    # 并购
    (r"(并购|重组|收购|兼并|合并|资产注入|借壳)", "m&a"),
    # 股权
    (r"(增持|减持|回购|定增|股权激励|限售|解禁|质押)", "equity"),
    (r"(股东|实控人|大股东).*?(增|减|变|转让)", "equity"),
    # 违规
    (r"(违规|处罚|警示|立案|调查|罚款|警告|监管函)", "violation"),
    # 产品
    (r"(发布|发布|上线|突破|量产|获证|审批通过|新药|新产品)", "product"),
    # 评级
    (r"(评级|上调|下调|买入|增持|中性|减持|卖出|目标价)", "rating"),
]

CODE_PATTERN = re.compile(r"(6\d{5}|0\d{5}|3\d{5}|8\d{5})")
SECTOR_PATTERN = re.compile(r"(行业|概念|板块).*?([\u4e00-\u9fa5]{2,6})")


def extract_events_keyword(title: str, content: str = "") -> dict:
    """正则事件提取(降级)"""
    text = f"{title} {content}"
    events = []

    for pattern, event_type in EVENT_PATTERNS:
        if re.search(pattern, text):
            # 提取股票代码
            codes = list(set(CODE_PATTERN.findall(text)))
            # 提取板块
            sectors = []
            for m in SECTOR_PATTERN.finditer(text):
                sectors.append(m.group(2))

            # 判断影响方向
            direction = "neutral"
            if any(kw in text for kw in ["利好", "增长", "突破", "增持", "买入", "获批"]):
                direction = "bullish"
            elif any(kw in text for kw in ["利空", "下降", "违规", "减持", "卖出", "处罚"]):
                direction = "bearish"

            events.append({
                "type": event_type,
                "title": title[:40],
                "description": "",
                "impact": "medium",
                "direction": direction,
                "related_codes": codes[:5],
                "related_sectors": sectors[:3],
                "timeline": "短期(1-5天)",
            })
            break  # 只取最匹配的类型

    return {"events": events}


# ========== 统一接口 ==========

async def extract_events(title: str, content: str = "") -> dict:
    """统一事件提取入口 — AI 优先，正则降级"""
    ai_result = await extract_events_ai(title, content)
    # A valid empty list is an AI conclusion, not a reason to invent keyword events.
    if ai_result is not None:
        ai_result["method"] = "ai"
        return ai_result

    result = extract_events_keyword(title, content)
    result["method"] = "keyword"
    return result
