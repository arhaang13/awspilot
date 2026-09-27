"""Every recipe's reference plan must run and satisfy the recipe's own checks."""

from __future__ import annotations

import pytest

from awspilot import engine, recipes
from awspilot.aws.caller import AwsCaller
from awspilot.bench.runner import run_bench, to_markdown
from awspilot.config import Settings
from awspilot.llm import FakeProvider
from awspilot.recipes import Recipe
from awspilot.runner import cleanup, run
from awspilot.safety import review

ALL = recipes.load_all()


def test_library_is_not_empty() -> None:
    assert len(ALL) >= 15


@pytest.mark.parametrize("recipe", ALL, ids=lambda r: r.id)
def test_reference_plan_passes_safety_and_dry_run(recipe: Recipe, aws: AwsCaller,
                                                  settings: Settings) -> None:
    assert recipe.reference is not None
    assert review(recipe.reference, settings).ok
    assert engine.dry_run(recipe.reference, aws) == []


@pytest.mark.parametrize("recipe", ALL, ids=lambda r: r.id)
def test_reference_plan_satisfies_checks(recipe: Recipe, aws: AwsCaller,
                                         settings: Settings) -> None:
    assert recipe.reference is not None
    scope: dict = {}  # type: ignore[type-arg]

    # Before the run the checks must fail, or they prove nothing.
    before = [engine.evaluate(c, scope, aws) for c in recipe.checks]
    assert not all(r.ok for r in before)

    llm = FakeProvider([recipe.reference.model_dump_json()])
    result = run(recipe.request, llm, settings, caller=aws, auto_approve=True)
    assert result.status == "succeeded", result.error

    scope = {}
    for check in recipe.checks:
        outcome = engine.evaluate(check, scope, aws)
        assert outcome.ok, f"{outcome.description}: {outcome.detail}"

    assert cleanup(result.run_id, settings, caller=aws) == []


def test_matching_finds_the_relevant_recipe() -> None:
    found = recipes.match("make a DynamoDB table with TTL on expiresAt")
    assert found and found[0].id == "dynamodb-ttl"
    assert recipes.match("bake a cake") == []


def test_bench_counts_ground_truth_not_agent_opinion(settings: Settings) -> None:
    good = recipes.get("logs-group-retention")
    assert good.reference is not None
    # A plan that "succeeds" but forgets the retention step must count as a failure.
    lazy = good.reference.model_copy(update={"steps": good.reference.steps[:1]})
    scripts = iter([good.reference.model_dump_json(), lazy.model_dump_json()])
    report = run_bench([good], lambda: FakeProvider([next(scripts)]), settings, runs=2)
    assert [t.success for t in report.trials] == [True, False]
    assert report.trials[1].status == "succeeded"
    assert "ground-truth" in report.trials[1].failure
    text = to_markdown(report, {good.id: good.baseline_minutes})
    assert "1 of 2" in text and "1/2" in text
