"""Typed plan and result models shared by every stage of the pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Risk(StrEnum):
    READ = "read"
    CREATE = "create"
    MODIFY = "modify"
    DESTROY = "destroy"


class Call(BaseModel):
    """One AWS API call: boto3 service name, snake_case operation, keyword params."""

    service: str
    operation: str
    params: dict[str, Any] = Field(default_factory=dict)


class Expect(BaseModel):
    """Assertion on a call's response. `path` is a JMESPath expression."""

    path: str
    equals: Any = None
    exists: bool | None = None
    contains: Any = None

    @model_validator(mode="after")
    def _one_assertion(self) -> Expect:
        if self.equals is None and self.exists is None and self.contains is None:
            self.exists = True
        return self


class Check(BaseModel):
    """A read-only call plus the assertions its response must satisfy."""

    # Optional name; later checks can read this response as ${steps.<id>...}.
    id: str | None = None
    description: str = ""
    call: Call
    expect: list[Expect] = Field(default_factory=list)


class Step(BaseModel):
    id: str
    description: str
    call: Call
    depends_on: list[str] = Field(default_factory=list)
    verify: list[Check] = Field(default_factory=list)
    rollback: Call | None = None


class Plan(BaseModel):
    summary: str
    steps: list[Step]

    @model_validator(mode="after")
    def _valid_graph(self) -> Plan:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id {step.id!r}")
            missing = [d for d in step.depends_on if d not in seen]
            if missing:
                raise ValueError(
                    f"step {step.id!r} depends on {missing}, which must appear earlier in the plan"
                )
            seen.add(step.id)
        return self


class Finding(BaseModel):
    step_id: str
    rule: str
    message: str


class SafetyReport(BaseModel):
    risks: dict[str, Risk] = Field(default_factory=dict)
    blocked: list[Finding] = Field(default_factory=list)
    warnings: list[Finding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked

    @property
    def highest(self) -> Risk:
        order = [Risk.READ, Risk.CREATE, Risk.MODIFY, Risk.DESTROY]
        return max(self.risks.values(), key=order.index, default=Risk.READ)


class StepResult(BaseModel):
    step_id: str
    ok: bool
    response: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Rollback call with references already resolved, ready to run as-is.
    rollback: dict[str, Any] | None = None


class CheckResult(BaseModel):
    step_id: str
    description: str
    ok: bool
    detail: str = ""


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
