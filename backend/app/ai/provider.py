"""AI Provider — 保留 Anthropic API，并支持 OpenAI API 与官方账号授权桥接。"""

import asyncio
from pathlib import Path
import httpx
from typing import Optional
from loguru import logger

from app.config.settings import settings
from app.ai.configuration import AIConfiguration, AIConfigurationUpdate, save_configuration


class AIProvider:
    """可配置的 LLM Provider；下游 NLP 与规则回退接口保持不变。"""

    def __init__(self, config_path: Path | None = None):
        self.config_path = Path(config_path or settings.AI_CONFIG_PATH)
        self.configuration_error = False
        try:
            config = AIConfiguration(
                enabled=settings.AI_ENABLED, base_url=settings.AI_BASE_URL,
                api_key=settings.AI_API_KEY, model=settings.AI_MODEL,
                max_tokens=settings.AI_MAX_TOKENS, temperature=settings.AI_TEMPERATURE,
                auth_mode=settings.AI_AUTH_MODE, api_format=settings.AI_API_FORMAT,
                oauth_model=settings.AI_CODEX_MODEL,
                oauth_reasoning_effort=settings.AI_CODEX_REASONING_EFFORT,
            )
        except ValueError:
            # 配置错误只能停用 AI，不能使交易/行情应用启动失败。
            config = AIConfiguration(base_url="https://api.minimaxi.com/anthropic", model="MiniMax-M2.7")
            self.configuration_error = True
            logger.error("AI 环境配置无效，已停用 AI；请从页面重新配置")
        if self.config_path.exists():
            try:
                config = AIConfiguration.model_validate_json(self.config_path.read_text(encoding="utf-8"))
                self.configuration_error = False
            except (OSError, ValueError):
                config.enabled = False
                self.configuration_error = True
                logger.error("AI 页面配置无法读取，已停用 AI；请重新保存配置")
        self._apply_config(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(2)
        self._config_lock = asyncio.Lock()

    def _apply_config(self, config: AIConfiguration):
        self._config = config
        self.enabled = config.enabled
        self.base_url = config.base_url
        self.api_key = config.api_key.get_secret_value()
        self.model = config.model
        self.max_tokens = config.max_tokens
        self.temperature = config.temperature
        self.auth_mode = config.auth_mode
        self.api_format = config.api_format
        self.oauth_model = config.oauth_model
        self.oauth_reasoning_effort = config.oauth_reasoning_effort

    async def configure(self, update: AIConfigurationUpdate):
        async with self._config_lock:
            payload = update.model_dump(exclude={"api_key", "clear_api_key"})
            # Older clients omit OAuth additions; do not erase saved account parameters.
            for field in ("oauth_model", "oauth_reasoning_effort"):
                if field not in update.model_fields_set:
                    payload[field] = getattr(self._config, field)
            key = update.api_key.get_secret_value() if update.api_key else ""
            payload["api_key"] = "" if update.clear_api_key else (key or self.api_key)
            config = AIConfiguration.model_validate(payload)
            # 勿将已保存密钥自动发送到新地址；更换服务时要求重新输入。
            if config.base_url != self.base_url and not key:
                config.api_key = type(config.api_key)("")
            await asyncio.to_thread(save_configuration, self.config_path, config)
            self._apply_config(config)
            self.configuration_error = False

    async def status(self):
        from app.ai.codex_bridge import codex_bridge
        try:
            oauth = await codex_bridge.status()
        except Exception:
            oauth = {"available": False, "connected": False, "login_status": "error", "message": "账号服务暂不可用，请检查 Codex CLI"}
        result = self._config.model_dump(exclude={"api_key"})
        result.update({
            "has_key": bool(self.api_key),
            "oauth": oauth,
            "configuration_error": self.configuration_error,
            "ready": self.enabled and (bool(oauth.get("connected") and oauth.get("chat_available", False)) if self.auth_mode == "openai_oauth" else bool(self.api_key)),
            "active_model": (self.oauth_model or "账号默认模型") if self.auth_mode == "openai_oauth" else self.model,
            "scope": "全局 AI（新闻情感、事件和摘要等共用）",
        })
        return result

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0, follow_redirects=False)
        return self._client

    async def chat(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
        temperature: float = None,
    ) -> Optional[str]:
        """按已保存配置调用 API 或 OpenAI 账号通道

        Args:
            prompt: 用户消息
            system: 系统提示词
            max_tokens: 最大 token 数
            temperature: 温度

        Returns:
            LLM 返回的文本，或 None(不可用时)
        """
        if not self.enabled:
            logger.debug("AI 未启用，跳过 LLM 调用")
            return None

        # 每次调用捕获不可变配置，页面切换不混用排队请求的地址/密钥。
        config = self._config
        if config.auth_mode == "openai_oauth":
            from app.ai.codex_bridge import codex_bridge
            try:
                async with self._semaphore:
                    return await codex_bridge.chat(
                        prompt, system=system, model=config.oauth_model,
                        reasoning_effort=config.oauth_reasoning_effort,
                    )
            except Exception:
                logger.warning("OpenAI 账号调用失败，新闻继续按既有规则降级")
                return None
        key = config.api_key.get_secret_value()
        if not key:
            logger.warning("AI API Key 未配置")
            return None

        try:
            client = await self._get_client()
            payload = {
                "model": config.model,
                "max_tokens": max_tokens or config.max_tokens,
                "temperature": temperature if temperature is not None else config.temperature,
                "messages": [{"role": "user", "content": prompt}],
            }
            base = config.base_url
            if config.api_format == "openai":
                if system:
                    payload["messages"].insert(0, {"role": "system", "content": system})
                url = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
                headers = {"Authorization": f"Bearer {key}"}
            else:
                if system:
                    payload["system"] = system
                url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
                headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            async with self._semaphore:
                resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if config.api_format == "openai":
                content = data.get("choices", [{}])[0].get("message", {}).get("content")
                return content if isinstance(content, str) and content else None
            return "\n".join(
                block["text"] for block in data.get("content", [])
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            ) or None
        except httpx.HTTPStatusError as exc:
            # 不记录响应正文：代理可能在错误消息中回显密钥。
            logger.warning("AI API HTTP 错误: {}", exc.response.status_code)
            return None
        except Exception:
            logger.warning("AI API 请求失败（网络、协议或响应异常）")
            return None

    async def chat_json(
        self,
        prompt: str,
        system: str = "",
    ) -> Optional[dict]:
        """调用 LLM 并解析 JSON 响应

        Returns:
            解析后的 dict，或 None
        """
        import json
        import re

        text = await self.chat(prompt, system)
        if not text:
            return None

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 块
        match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试找第一个 { 到最后一个 }
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning("AI 返回非 JSON，使用既有规则降级")
        return None

    async def close(self):
        """关闭客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        from app.ai.codex_bridge import codex_bridge
        await codex_bridge.close()


# 全局 Provider
ai_provider = AIProvider()
