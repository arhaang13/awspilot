"""Provider-neutral LLM interface.

The agent only ever needs one capability from a model: given instructions and a
request, return JSON that validates against a pydantic schema. Keeping the
interface that small is what makes providers swappable.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from awspilot.models import Usage

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The provider failed or never produced valid output."""


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, model: str) -> None:
        self.model = model
        self.usage = Usage()

    @abstractmethod
    def _complete(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return the model's raw text for a conversation."""

    def complete_json(self, system: str, user: str, schema: type[T], retries: int = 2) -> T:
        """Ask for JSON matching `schema`; feed validation errors back on failure."""
        full_system = (
            f"{system}\n\nRespond with a single JSON object and nothing else. "
            f"It must validate against this JSON Schema:\n"
            f"{json.dumps(schema.model_json_schema(), sort_keys=True)}"
        )
        messages = [{"role": "user", "content": user}]
        last_error = ""
        for _ in range(retries + 1):
            text = self._complete(full_system, messages)
            try:
                return schema.model_validate(extract_json(text))
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": f"That output was invalid:\n{last_error}\n"
                        "Reply with the corrected JSON object only.",
                    },
                ]
        raise LLMError(f"{self.name} never produced valid {schema.__name__}: {last_error}")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> object:
    """Parse JSON from model text, tolerating code fences and surrounding prose."""
    text = text.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in model output") from None
        return json.loads(text[start : end + 1])
