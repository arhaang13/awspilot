"""The pipeline stages as plain functions. `graph.py` wires them together."""

from __future__ import annotations

import json
from typing import Any

import jmespath

from awspilot import recipes
from awspilot.audit import AuditLog
from awspilot.aws.caller import AwsCaller, CallError, has_templates, referenced_steps, resolve
from awspilot.llm import LLMProvider
from awspilot.models import Call, Check, CheckResult, Expect, Plan, Step, StepResult

PLANNER_SYSTEM = """\
You plan AWS workflows. Turn the user's request into an ordered list of AWS API calls.

Each step makes exactly one call:
- "service" is the boto3 client name (for example "s3", "dynamodb", "sqs").
- "operation" is the boto3 method name in snake_case (for example "create_bucket").
- "params" are the keyword arguments for that method, using boto3's exact casing.

Rules:
- Use the fewest steps that fully satisfy the request. Do not add resources nobody asked for.
- To use a value returned by an earlier step, write ${steps.<step_id>.<JMESPath>} inside a
  param value, for example ${steps.create_queue.QueueUrl}. List that step in "depends_on".
- Give every step that creates something a "rollback" call that deletes it.
- Give every step that changes state at least one "verify" check. A check is a read-only
  call (describe_*, get_*, list_*, head_*) plus "expect" assertions on its response.
  Each assertion has a JMESPath "path" and one of "equals", "contains" or "exists".
- Policy documents passed as JSON strings must be valid JSON.
- Follow least privilege. Never make anything public, never grant wildcard or administrator
  permissions, and never open SSH or RDP to the internet. Those plans are rejected.
- Step ids are short snake_case names, unique within the plan.
"""

REPAIR_SYSTEM = """\
You repair one failed step of an AWS workflow plan. You are given the plan, the step that
failed, and the error. Return a corrected version of that step only, keeping the same "id".
Fix the actual cause of the error. Do not widen permissions or make resources public to get
around it. The format and rules of a step are unchanged:
- one boto3 call per step, snake_case operation, boto3 casing for params
- ${steps.<step_id>.<JMESPath>} refers to an earlier step's response
- include "rollback" for anything created and read-only "verify" checks
"""


def make_plan(llm: LLMProvider, request: str, feedback: str = "") -> Plan:
    hints = recipes.match(request)
    user = f"Request: {request}"
    if hints:
        notes = "\n".join(f"- {line}" for r in hints for line in r.guidance)
        if notes:
            user += f"\n\nNotes from similar workflows:\n{notes}"
    if feedback:
        user += f"\n\nYour previous plan was rejected:\n{feedback}\nProduce a corrected plan."
    return llm.complete_json(PLANNER_SYSTEM, user, Plan)


def repair_step(llm: LLMProvider, plan: Plan, step: Step, error: str) -> Step:
    user = (
        f"Plan:\n{plan.model_dump_json(indent=1)}\n\n"
        f"Failed step id: {step.id}\n\nError:\n{error}"
    )
    fixed = llm.complete_json(REPAIR_SYSTEM, user, Step)
    return fixed.model_copy(update={"id": step.id})


def dry_run(plan: Plan, caller: AwsCaller) -> list[str]:
    """Validate every call without changing anything. Returns a list of problems."""
    problems: list[str] = []
    known: set[str] = set()
    for step in plan.steps:
        calls = [("call", step.call)]
        if step.rollback:
            calls.append(("rollback", step.rollback))
        calls += [("verify", c.call) for c in step.verify]
        for kind, call in calls:
            refs = referenced_steps(call.params)
            allowed = known | ({step.id} if kind != "call" else set())
            for ref in sorted(refs - allowed):
                problems.append(f"{step.id} ({kind}): refers to step {ref!r}, which runs later "
                                "or does not exist")
            try:
                if has_templates(call.params):
                    caller.validate_shape(call)
                else:
                    caller.validate(call)
            except CallError as exc:
                problems.append(f"{step.id} ({kind}): {exc}")
        # Emulators may ignore DryRun and really create the resource, so only real AWS.
        if caller.settings.is_real_aws and not has_templates(step.call.params):
            problem = _service_dry_run(step, caller)
            if problem:
                problems.append(problem)
        known.add(step.id)
    return problems


def _service_dry_run(step: Step, caller: AwsCaller) -> str | None:
    """Use the service's own DryRun flag where the API has one (mostly EC2)."""
    try:
        if not caller.supports_dry_run(step.call):
            return None
        probe = step.call.model_copy(update={"params": {**step.call.params, "DryRun": True}})
        caller.invoke(probe)
    except CallError as exc:
        # DryRunOperation means "this would have succeeded".
        if exc.code == "DryRunOperation":
            return None
        # Missing dependencies are expected: earlier steps have not run yet.
        if "NotFound" in exc.code:
            return None
        return f"{step.id} (dry run): {exc}"
    return None


def run_step(
    step: Step, outputs: dict[str, dict[str, Any]], caller: AwsCaller, audit: AuditLog
) -> StepResult:
    try:
        call = Call(
            service=step.call.service,
            operation=step.call.operation,
            params=resolve(step.call.params, outputs),
        )
        response = caller.invoke(call)
    except CallError as exc:
        audit.record("call", step_id=step.id, call=step.call.model_dump(), ok=False,
                     error=str(exc))
        return StepResult(step_id=step.id, ok=False, error=str(exc))
    rollback = None
    if step.rollback:
        try:
            scope = {**outputs, step.id: response}
            rollback = Call(
                service=step.rollback.service,
                operation=step.rollback.operation,
                params=resolve(step.rollback.params, scope),
            ).model_dump()
        except CallError:
            rollback = None
    # The resolved rollback is stored so `cleanup` can undo a run from the log alone.
    audit.record("call", step_id=step.id, call=call.model_dump(), ok=True, rollback=rollback)
    return StepResult(step_id=step.id, ok=True, response=response, rollback=rollback)


def evaluate(check: Check, outputs: dict[str, dict[str, Any]], caller: AwsCaller,
             step_id: str = "") -> CheckResult:
    """Run a check. If it has an id, its response is added to `outputs`."""
    label = check.description or f"{check.call.service}.{check.call.operation}"
    try:
        call = Call(
            service=check.call.service,
            operation=check.call.operation,
            params=resolve(check.call.params, outputs),
        )
        response = caller.invoke(call)
    except CallError as exc:
        return CheckResult(step_id=step_id, description=label, ok=False, detail=str(exc))
    if check.id:
        outputs[check.id] = response
    for expect in check.expect:
        problem = _assert(expect, response)
        if problem:
            return CheckResult(step_id=step_id, description=label, ok=False, detail=problem)
    return CheckResult(step_id=step_id, description=label, ok=True)


def _assert(expect: Expect, response: dict[str, Any]) -> str | None:
    try:
        found = jmespath.search(expect.path, response)
    except jmespath.exceptions.JMESPathError as exc:
        return f"bad path {expect.path!r}: {exc}"
    if expect.exists is not None and (found is not None) != expect.exists:
        return f"{expect.path}: expected {'a value' if expect.exists else 'nothing'}, got {found!r}"
    if expect.equals is not None and found != expect.equals:
        return f"{expect.path}: expected {expect.equals!r}, got {found!r}"
    if expect.contains is not None:
        haystack = found if isinstance(found, list | str | dict) else []
        if isinstance(found, str) and not isinstance(expect.contains, str):
            haystack = json.dumps(found)
        if expect.contains not in haystack:
            return f"{expect.path}: expected to contain {expect.contains!r}, got {found!r}"
    return None


def roll_back(calls: list[dict[str, Any]], caller: AwsCaller, audit: AuditLog) -> list[str]:
    """Run resolved rollback calls, newest first. Returns failures; never raises."""
    failures: list[str] = []
    for raw in reversed(calls):
        call = Call.model_validate(raw)
        try:
            caller.invoke(call)
            audit.record("rollback", call=raw, ok=True)
        except CallError as exc:
            audit.record("rollback", call=raw, ok=False, error=str(exc))
            failures.append(str(exc))
    return failures

__all__ = [
    "dry_run",
    "evaluate",
    "make_plan",
    "repair_step",
    "resolve",
    "roll_back",
    "run_step",
]
