"""新闻情感分析 — AI 增强 + 关键词降级

优先级:
1. AI (MiniMax M2.7) → 结构化情感分析
2. 关键词词典 → 正则匹配兜底
"""

import re
from typing import Optional
from loguru import logger

from app.ai.provider import ai_provider

VALID_SENTIMENTS = {"bullish", "bearish", "neutral"}
VALID_IMPACT_SCOPES = {"stock", "sector", "market", "global"}


# ========== 关键词词典(降级兜底) ==========

BULL_KEYWORDS = [
    "利好", "涨停", "大涨", "暴涨", "突破", "新高", "翻倍", "强势",
    "增持", "回购", "业绩超预期", "净利润增长", "营收增长", "中标",
    "签约", "获批", "放量", "机构买入", "北向流入", "主力加仓",
    "政策利好", "降准", "降息", "刺激", "扶持", "补贴",
]

BEAR_KEYWORDS = [
    "利空", "跌停", "大跌", "暴跌", "破位", "新低", "减值", "亏损",
    "减持", "清仓", "暴雷", "违规", "处罚", "退市", "退市风险",
    "商誉减值", "计提", "业绩不及预期", "营收下滑", "违约",
    "质押平仓", "冻结", "调查", "诉讼", "停牌", "崩盘",
    "北向流出", "主力出逃", "恐慌",
]

INTENSIFIERS = ["重大", "强烈", "极其", "历史性", "罕见", "突发"]


# ========== AI 增强情感分析 ==========

SENTIMENT_SYSTEM_PROMPT = """你是一个专业的A股新闻情感分析引擎。
分析新闻标题和内容，返回 JSON 格式结果:

{
  "sentiment": "bullish" | "bearish" | "neutral",
  "confidence": 0.0-1.0,
  "impact_scope": "stock" | "sector" | "market" | "global",
  "related_sectors": ["板块1", "板块2"],
  "summary": "一句话摘要",
  "key_points": ["要点1", "要点2"]
}

注意:
- sentiment 基于对A股市场的影响判断
- confidence 表示判断信心(0-1)
- impact_scope 表示影响范围
- related_sectors 列出受影响的板块
- 不要把行业/宏观新闻强行归因到未被标题或正文明确提及的个股
- key_points 要写清楚利好/利空传导逻辑、受益对象和时效，不要只复述标题
- 只返回 JSON，不要其他文字"""


async def analyze_sentiment_ai(title: str, content: str = "") -> Optional[dict]:
    """AI 增强情感分析

    Returns:
        {sentiment, confidence, impact_scope, related_sectors, summary, key_points}
        或 None(AI 不可用时)
    """
    text = f"标题: {title}"
    if content:
        text += f"\n内容: {content[:1000]}"  # 限制长度

    result = await ai_provider.chat_json(
        prompt=text,
        system=SENTIMENT_SYSTEM_PROMPT,
    )
    if not isinstance(result, dict):
        return None
    if result:
        logger.debug(f"AI 情感分析: {title[:30]} → {result.get('sentiment')}")
    return result


# ========== 关键词降级情感分析 ==========

def analyze_sentiment_keyword(title: str, content: str = "") -> dict:
    """关键词 + 正则情感分析(降级方案)

    Returns:
        {sentiment, confidence, impact_scope, related_sectors}
    """
    text = f"{title} {content}"

    bull_count = sum(1 for kw in BULL_KEYWORDS if kw in text)
    bear_count = sum(1 for kw in BEAR_KEYWORDS if kw in text)
    intensifier_count = sum(1 for kw in INTENSIFIERS if kw in text)

    # 计算情感分数
    score = (bull_count - bear_count) * (1 + 0.2 * intensifier_count)

    if score > 1:
        sentiment = "bullish"
        confidence = min(0.5 + 0.1 * abs(score), 0.9)
    elif score < -1:
        sentiment = "bearish"
        confidence = min(0.5 + 0.1 * abs(score), 0.9)
    elif score > 0:
        sentiment = "bullish"
        confidence = 0.4
    elif score < 0:
        sentiment = "bearish"
        confidence = 0.4
    else:
        sentiment = "neutral"
        confidence = 0.3

    # 影响范围判断
    if any(kw in text for kw in ["A股", "大盘", "沪指", "深成指", "全市场"]):
        impact_scope = "market"
    elif any(kw in text for kw in ["行业", "板块", "概念"]):
        impact_scope = "sector"
    else:
        impact_scope = "stock"

    return {
        "sentiment": sentiment,
        "confidence": confidence,
        "impact_scope": impact_scope,
        "related_sectors": [],
    }


# ========== 统一接口 ==========

async def analyze_sentiment(title: str, content: str = "") -> dict:
    """统一情感分析入口 — AI 优先，关键词降级

    Returns:
        {sentiment, confidence, impact_scope, related_sectors, summary?, key_points?, method}
    """
    # 尝试 AI
    ai_result = await analyze_sentiment_ai(title, content)
    if ai_result and ai_result.get("sentiment"):
        normalized = normalize_sentiment_result(ai_result)
        if normalized:
            normalized["method"] = "ai"
            return normalized
        logger.warning(f"AI 情感分析字段非法，降级关键词: {title[:30]}")

    # 降级关键词
    result = analyze_sentiment_keyword(title, content)
    result["method"] = "keyword"
    return result


def normalize_sentiment_result(result: dict | None) -> Optional[dict]:
    """校验并归一化 AI 情感结果，避免模型漂移污染交易方向字段."""
    if not isinstance(result, dict):
        return None

    sentiment = str(result.get("sentiment") or "").strip().lower()
    if sentiment not in VALID_SENTIMENTS:
        return None

    scope = str(result.get("impact_scope") or "stock").strip().lower()
    if scope not in VALID_IMPACT_SCOPES:
        scope = "stock"

    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    related_sectors = result.get("related_sectors") or []
    if not isinstance(related_sectors, list):
        related_sectors = []

    key_points = result.get("key_points") or []
    if not isinstance(key_points, list):
        key_points = []

    normalized = dict(result)
    normalized.update({
        "sentiment": sentiment,
        "confidence": confidence,
        "impact_scope": scope,
        "related_sectors": [str(v).strip() for v in related_sectors if str(v).strip()][:8],
        "key_points": [str(v).strip() for v in key_points if str(v).strip()][:6],
    })
    return normalized
