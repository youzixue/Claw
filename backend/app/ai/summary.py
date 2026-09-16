"""摘要生成 — AI 增强 + 截断降级

为新闻生成一句话摘要:
- AI: LLM 生成高质量摘要
- 降级: 取标题/内容前 100 字
"""

from typing import Optional
from loguru import logger

from app.ai.provider import ai_provider


# ========== AI 摘要 ==========

SUMMARY_SYSTEM_PROMPT = """你是一个专业的A股财经新闻摘要引擎。
为以下新闻生成一句话摘要，返回 JSON:
{"summary": "不超过50字的摘要"}
只返回 JSON，不要解释、推理过程或其他内容。"""


async def generate_summary_ai(title: str, content: str = "") -> Optional[str]:
    """AI 生成摘要"""
    text = f"标题: {title}"
    if content:
        text += f"\n内容: {content[:800]}"

    result = await ai_provider.chat_json(
        prompt=text,
        system=SUMMARY_SYSTEM_PROMPT,
    )
    if result and result.get("summary"):
        summary = _clean_summary(str(result.get("summary") or ""))
        logger.debug(f"AI 摘要: {title[:20]} → {summary[:30]}")
        return summary
    return None


def _clean_summary(summary: str) -> str:
    """清理模型偶发返回的解释性前缀，只保留摘要正文."""
    summary = summary.strip().strip("`").strip()
    if not summary:
        return ""
    for marker in ("摘要：", "摘要:", "一句话摘要：", "一句话摘要:"):
        if marker in summary:
            summary = summary.split(marker, 1)[-1].strip()
    lines = [
        line.strip(" -\t")
        for line in summary.splitlines()
        if line.strip() and not line.strip().startswith(("用户要求", "让我", "这条新闻", "我需要", "分析"))
    ]
    summary = lines[-1] if lines else summary
    return summary[:80].rstrip("，。、；; ")


# ========== 截断降级 ==========

def generate_summary_fallback(title: str, content: str = "") -> str:
    """截断降级摘要"""
    if content and len(content) > 20:
        return content[:80].rstrip("，。、；") + "..."
    return title


# ========== 统一接口 ==========

async def generate_summary(title: str, content: str = "") -> str:
    """统一摘要入口 — AI 优先，截断降级"""
    # 尝试 AI
    ai_result = await generate_summary_ai(title, content)
    if ai_result and len(ai_result) > 5:
        return ai_result.strip()

    # 降级
    return generate_summary_fallback(title, content)
