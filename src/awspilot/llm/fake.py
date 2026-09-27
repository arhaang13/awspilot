"""Scripted provider for tests and offline demos. Makes no network calls."""

from __future__ import annotations

from collections.abc import Iterable

from awspilot.llm.base import LLMError, LLMProvider


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, responses: Iterable[str], model: str = "fake") -> None:
        super().__init__(model)
        self._responses = list(responses)
        self.prompts: list[list[dict[str, str]]] = []

    def _complete(self, system: str, messages: list[dict[str, str]]) -> str:
        self.prompts.append(messages)
        self.usage.calls += 1
        if not self._responses:
            raise LLMError("FakeProvider ran out of scripted responses")
        return self._responses.pop(0)
