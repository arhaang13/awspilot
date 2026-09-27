"""Claude provider, via the official Anthropic SDK."""

from __future__ import annotations

from typing import Any

from awspilot.llm.base import LLMError, LLMProvider


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str, effort: str = "high") -> None:
        super().__init__(model)
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("install the extra: uv sync --extra anthropic") from exc
        self._sdk = anthropic
        # Credentials come from the environment (ANTHROPIC_API_KEY or a login profile).
        self._client = anthropic.Anthropic()
        self._effort = effort

    def _complete(self, system: str, messages: list[dict[str, str]]) -> str:
        sdk = self._sdk
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            # The schema-bearing system prompt is identical across a run, so cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if not self.model.startswith("claude-haiku"):
            params["output_config"] = {"effort": self._effort}
        try:
            response = self._client.messages.create(**params)
        except sdk.AuthenticationError as exc:
            raise LLMError("Anthropic credentials are missing or invalid") from exc
        except sdk.RateLimitError as exc:
            raise LLMError("Anthropic rate limit hit after retries") from exc
        except sdk.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise LLMError("could not reach the Anthropic API") from exc

        self.usage.calls += 1
        self.usage.input_tokens += response.usage.input_tokens
        self.usage.output_tokens += response.usage.output_tokens
        if response.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("the model's reply was cut off at max_tokens")
        return "".join(block.text for block in response.content if block.type == "text")
