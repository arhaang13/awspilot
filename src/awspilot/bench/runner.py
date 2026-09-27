"""Run every recipe N times against a clean backend and record the outcome.

Success is decided by the recipe's own checks, which the planner never sees.
The agent's opinion of its own work does not count.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from awspilot import engine
from awspilot.aws.caller import AwsCaller
from awspilot.config import Settings
from awspilot.llm import LLMProvider
from awspilot.recipes import Recipe
from awspilot.runner import run


class Trial(BaseModel):
    recipe: str
    area: str
    attempt: int
    success: bool
    status: str
    failure: str = ""
    seconds: float
    steps: int
    repairs: int
    llm_calls: int
    input_tokens: int
    output_tokens: int


class BenchReport(BaseModel):
    date: str
    provider: str
    model: str
    target: str
    runs_per_recipe: int
    skipped: dict[str, str] = Field(default_factory=dict)
    trials: list[Trial] = Field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return sum(t.success for t in self.trials) / len(self.trials) if self.trials else 0.0


@contextlib.contextmanager
def clean_backend(settings: Settings) -> Iterator[None]:
    if settings.target == "moto":
        from moto import mock_aws

        with mock_aws(config={"iam": {"load_aws_managed_policies": True}}):
            yield
    elif settings.target == "localstack":
        req = urllib.request.Request(f"{settings.endpoint_url}/_localstack/state/reset",
                                     method="POST")
        urllib.request.urlopen(req, timeout=30).close()  # noqa: S310
        yield
    else:
        raise ValueError("the benchmark only runs against localstack or moto, never real AWS")


def run_bench(
    recipes: list[Recipe],
    make_llm: Callable[[], LLMProvider],
    settings: Settings,
    runs: int = 3,
    on_trial: Callable[[Trial], None] | None = None,
) -> BenchReport:
    report = BenchReport(
        date=dt.date.today().isoformat(), provider=settings.provider, model=settings.model,
        target=settings.target, runs_per_recipe=runs,
    )
    for recipe in recipes:
        if recipe.requires_real_aws:
            report.skipped[recipe.id] = recipe.requires_real_aws
            continue
        for attempt in range(1, runs + 1):
            llm = make_llm()
            report.provider, report.model = llm.name, llm.model
            with clean_backend(settings):
                trial = _trial(recipe, attempt, llm, settings)
            report.trials.append(trial)
            if on_trial:
                on_trial(trial)
    return report


def _trial(recipe: Recipe, attempt: int, llm: LLMProvider, settings: Settings) -> Trial:
    caller = AwsCaller(settings)
    result = run(recipe.request, llm, settings, caller=caller, auto_approve=True)
    failure = ""
    if not result.ok:
        failure = f"{result.status}: {result.error}"
    else:
        scope: dict[str, dict[str, Any]] = {}  # ground truth must not lean on the plan
        for check in recipe.checks:
            outcome = engine.evaluate(check, scope, caller)
            if not outcome.ok:
                failure = f"ground-truth check failed: {outcome.description}: {outcome.detail}"
                break
    return Trial(
        recipe=recipe.id, area=recipe.area, attempt=attempt, success=not failure,
        status=result.status, failure=failure, seconds=round(result.seconds, 2),
        steps=len(result.plan.steps) if result.plan else 0, repairs=result.repairs,
        llm_calls=result.usage.calls, input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )


def save(report: BenchReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.date}-{report.provider}-{report.model}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def to_markdown(report: BenchReport, baselines: dict[str, float | None]) -> str:
    by_recipe: dict[str, list[Trial]] = {}
    for trial in report.trials:
        by_recipe.setdefault(trial.recipe, []).append(trial)
    passed = sum(t.success for t in report.trials)
    lines = [
        "# Benchmark results",
        "",
        f"- Date: {report.date}",
        f"- Model: {report.model} ({report.provider})",
        f"- Target: {report.target}",
        f"- Runs per workflow: {report.runs_per_recipe}",
        f"- Workflows run: {len(by_recipe)}",
        f"- Trials passed: {passed} of {len(report.trials)} "
        f"({report.success_rate:.0%})",
        "",
        "A trial passes only if the recipe's own checks pass against the backend afterwards.",
        "Manual times are estimates for a practiced person using the console, not measurements.",
        "",
        "| Workflow | Area | Passed | Median time (s) | Manual estimate (min) | Repairs |",
        "|---|---|---|---|---|---|",
    ]
    for recipe_id, trials in sorted(by_recipe.items()):
        times = sorted(t.seconds for t in trials)
        median = times[len(times) // 2]
        manual = baselines.get(recipe_id)
        lines.append(
            f"| {recipe_id} | {trials[0].area} | {sum(t.success for t in trials)}/{len(trials)} "
            f"| {median:.1f} | {manual if manual is not None else '-'} "
            f"| {sum(t.repairs for t in trials)} |"
        )
    failures = [t for t in report.trials if not t.success]
    if failures:
        lines += ["", "## Failures", ""]
        lines += [f"- `{t.recipe}` run {t.attempt}: {t.failure}" for t in failures]
    if report.skipped:
        lines += ["", "## Skipped", ""]
        lines += [f"- `{rid}`: {why}" for rid, why in sorted(report.skipped.items())]
    return "\n".join(lines) + "\n"


def with_target(settings: Settings, target: str) -> Settings:
    from awspilot.config import LOCALSTACK_URL

    url = (settings.endpoint_url or LOCALSTACK_URL) if target == "localstack" else None
    return replace(settings, target=target, endpoint_url=url)


def load(path: Path) -> BenchReport:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    return BenchReport.model_validate(data)
