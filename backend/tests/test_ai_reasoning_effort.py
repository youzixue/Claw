"""Reasoning settings tests: isolated persistence and mocked bridge, no real inference."""
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.ai.configuration import AIConfiguration
from app.ai.provider import AIProvider
from test_ai_configuration import provider, update


@pytest.mark.parametrize("effort", ["", "none", "minimal", "low", "medium", "high", "xhigh"])
@pytest.mark.asyncio
async def test_effort_roundtrip_status_and_dispatch(provider, monkeypatch, effort):
    from app.ai.codex_bridge import codex_bridge
    chat = AsyncMock(return_value="answer")
    monkeypatch.setattr(codex_bridge, "chat", chat)
    monkeypatch.setattr(codex_bridge, "status", AsyncMock(return_value={"connected": True, "chat_available": True}))
    await provider.configure(update(auth_mode="openai_oauth", oauth_model="chosen", oauth_reasoning_effort=effort))
    restored = AIProvider(provider.config_path)
    assert restored.oauth_reasoning_effort == effort
    assert (await restored.status())["oauth_reasoning_effort"] == effort
    assert await restored.chat("news", "system") == "answer"
    chat.assert_awaited_once_with("news", system="system", model="chosen", reasoning_effort=effort)


@pytest.mark.parametrize("value", ["auto", "HIGH", " high ", None, 1, [], {}])
def test_rejects_invalid_effort(value):
    with pytest.raises(ValidationError):
        update(oauth_reasoning_effort=value)


@pytest.mark.asyncio
async def test_old_configuration_and_old_client_preserve_oauth(provider):
    assert AIConfiguration(base_url="https://example.com", model="m").oauth_reasoning_effort == ""
    await provider.configure(update(auth_mode="openai_oauth", oauth_model="chosen", oauth_reasoning_effort="xhigh"))
    await provider.configure(update(auth_mode="api_key"))
    assert provider.oauth_model == "chosen"
    assert provider.oauth_reasoning_effort == "xhigh"
    assert AIProvider(provider.config_path).oauth_reasoning_effort == "xhigh"
    payload = json.loads(provider.config_path.read_text())
    del payload["oauth_reasoning_effort"]
    provider.config_path.write_text(json.dumps(payload))
    assert AIProvider(provider.config_path).oauth_reasoning_effort == ""


@pytest.mark.asyncio
async def test_effort_failed_write_preserves_active_setting(provider, monkeypatch):
    await provider.configure(update(oauth_reasoning_effort="high"))
    def fail(*args):
        raise OSError("test write failure")
    monkeypatch.setattr("app.ai.provider.save_configuration", fail)
    with pytest.raises(OSError):
        await provider.configure(update(oauth_reasoning_effort="low"))
    assert provider.oauth_reasoning_effort == "high"
