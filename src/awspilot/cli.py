"""Command line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from awspilot import recipes as recipe_lib
from awspilot.config import Settings
from awspilot.llm import LLMError, get_provider
from awspilot.models import Plan, Risk, SafetyReport
from awspilot.runner import RunResult, cleanup, run

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Agentic AWS workflow automation.")
console = Console()

_COLORS = {Risk.READ: "green", Risk.CREATE: "cyan", Risk.MODIFY: "yellow", Risk.DESTROY: "red"}


def _show_plan(plan: Plan, safety: SafetyReport | None) -> None:
    table = Table(title=plan.summary, title_justify="left", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Step")
    table.add_column("Call")
    table.add_column("Risk")
    for i, step in enumerate(plan.steps, 1):
        risk = safety.risks.get(step.id) if safety else None
        table.add_row(
            str(i),
            f"[bold]{step.id}[/bold]\n{step.description}",
            f"{step.call.service}.{step.call.operation}\n"
            f"[dim]{json.dumps(step.call.params, default=str)[:200]}[/dim]",
            f"[{_COLORS[risk]}]{risk.value}[/]" if risk else "",
        )
    console.print(table)


def _show_result(result: RunResult) -> None:
    for check in result.checks:
        mark = "[green]pass[/]" if check.ok else "[red]FAIL[/]"
        console.print(f"  {mark}  {check.step_id}: {check.description} {check.detail}")
    color = "green" if result.ok else "yellow" if result.status == "planned" else "red"
    console.print(f"\n[{color}]{result.status}[/] in {result.seconds:.1f}s, "
                  f"{result.usage.calls} model calls, {result.repairs} repairs. "
                  f"Run id: {result.run_id}")
    if result.error:
        console.print(f"[red]{result.error}[/]")


def _approve(plan: Plan, safety: SafetyReport) -> bool:
    _show_plan(plan, safety)
    return typer.confirm("Run this plan?", default=False)


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc


@app.command()
def plan(request: str) -> None:
    """Show the plan for a request. Changes nothing."""
    settings = _settings()
    try:
        result = run(request, get_provider(settings), settings, plan_only=True)
    except LLMError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    if result.plan:
        _show_plan(result.plan, result.safety)
    _show_result(result)
    raise typer.Exit(0 if result.status == "planned" else 1)


@app.command(name="run")
def run_cmd(
    request: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the approval prompt. On real "
                   "AWS this only applies to plans that create or read.")] = False,
) -> None:
    """Plan a request, ask for approval, run it, and verify the result."""
    settings = _settings()
    console.print(f"Target: [bold]{settings.target}[/] ({settings.region})")
    try:
        result = run(request, get_provider(settings), settings, approver=_approve,
                     auto_approve=yes)
    except LLMError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    if yes and result.plan:
        _show_plan(result.plan, result.safety)
    _show_result(result)
    raise typer.Exit(0 if result.ok else 1)


@app.command(name="cleanup")
def cleanup_cmd(run_id: str) -> None:
    """Delete what a run created, using its audit log."""
    settings = _settings()
    try:
        failures = cleanup(run_id, settings)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    for failure in failures:
        console.print(f"[red]{failure}[/]")
    console.print("Cleanup finished." if not failures else "Cleanup finished with errors.")
    raise typer.Exit(1 if failures else 0)


@app.command(name="recipes")
def recipes_cmd() -> None:
    """List the workflow recipes."""
    table = Table()
    table.add_column("Id")
    table.add_column("Area")
    table.add_column("Request")
    for recipe in recipe_lib.load_all():
        table.add_row(recipe.id, recipe.area, recipe.request)
    console.print(table)
    console.print(f"{len(recipe_lib.load_all())} recipes")


@app.command()
def demo(recipe_id: Annotated[str, typer.Argument(help="See `awspilot recipes`.")]) -> None:
    """Run a recipe's reference plan on an in-process fake AWS. Needs no keys or Docker.

    No model is involved: this shows the pipeline (safety, approval, execution,
    verification, cleanup), not the planner.
    """
    from dataclasses import replace

    from awspilot.aws.caller import AwsCaller
    from awspilot.bench.runner import clean_backend
    from awspilot.llm import FakeProvider

    try:
        recipe = recipe_lib.get(recipe_id)
    except KeyError as exc:
        console.print(f"[red]{exc.args[0]}[/]")
        raise typer.Exit(2) from exc
    if recipe.reference is None:
        console.print("[red]This recipe has no reference plan.[/]")
        raise typer.Exit(2)
    settings = replace(_settings(), target="moto", endpoint_url=None)
    console.print(f"[dim]Request:[/] {recipe.request}\n")
    with clean_backend(settings):
        caller = AwsCaller(settings)
        llm = FakeProvider([recipe.reference.model_dump_json()], model="reference-plan")
        result = run(recipe.request, llm, settings, caller=caller, approver=_approve)
        if result.ok:
            from awspilot import engine

            scope: dict[str, dict[str, object]] = {}
            result.checks = [engine.evaluate(c, scope, caller, "check") for c in recipe.checks]
            if not all(c.ok for c in result.checks):
                result.status = "unverified"
        _show_result(result)
        if result.ok:
            failures = cleanup(result.run_id, settings, caller=caller)
            console.print("Cleaned up." if not failures else f"[red]{failures}[/]")
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def bench(
    runs: Annotated[int, typer.Option(help="Runs per recipe.")] = 3,
    only: Annotated[list[str] | None, typer.Option(help="Recipe id; repeatable.")] = None,
    target: Annotated[str, typer.Option(help="localstack or moto")] = "localstack",
    out: Annotated[Path, typer.Option(help="Results directory.")] = Path("bench/results"),
    report: Annotated[Path, typer.Option(help="Markdown report.")] = Path("docs/benchmark.md"),
) -> None:
    """Run the recipes and write measured results."""
    from awspilot.bench.runner import run_bench, save, to_markdown, with_target

    if target not in {"localstack", "moto"}:
        console.print("[red]The benchmark never runs against real AWS.[/]")
        raise typer.Exit(2)
    settings = with_target(_settings(), target)
    chosen = [r for r in recipe_lib.load_all() if not only or r.id in only]
    if not chosen:
        console.print("[red]No recipes matched.[/]")
        raise typer.Exit(2)

    def show(trial: object) -> None:
        from awspilot.bench.runner import Trial

        assert isinstance(trial, Trial)
        mark = "[green]pass[/]" if trial.success else "[red]FAIL[/]"
        console.print(f"{mark} {trial.recipe} #{trial.attempt} {trial.seconds:.1f}s "
                      f"{trial.failure}")

    try:
        result = run_bench(chosen, lambda: get_provider(settings), settings, runs, show)
    except LLMError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    path = save(result, out)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        to_markdown(result, {r.id: r.baseline_minutes for r in chosen}), encoding="utf-8"
    )
    console.print(f"\n{sum(t.success for t in result.trials)}/{len(result.trials)} trials passed "
                  f"({result.success_rate:.0%}). Saved {path} and {report}.")


if __name__ == "__main__":
    app()
