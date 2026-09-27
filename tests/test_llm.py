from __future__ import annotations

import pytest

from awspilot.llm import FakeProvider, LLMError
from awspilot.llm.base import extract_json
from awspilot.models import Plan
from tests.helpers import BUCKET, plan


def test_extract_json_variants() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": 1} hope it helps') == {"a": 1}
    with pytest.raises(ValueError, match="no JSON"):
        extract_json("nothing here")


def test_retries_with_error_feedback() -> None:
    llm = FakeProvider(['{"summary": "x"}', plan(BUCKET)])
    result = llm.complete_json("sys", "make a bucket", Plan)
    assert result.steps[0].id == "create_bucket"
    assert "invalid" in llm.prompts[1][-1]["content"]


def test_gives_up_after_retries() -> None:
    llm = FakeProvider(["nope"] * 3)
    with pytest.raises(LLMError, match="never produced valid Plan"):
        llm.complete_json("sys", "x", Plan, retries=2)


def test_plan_rejects_forward_dependency() -> None:
    bad = plan({**BUCKET, "depends_on": ["later"]})
    with pytest.raises(ValueError, match="must appear earlier"):
        Plan.model_validate_json(bad)
