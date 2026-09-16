"""隔离 AI 配置及协议回归；不读取真实凭据、不调用真实服务。"""
import asyncio
import json
import stat

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.ai.configuration import AIConfigurationUpdate
from app.ai.provider import AIProvider
from app.config.settings import settings


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "AI_AUTH_MODE", "api_key")
    monkeypatch.setattr(settings, "AI_API_FORMAT", "anthropic")
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.example.com/anthropic")
    monkeypatch.setattr(settings, "AI_MODEL", "test-model")
    monkeypatch.setattr(settings, "AI_CODEX_MODEL", "")
    monkeypatch.setattr(settings, "AI_CODEX_REASONING_EFFORT", "")
    return AIProvider(tmp_path / "private" / "ai.json")


def update(**kwargs):
    return AIConfigurationUpdate(
        **({"base_url": "https://api.example.com/anthropic", "model": "test-model",
            "api_key": "test-secret", "enabled": True} | kwargs)
    )


@pytest.mark.asyncio
async def test_config_roundtrip_keep_clear_and_private_permissions(provider):
    await provider.configure(update())
    assert provider.enabled
    assert stat.S_IMODE(provider.config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(provider.config_path.parent.stat().st_mode) == 0o700
    restored = AIProvider(provider.config_path)
    assert restored.api_key == "test-secret"
    await provider.configure(update(api_key=None))
    assert provider.api_key == "test-secret"
    await provider.configure(update(api_key=None, clear_api_key=True))
    assert provider.api_key == ""


@pytest.mark.asyncio
async def test_changing_endpoint_never_reuses_old_secret(provider):
    await provider.configure(update())
    await provider.configure(update(base_url="https://different.example/v1", api_key=None))
    assert provider.api_key == ""


@pytest.mark.asyncio
async def test_failed_save_does_not_change_active_provider(provider, monkeypatch):
    await provider.configure(update())
    def deny(*_):
        raise PermissionError("blocked")
    monkeypatch.setattr("app.ai.provider.save_configuration", deny)
    with pytest.raises(PermissionError):
        await provider.configure(update(model="new-model"))
    assert provider.model == "test-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,base,suffix", [
    ("anthropic", "https://api.example.com/anthropic", "/anthropic/v1/messages"),
    ("anthropic", "https://api.example.com/v1", "/v1/messages"),
    ("openai", "https://api.example.com/v1", "/v1/chat/completions"),
    ("openai", "https://api.example.com", "/v1/chat/completions"),
])
async def test_protocol_headers_and_response(provider, protocol, base, suffix):
    await provider.configure(update(api_format=protocol, base_url=base))
    def handler(request):
        assert request.url.path == suffix
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        if protocol == "openai":
            assert request.headers["Authorization"] == "Bearer test-secret"
            assert "x-api-key" not in request.headers
            assert payload["messages"][0]["role"] == "system"
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        assert request.headers["x-api-key"] == "test-secret"
        assert "Authorization" not in request.headers
        assert payload["system"] == "system"
        return httpx.Response(200, json={"content": [{"type": "thinking", "thinking": "not output"}, {"type": "text", "text": "ok"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider._client = client
        assert await provider.chat("prompt", "system") == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [(401, {"error": "secret reflected"}), (429, {}), (200, {"choices": []})])
async def test_error_responses_fail_closed(provider, status, payload):
    await provider.configure(update(api_format="openai"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))) as client:
        provider._client = client
        assert await provider.chat("prompt") is None


@pytest.mark.parametrize("kwargs", [
    {"base_url": "file:///etc/passwd"}, {"base_url": "https://u:password@host.example"},
    {"base_url": "http://host.example"}, {"temperature": float("nan")},
    {"model": "   "}, {"max_tokens": 0}, {"auth_mode": "fake-oauth"},
])
def test_config_rejects_invalid_input(kwargs):
    with pytest.raises(ValidationError):
        update(**kwargs)


def test_corrupt_saved_config_disables_ai(provider):
    provider.config_path.parent.mkdir(parents=True)
    provider.config_path.write_text("{bad", encoding="utf-8")
    restored = AIProvider(provider.config_path)
    assert not restored.enabled
    assert restored.configuration_error


@pytest.mark.asyncio
async def test_status_never_returns_key(provider, monkeypatch):
    from app.ai.codex_bridge import codex_bridge
    async def oauth_status():
        return {"available": False, "connected": False}
    monkeypatch.setattr(codex_bridge, "status", oauth_status)
    await provider.configure(update())
    response = await provider.status()
    assert response["has_key"] and response["ready"]
    assert "api_key" not in response
    assert "test-secret" not in json.dumps(response)


@pytest.mark.asyncio
async def test_oauth_dispatch_does_not_call_key_endpoint(provider, monkeypatch):
    from app.ai.codex_bridge import codex_bridge
    calls = []
    async def chat(prompt, system="", model="", reasoning_effort=""):
        calls.append((prompt, system, model, reasoning_effort))
        return "oauth answer"
    monkeypatch.setattr(codex_bridge, "chat", chat)
    await provider.configure(update(auth_mode="openai_oauth", oauth_model="account-model", oauth_reasoning_effort="high"))
    assert await provider.chat("news", "system") == "oauth answer"
    assert calls == [("news", "system", "account-model", "high")]
    assert provider._client is None
    assert provider.api_key == "test-secret"


@pytest.mark.asyncio
async def test_config_route_rejects_remote_and_cross_site(provider, monkeypatch):
    from app.api.v1 import ai
    monkeypatch.setattr(ai, "ai_provider", provider)
    app = FastAPI()
    app.include_router(ai.router, prefix="/ai")
    payload = update().model_dump(exclude={"api_key"})
    payload["api_key"] = "test-secret"
    for host, base, origin in [
        ("10.0.0.8", "http://localhost", "http://localhost:5173"),
        ("127.0.0.1", "http://evil.example", "http://evil.example"),
        ("127.0.0.1", "http://localhost", "https://evil.example"),
    ]:
        transport = httpx.ASGITransport(app=app, client=(host, 123))
        async with httpx.AsyncClient(transport=transport, base_url=base) as client:
            response = await client.put("/ai/config", json=payload, headers={"Origin": origin})
            assert response.status_code == 403
    assert not provider.config_path.exists()


@pytest.mark.asyncio
async def test_local_config_route_preserves_secret_boundary(provider, monkeypatch):
    from app.api.v1 import ai
    from app.ai.codex_bridge import codex_bridge
    async def oauth_status():
        return {"available": False, "connected": False, "chat_available": False}
    monkeypatch.setattr(codex_bridge, "status", oauth_status)
    monkeypatch.setattr(ai, "ai_provider", provider)
    app = FastAPI()
    app.include_router(ai.router, prefix="/ai")
    payload = update().model_dump(exclude={"api_key"})
    payload["api_key"] = "test-secret"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://127.0.0.1:8000") as client:
        response = await client.put("/ai/config", json=payload, headers={"Origin": "http://localhost:5173"})
        assert response.status_code == 200
        assert response.json()["has_key"]
        assert "test-secret" not in response.text
        assert "api_key" not in response.json()
        assert provider.api_key == "test-secret"


def test_invalid_environment_disables_ai_without_breaking_app(provider, monkeypatch):
    monkeypatch.setattr(settings, "AI_BASE_URL", "not-a-url")
    restored = AIProvider(provider.config_path)
    assert not restored.enabled
    assert restored.configuration_error


@pytest.mark.asyncio
async def test_oauth_connection_alone_does_not_claim_analysis_ready(provider, monkeypatch):
    from app.ai.codex_bridge import codex_bridge
    async def status():
        return {"available": True, "connected": True, "chat_available": False}
    monkeypatch.setattr(codex_bridge, "status", status)
    await provider.configure(update(auth_mode="openai_oauth"))
    assert not (await provider.status())["ready"]


@pytest.mark.asyncio
async def test_model_catalog_route_is_local_readonly(provider, monkeypatch):
    from app.api.v1 import ai
    from app.ai.codex_bridge import codex_bridge
    from unittest.mock import AsyncMock
    catalog = AsyncMock(return_value={"ok": True, "models": [{"id": "test-model", "label": "Test", "is_default": True}]})
    monkeypatch.setattr(codex_bridge, "list_models", catalog)
    monkeypatch.setattr(ai, "ai_provider", provider)
    app = FastAPI()
    app.include_router(ai.router, prefix="/ai")
    for host, origin, expected in [
        ("127.0.0.1", "http://localhost:5173", 200),
        ("10.0.0.1", "http://localhost:5173", 403),
        ("127.0.0.1", "https://evil.example", 403),
    ]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 123)), base_url="http://localhost:8000") as client:
            response = await client.get("/ai/oauth/models", headers={"Origin": origin})
            assert response.status_code == expected
    catalog.assert_awaited_once()
    assert not provider.config_path.exists()


@pytest.mark.asyncio
async def test_oauth_model_save_persists_even_when_analysis_not_ready(provider, monkeypatch):
    from app.api.v1 import ai
    from app.ai.codex_bridge import codex_bridge
    async def status():
        return {"available": True, "connected": True, "chat_available": False}
    monkeypatch.setattr(codex_bridge, "status", status)
    monkeypatch.setattr(ai, "ai_provider", provider)
    await provider.configure(update())
    app = FastAPI()
    app.include_router(ai.router, prefix="/ai")
    payload = update(auth_mode="openai_oauth", oauth_model="chosen-account-model", api_key=None).model_dump(exclude={"api_key"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://127.0.0.1:8000") as client:
        response = await client.put("/ai/config", json=payload)
    assert response.status_code == 200
    assert response.json()["oauth_model"] == "chosen-account-model"
    assert response.json()["ready"] is False
    assert "test-secret" not in response.text
    restored = AIProvider(provider.config_path)
    assert restored.auth_mode == "openai_oauth"
    assert restored.oauth_model == "chosen-account-model"
    assert restored.api_key == "test-secret"
