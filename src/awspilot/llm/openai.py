"""OpenAI provider (GPT-4o by default), via the official OpenAI SDK."""

from __future__ import annotations

from awspilot.llm.base import LLMError, LLMProvider


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        try:
            import openai
        except ImportError as exc:
            raise LLMError("install the extra: uv sync --extra openai") from exc
        self._sdk = openai
        self._client = openai.OpenAI()

    def _complete(self, system: str, messages: list[dict[str, str]]) -> str:
        sdk = self._sdk
        try:
            response = self._client.chat.completions.create(  # type: ignore[call-overload]
                model=self.model,
                messages=[{"role": "system", "content": system}, *messages],
                response_format={"type": "json_object"},
            )
        except sdk.AuthenticationError as exc:
            raise LLMError("OpenAI credentials are missing or invalid") from exc
        except sdk.APIStatusError as exc:
            raise LLMError(f"OpenAI API error {exc.status_code}") from exc
        except sdk.APIConnectionError as exc:
            raise LLMError("could not reach the OpenAI API") from exc

        self.usage.calls += 1
        if response.usage:
            self.usage.input_tokens += response.usage.prompt_tokens
            self.usage.output_tokens += response.usage.completion_tokens
        return response.choices[0].message.content or ""
