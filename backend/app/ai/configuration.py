"""AI 接入配置：环境变量兜底，页面配置原子持久化，密钥只保留在服务端。"""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class AIConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    auth_mode: Literal["api_key", "openai_oauth"] = "api_key"
    api_format: Literal["anthropic", "openai"] = "anthropic"
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=120)
    oauth_model: str = Field(default="", max_length=120)
    oauth_reasoning_effort: Literal["", "none", "minimal", "low", "medium", "high", "xhigh"] = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    max_tokens: int = Field(default=2048, ge=64, le=32768)
    temperature: float = Field(default=0.3, ge=0, le=2, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        value = value.strip().rstrip("/")
        url = urlsplit(value)
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("填写不含账号、查询参数的 HTTP(S) API 地址")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("非本机 API 地址必须使用 HTTPS")
        return value

    @field_validator("model", "oauth_model", mode="before")
    @classmethod
    def validate_model(cls, value):
        return value.strip() if isinstance(value, str) else value


class AIConfigurationUpdate(AIConfiguration):
    api_key: SecretStr | None = None
    clear_api_key: bool = False


def save_configuration(path: Path, config: AIConfiguration):
    """临时文件与目标位于同目录；失败时不改变当前生效配置。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = config.model_dump(exclude={"api_key"})
    payload["api_key"] = config.api_key.get_secret_value()
    fd, temporary = tempfile.mkstemp(prefix=".ai-config-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
