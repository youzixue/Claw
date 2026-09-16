"""AI 模块 API — LLM调用+配置"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.ai.configuration import AIConfigurationUpdate

from app.ai.provider import ai_provider
from app.ai.sentiment import analyze_sentiment
from app.ai.event_extractor import extract_events
from app.ai.summary import generate_summary

router = APIRouter()


@router.get("/status")
async def ai_status():
    """AI模块状态"""
    return await ai_provider.status()


async def require_local_configuration(request: Request):
    """配置与账号授权只开放给本机同源工作台，勿裸露到公网。"""
    local = {"127.0.0.1", "localhost", "::1"}
    if not request.client or request.client.host not in local or request.url.hostname not in local:
        raise HTTPException(403, "AI 接入配置仅允许从本机工作台操作")
    origin = request.headers.get("origin")
    if origin and origin not in {str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"}:
        raise HTTPException(403, "不允许跨站修改 AI 接入配置")


@router.put("/config", dependencies=[Depends(require_local_configuration)])
async def update_ai_configuration(payload: AIConfigurationUpdate):
    try:
        await ai_provider.configure(payload)
    except OSError:
        raise HTTPException(503, "AI 配置未保存，请检查服务端配置目录写权限") from None
    return await ai_provider.status()


@router.post("/test", dependencies=[Depends(require_local_configuration)])
async def test_ai_connection():
    """仅测试已保存配置；不把模型输出或凭据返回浏览器。"""
    if not ai_provider.enabled:
        return {"ok": False, "message": "AI 未启用，请保存并启用后测试"}
    result = await ai_provider.chat("Reply with OK only.", "This is a connection test.", max_tokens=64, temperature=0)
    return {"ok": bool(result), "message": "连接成功" if result else "连接未通过，请检查授权、模型、额度及网络；未切换到其他付费服务"}


@router.get("/oauth/models", dependencies=[Depends(require_local_configuration)])
async def list_openai_models():
    """只读 Codex 模型目录；不切换全局接入、不发送模型请求。"""
    from app.ai.codex_bridge import codex_bridge
    return await codex_bridge.list_models()


@router.post("/oauth/start", dependencies=[Depends(require_local_configuration)])
async def start_openai_login():
    from app.ai.codex_bridge import codex_bridge
    try:
        return await codex_bridge.start_login()
    except Exception:
        raise HTTPException(503, "无法启动 OpenAI 授权，请检查 Codex CLI 安装、版本及网络") from None


@router.post("/oauth/cancel", dependencies=[Depends(require_local_configuration)])
async def cancel_openai_login():
    from app.ai.codex_bridge import codex_bridge
    await codex_bridge.cancel_login()
    return await codex_bridge.status()


@router.post("/oauth/logout", dependencies=[Depends(require_local_configuration)])
async def logout_openai():
    from app.ai.codex_bridge import codex_bridge
    await codex_bridge.logout()
    return await codex_bridge.status()


@router.post("/sentiment")
async def ai_sentiment(title: str, content: str = ""):
    """AI情感分析"""
    result = await analyze_sentiment(title, content)
    return {"result": result}


@router.post("/events")
async def ai_events(title: str, content: str = ""):
    """AI事件提取"""
    result = await extract_events(title, content)
    return {"result": result}


@router.post("/summary")
async def ai_summary(title: str, content: str = ""):
    """AI摘要生成"""
    result = await generate_summary(title, content)
    return {"result": result}


@router.post("/chat")
async def ai_chat(prompt: str, system: str = ""):
    """AI对话(测试用)"""
    result = await ai_provider.chat(prompt, system)
    return {"result": result}
