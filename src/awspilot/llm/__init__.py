"""LLM providers."""

from __future__ import annotations

from awspilot.config import Settings
from awspilot.llm.base import LLMError, LLMProvider
from awspilot.llm.fake import FakeProvider

__all__ = ["FakeProvider", "LLMError", "LLMProvider", "get_provider"]


def get_provider(settings: Settings) -> LLMProvider:
    if settings.provider == "anthropic":
        from awspilot.llm.anthropic import AnthropicProvider

        return AnthropicProvider(settings.model)
    if settings.provider == "openai":
        from awspilot.llm.openai import OpenAIProvider

        return OpenAIProvider(settings.model)
    raise LLMError(f"unknown provider {settings.provider!r}; use 'anthropic' or 'openai'")
