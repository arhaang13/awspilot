"""LangGraph state machine: plan, check, approve, execute, verify."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from awspilot import engine
from awspilot.audit import AuditLog
from awspilot.aws.caller import AwsCaller
from awspilot.config import Settings
from awspilot.llm import LLMError, LLMProvider
from awspilot.models import Plan, Risk
from awspilot.safety import review

END: Literal["__end__"] = "__end__"


class RunState(TypedDict, total=False):
    request: str
    plan_only: bool
    auto_approve: bool
    plan: dict[str, Any]
    safety: dict[str, Any]
    problems: list[str]
    approved: bool
    needs_approval: bool
    outputs: dict[str, dict[str, Any]]
    rollbacks: list[dict[str, Any]]
    done: list[str]
    failed_step: str
    error: str
    repairs: int
    checks: list[dict[str, Any]]
    status: str


def build_graph(llm: LLMProvider, caller: AwsCaller, settings: Settings, audit: AuditLog) -> Any:
    def plan_node(state: RunState) -> RunState:
        feedback = "\n".join(state.get("problems", []))
        try:
            plan = engine.make_plan(llm, state["request"], feedback)
        except LLMError as exc:
            return {"status": "failed", "error": str(exc)}
        audit.record("plan", plan=plan.model_dump(mode="json"))
        return {"plan": plan.model_dump(mode="json"), "problems": [], "needs_approval": True}

    def safety_node(state: RunState) -> RunState:
        report = review(Plan.model_validate(state["plan"]), settings)
        audit.record("safety", report=report.model_dump(mode="json"))
        if not report.ok:
            reasons = "; ".join(f"{f.step_id}: {f.message}" for f in report.blocked)
            return {"safety": report.model_dump(mode="json"), "status": "blocked", "error": reasons}
        return {"safety": report.model_dump(mode="json")}

    def dry_run_node(state: RunState) -> RunState:
        problems = engine.dry_run(Plan.model_validate(state["plan"]), caller)
        audit.record("dry_run", problems=problems)
        if not problems:
            return {"problems": []}
        repairs = state.get("repairs", 0)
        if repairs >= settings.max_repairs:
            return {"problems": problems, "status": "failed",
                    "error": "plan is invalid: " + "; ".join(problems)}
        return {"problems": problems, "repairs": repairs + 1}

    def approve_node(state: RunState) -> RunState:
        if state.get("plan_only"):
            return {"status": "planned"}
        risks = set(state["safety"]["risks"].values())
        risky = bool(risks & {Risk.MODIFY.value, Risk.DESTROY.value})
        if state.get("auto_approve") and not (settings.is_real_aws and risky):
            audit.record("approval", approved=True, by="auto")
            return {"approved": True, "needs_approval": False}
        answer = interrupt({"plan": state["plan"], "safety": state["safety"]})
        approved = bool(answer)
        audit.record("approval", approved=approved, by="user")
        if not approved:
            return {"approved": False, "status": "rejected"}
        return {"approved": True, "needs_approval": False}

    def execute_node(state: RunState) -> RunState:
        plan = Plan.model_validate(state["plan"])
        outputs = dict(state.get("outputs", {}))
        rollbacks = list(state.get("rollbacks", []))
        done = list(state.get("done", []))
        for step in plan.steps:
            if step.id in done:
                continue
            result = engine.run_step(step, outputs, caller, audit)
            if not result.ok:
                return {"outputs": outputs, "rollbacks": rollbacks, "done": done,
                        "failed_step": step.id, "error": result.error or ""}
            outputs[step.id] = result.response
            done.append(step.id)
            if result.rollback:
                rollbacks.append(result.rollback)
        return {"outputs": outputs, "rollbacks": rollbacks, "done": done,
                "failed_step": "", "error": ""}

    def repair_node(state: RunState) -> RunState:
        plan = Plan.model_validate(state["plan"])
        failed = next(s for s in plan.steps if s.id == state["failed_step"])
        try:
            fixed = engine.repair_step(llm, plan, failed, state["error"])
        except LLMError as exc:
            return {"status": "failed", "error": f"{state['error']} (repair failed: {exc})"}
        steps = [fixed if s.id == fixed.id else s for s in plan.steps]
        try:
            revised = Plan(summary=plan.summary, steps=steps)
        except ValueError as exc:
            return {"status": "failed", "error": f"repair produced an invalid plan: {exc}"}
        audit.record("repair", step_id=fixed.id, error=state["error"],
                     step=fixed.model_dump(mode="json"))
        changed = fixed.call != failed.call
        return {"plan": revised.model_dump(mode="json"), "repairs": state.get("repairs", 0) + 1,
                # A changed call on a real account goes back to the user.
                "needs_approval": changed and settings.is_real_aws}

    def rollback_node(state: RunState) -> RunState:
        failures = engine.roll_back(state.get("rollbacks", []), caller, audit)
        error = state.get("error", "")
        if failures:
            error += " | rollback incomplete: " + "; ".join(failures)
        return {"status": "failed", "error": error, "rollbacks": []}

    def verify_node(state: RunState) -> RunState:
        plan = Plan.model_validate(state["plan"])
        outputs = dict(state.get("outputs", {}))
        results = [
            engine.evaluate(check, outputs, caller, step.id)
            for step in plan.steps
            for check in step.verify
        ]
        audit.record("verify", checks=[r.model_dump(mode="json") for r in results])
        ok = all(r.ok for r in results)
        return {"checks": [r.model_dump(mode="json") for r in results],
                "status": "succeeded" if ok else "unverified",
                "error": "" if ok else "one or more verification checks failed"}

    def after_plan(state: RunState) -> Literal["safety", "__end__"]:
        return END if state.get("status") == "failed" else "safety"

    def after_safety(state: RunState) -> Literal["dry_run", "__end__", "rollback"]:
        if state.get("status") == "blocked":
            # A repair that fails the policy mid-run must undo what already ran.
            return "rollback" if state.get("done") else END
        return "dry_run"

    def after_dry_run(state: RunState) -> Literal["plan", "approve", "execute", "rollback",
                                                  "__end__"]:
        if state.get("status") == "failed":
            return "rollback" if state.get("done") else END
        if state.get("problems"):
            return "rollback" if state.get("done") else "plan"
        return "approve" if state.get("needs_approval", True) else "execute"

    def after_approve(state: RunState) -> Literal["execute", "rollback", "__end__"]:
        if state.get("approved") and not state.get("plan_only"):
            return "execute"
        return "rollback" if state.get("done") else END

    def after_execute(state: RunState) -> Literal["verify", "repair", "rollback"]:
        if not state.get("failed_step"):
            return "verify"
        return "repair" if state.get("repairs", 0) < settings.max_repairs else "rollback"

    def after_repair(state: RunState) -> Literal["safety", "rollback"]:
        return "rollback" if state.get("status") == "failed" else "safety"

    graph = StateGraph(RunState)
    graph.add_node("plan", plan_node)
    graph.add_node("safety", safety_node)
    graph.add_node("dry_run", dry_run_node)
    graph.add_node("approve", approve_node)
    graph.add_node("execute", execute_node)
    graph.add_node("repair", repair_node)
    graph.add_node("rollback", rollback_node)
    graph.add_node("verify", verify_node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", after_plan)
    graph.add_conditional_edges("safety", after_safety)
    graph.add_conditional_edges("dry_run", after_dry_run)
    graph.add_conditional_edges("approve", after_approve)
    graph.add_conditional_edges("execute", after_execute)
    graph.add_conditional_edges("repair", after_repair)
    graph.add_edge("rollback", END)
    graph.add_edge("verify", END)
    return graph.compile(checkpointer=MemorySaver())
