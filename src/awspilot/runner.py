"""Run one request through the graph."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from awspilot.audit import AuditLog
from awspilot.aws.caller import AwsCaller
from awspilot.config import Settings
from awspilot.graph import build_graph
from awspilot.llm import LLMProvider
from awspilot.models import CheckResult, Plan, SafetyReport, Usage

Approver = Callable[[Plan, SafetyReport], bool]


@dataclass
class RunResult:
    run_id: str
    status: str
    error: str = ""
    plan: Plan | None = None
    safety: SafetyReport | None = None
    checks: list[CheckResult] = field(default_factory=list)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    repairs: int = 0
    seconds: float = 0.0
    usage: Usage = field(default_factory=Usage)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def run(
    request: str,
    llm: LLMProvider,
    settings: Settings,
    *,
    caller: AwsCaller | None = None,
    approver: Approver | None = None,
    auto_approve: bool = False,
    plan_only: bool = False,
) -> RunResult:
    run_id = new_run_id()
    audit = AuditLog(settings.state_dir, run_id)
    audit.record("request", request=request, target=settings.target, provider=llm.name,
                 model=llm.model)
    graph = build_graph(llm, caller or AwsCaller(settings), settings, audit)
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 60}

    started = time.perf_counter()
    payload: Any = {"request": request, "auto_approve": auto_approve, "plan_only": plan_only}
    while True:
        state = graph.invoke(payload, config)
        pending = state.get("__interrupt__")
        if not pending:
            break
        ask = pending[0].value
        decision = bool(approver) and approver(  # type: ignore[misc]
            Plan.model_validate(ask["plan"]), SafetyReport.model_validate(ask["safety"])
        )
        payload = Command(resume=decision)

    result = RunResult(
        run_id=run_id,
        status=state.get("status", "failed"),
        error=state.get("error", ""),
        plan=Plan.model_validate(state["plan"]) if state.get("plan") else None,
        safety=SafetyReport.model_validate(state["safety"]) if state.get("safety") else None,
        checks=[CheckResult.model_validate(c) for c in state.get("checks", [])],
        outputs=state.get("outputs", {}),
        repairs=state.get("repairs", 0),
        seconds=time.perf_counter() - started,
        usage=llm.usage,
    )
    audit.record("result", status=result.status, error=result.error,
                 seconds=round(result.seconds, 3))
    return result


def cleanup(run_id: str, settings: Settings, caller: AwsCaller | None = None) -> list[str]:
    """Undo a finished run using the rollback calls recorded in its audit log."""
    from awspilot.engine import roll_back

    events = AuditLog.read(settings.state_dir, run_id)
    calls = [e["rollback"] for e in events
             if e["event"] == "call" and e.get("ok") and e.get("rollback")]
    audit = AuditLog(settings.state_dir, run_id)
    return roll_back(calls, caller or AwsCaller(settings), audit)
