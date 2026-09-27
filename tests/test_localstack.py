"""The same reference plans, against a real LocalStack. Run with `-m integration`."""

from __future__ import annotations

from pathlib import Path

import pytest

from awspilot import engine, recipes
from awspilot.aws.caller import AwsCaller
from awspilot.bench.runner import clean_backend
from awspilot.config import Settings
from awspilot.llm import FakeProvider
from awspilot.recipes import Recipe
from awspilot.runner import run

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("recipe", recipes.load_all(), ids=lambda r: r.id)
def test_reference_plan_on_localstack(recipe: Recipe, tmp_path: Path) -> None:
    assert recipe.reference is not None
    settings = Settings(target="localstack", state_dir=tmp_path)
    with clean_backend(settings):
        caller = AwsCaller(settings)
        llm = FakeProvider([recipe.reference.model_dump_json()])
        result = run(recipe.request, llm, settings, caller=caller, auto_approve=True)
        assert result.status == "succeeded", result.error
        scope: dict = {}  # type: ignore[type-arg]
        for check in recipe.checks:
            outcome = engine.evaluate(check, scope, caller)
            assert outcome.ok, f"{outcome.description}: {outcome.detail}"
