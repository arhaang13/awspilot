"""Rules that block a plan before anything is executed."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from awspilot.config import Settings
from awspilot.models import Call, Finding, Plan, SafetyReport
from awspilot.safety.classify import classify

Rule = Callable[[Call], str | None]

_ACCOUNT_LEVEL = {"organizations", "account", "billing", "sso-admin", "identitystore"}
_PUBLIC_ACLS = {"public-read", "public-read-write", "authenticated-read"}
_ADMIN_PORTS = {22, 3389}
_OPEN_CIDRS = {"0.0.0.0/0", "::/0"}


def _as_doc(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def _statements(doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    stmts = doc.get("Statement", [])
    for stmt in stmts if isinstance(stmts, list) else [stmts]:
        if isinstance(stmt, dict):
            yield stmt


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def admin_managed_policy(call: Call) -> str | None:
    arn = str(call.params.get("PolicyArn", ""))
    if call.service == "iam" and arn.endswith(("/AdministratorAccess", "/IAMFullAccess")):
        return f"attaches the {arn.rsplit('/', 1)[-1]} managed policy"
    return None


def wildcard_policy(call: Call) -> str | None:
    if call.service != "iam":
        return None
    doc = _as_doc(call.params.get("PolicyDocument"))
    if doc is None:
        return None
    for stmt in _statements(doc):
        if (
            stmt.get("Effect") == "Allow"
            and "*" in _listed(stmt.get("Action"))
            and "*" in _listed(stmt.get("Resource"))
        ):
            return "grants Action '*' on Resource '*'"
    return None


def public_bucket(call: Call) -> str | None:
    if call.service != "s3":
        return None
    if call.params.get("ACL") in _PUBLIC_ACLS:
        return f"sets the public ACL {call.params['ACL']!r}"
    block = call.params.get("PublicAccessBlockConfiguration")
    if isinstance(block, dict) and not all(block.values()):
        return "turns off part of the bucket's public access block"
    if call.operation == "delete_public_access_block":
        return "removes the bucket's public access block"
    doc = _as_doc(call.params.get("Policy"))
    if doc is not None:
        for stmt in _statements(doc):
            principal = stmt.get("Principal")
            anyone = principal == "*" or (
                isinstance(principal, dict) and "*" in _listed(principal.get("AWS"))
            )
            if stmt.get("Effect") == "Allow" and anyone and not stmt.get("Condition"):
                return "bucket policy allows any principal"
    return None


def open_admin_port(call: Call) -> str | None:
    if call.service != "ec2" or call.operation != "authorize_security_group_ingress":
        return None
    perms = list(call.params.get("IpPermissions", []))
    if "CidrIp" in call.params or "CidrIpv6" in call.params:
        perms.append(
            {
                "FromPort": call.params.get("FromPort"),
                "ToPort": call.params.get("ToPort"),
                "IpProtocol": call.params.get("IpProtocol"),
                "IpRanges": [{"CidrIp": call.params.get("CidrIp")}],
                "Ipv6Ranges": [{"CidrIpv6": call.params.get("CidrIpv6")}],
            }
        )
    for perm in perms:
        cidrs = {r.get("CidrIp") for r in perm.get("IpRanges", [])}
        cidrs |= {r.get("CidrIpv6") for r in perm.get("Ipv6Ranges", [])}
        if not cidrs & _OPEN_CIDRS:
            continue
        if str(perm.get("IpProtocol")) == "-1":
            return "opens every port to the internet"
        lo, hi = perm.get("FromPort"), perm.get("ToPort")
        if isinstance(lo, int) and isinstance(hi, int):
            for port in _ADMIN_PORTS:
                if lo <= port <= hi:
                    return f"opens port {port} to the internet"
    return None


def account_level(call: Call) -> str | None:
    if call.service in _ACCOUNT_LEVEL:
        return f"{call.service} is an account-level service"
    if call.service == "iam" and call.operation in {
        "delete_account_password_policy",
        "delete_account_alias",
        "create_account_alias",
    }:
        return "changes account-wide IAM settings"
    return None


RULES: dict[str, Rule] = {
    "iam-admin-policy": admin_managed_policy,
    "iam-wildcard-policy": wildcard_policy,
    "s3-public-access": public_bucket,
    "ec2-open-admin-port": open_admin_port,
    "account-level": account_level,
}


def review(plan: Plan, settings: Settings) -> SafetyReport:
    report = SafetyReport()
    for step in plan.steps:
        calls = [step.call] + ([step.rollback] if step.rollback else [])
        report.risks[step.id] = classify(step.call.operation)

        if step.call.service not in settings.allowed_services:
            report.blocked.append(
                Finding(
                    step_id=step.id,
                    rule="service-allowlist",
                    message=f"service {step.call.service!r} is not in the allowed list",
                )
            )
        region = _region_of(step.call)
        if region and region not in settings.allowed_regions:
            report.blocked.append(
                Finding(
                    step_id=step.id,
                    rule="region-allowlist",
                    message=f"targets region {region!r}, which is not allowed",
                )
            )
        for call in calls:
            for name, rule in RULES.items():
                message = rule(call)
                if message:
                    report.blocked.append(Finding(step_id=step.id, rule=name, message=message))
        for check in step.verify:
            if classify(check.call.operation) != "read":
                report.blocked.append(
                    Finding(
                        step_id=step.id,
                        rule="verify-read-only",
                        message=f"verification uses {check.call.operation}, which is not read-only",
                    )
                )
    return report


def _region_of(call: Call) -> str | None:
    cfg = call.params.get("CreateBucketConfiguration")
    if isinstance(cfg, dict) and cfg.get("LocationConstraint"):
        return str(cfg["LocationConstraint"])
    return None
