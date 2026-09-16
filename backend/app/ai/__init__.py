"""Claw AI 模块 — 可选增强层

设计:
- 有 API Key → LLM 增强(新闻情感/事件提取/摘要)
- 没 API Key → 降级为关键词+正则，功能不丢
- 默认 MiniMax M2.7 (Anthropic 兼容)，支持换 DeepSeek/Qwen/OpenAI
"""
