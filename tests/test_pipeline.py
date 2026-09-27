from __future__ import annotations

import json

from awspilot.audit import AuditLog
from awspilot.aws.caller import AwsCaller
from awspilot.config import Settings
from awspilot.llm import FakeProvider
from awspilot.models import Call
from awspilot.runner import cleanup, run
from tests.helpers import BUCKET, call, plan, step


def buckets(aws: AwsCaller) -> list[str]:
    return [b["Name"] for b in aws.invoke(Call(service="s3", operation="list_buckets"))["Buckets"]]


def test_happy_path(aws: AwsCaller, settings: Settings) -> None:
    result = run("make a bucket", FakeProvider([plan(BUCKET)]), settings, caller=aws,
                 auto_approve=True)
    assert result.status == "succeeded"
    assert [c.ok for c in result.checks] == [True]
    assert buckets(aws) == ["unit-test-bucket"]


def test_plan_only_changes_nothing(aws: AwsCaller, settings: Settings) -> None:
    result = run("make a bucket", FakeProvider([plan(BUCKET)]), settings, caller=aws,
                 plan_only=True)
    assert result.status == "planned"
    assert buckets(aws) == []


def test_rejection_changes_nothing(aws: AwsCaller, settings: Settings) -> None:
    seen = []
    result = run("make a bucket", FakeProvider([plan(BUCKET)]), settings, caller=aws,
                 approver=lambda p, s: bool(seen.append(p)))
    assert result.status == "rejected"
    assert len(seen) == 1
    assert buckets(aws) == []


def test_no_approver_means_no(aws: AwsCaller, settings: Settings) -> None:
    result = run("make a bucket", FakeProvider([plan(BUCKET)]), settings, caller=aws)
    assert result.status == "rejected"
    assert buckets(aws) == []


def test_unsafe_plan_is_blocked_before_approval(aws: AwsCaller, settings: Settings) -> None:
    public = step("create_bucket", call("s3", "create_bucket", Bucket="b", ACL="public-read"))
    asked = []
    result = run("public bucket", FakeProvider([plan(public)]), settings, caller=aws,
                 approver=lambda p, s: bool(asked.append(1)) or True)
    assert result.status == "blocked"
    assert "public" in result.error
    assert asked == []
    assert buckets(aws) == []


def test_invalid_plan_is_replanned(aws: AwsCaller, settings: Settings) -> None:
    broken = step("create_bucket", call("s3", "create_bucket", BucketName="b"))
    llm = FakeProvider([plan(broken), plan(BUCKET)])
    result = run("make a bucket", llm, settings, caller=aws, auto_approve=True)
    assert result.status == "succeeded"
    assert "rejected" in llm.prompts[1][0]["content"]


def test_failed_step_is_repaired(aws: AwsCaller, settings: Settings) -> None:
    # Valid shape, but the table does not exist, so the service rejects it.
    bad_ttl = step("ttl", call("dynamodb", "update_time_to_live", TableName="Missing",
                               TimeToLiveSpecification={"Enabled": True, "AttributeName": "e"}))
    fixed = step("ttl", call("s3", "put_bucket_versioning", Bucket="unit-test-bucket",
                             VersioningConfiguration={"Status": "Enabled"}))
    llm = FakeProvider([plan(BUCKET, bad_ttl), json.dumps(fixed)])
    result = run("x", llm, settings, caller=aws, auto_approve=True)
    assert result.status == "succeeded"
    assert result.repairs == 1
    # The bucket step ran once, not twice.
    events = AuditLog.read(settings.state_dir, result.run_id)
    created = [e for e in events if e["event"] == "call" and e["step_id"] == "create_bucket"]
    assert len(created) == 1


def test_unrepairable_failure_rolls_back(aws: AwsCaller, settings: Settings) -> None:
    bad = step("ttl", call("dynamodb", "update_time_to_live", TableName="Missing",
                           TimeToLiveSpecification={"Enabled": True, "AttributeName": "e"}))
    llm = FakeProvider([plan(BUCKET, bad), json.dumps(bad), json.dumps(bad)])
    result = run("x", llm, settings, caller=aws, auto_approve=True)
    assert result.status == "failed"
    assert "Missing" in result.error or "ResourceNotFound" in result.error
    assert buckets(aws) == []


def test_unsafe_repair_is_blocked_and_rolled_back(aws: AwsCaller, settings: Settings) -> None:
    bad = step("ttl", call("dynamodb", "update_time_to_live", TableName="Missing",
                           TimeToLiveSpecification={"Enabled": True, "AttributeName": "e"}))
    unsafe = step("ttl", call("s3", "put_bucket_acl", Bucket="unit-test-bucket",
                              ACL="public-read-write"))
    llm = FakeProvider([plan(BUCKET, bad), json.dumps(unsafe)])
    result = run("x", llm, settings, caller=aws, auto_approve=True)
    assert result.status in {"blocked", "failed"}
    assert not result.ok
    assert buckets(aws) == []


def test_failed_verification_is_not_success(aws: AwsCaller, settings: Settings) -> None:
    wrong = {**BUCKET, "verify": [{
        "call": call("s3", "get_bucket_versioning", Bucket="unit-test-bucket"),
        "expect": [{"path": "Status", "equals": "Enabled"}],
    }]}
    result = run("x", FakeProvider([plan(wrong)]), settings, caller=aws, auto_approve=True)
    assert result.status == "unverified"
    assert not result.ok


def test_real_aws_never_auto_approves_changes(aws: AwsCaller, settings: Settings) -> None:
    from dataclasses import replace

    real = replace(settings, target="aws")
    change = step("v", call("s3", "put_bucket_versioning", Bucket="b",
                            VersioningConfiguration={"Status": "Enabled"}))
    result = run("x", FakeProvider([plan(change)]), real, caller=aws, auto_approve=True)
    assert result.status == "rejected"


def test_cleanup_from_audit_log(aws: AwsCaller, settings: Settings) -> None:
    result = run("make a bucket", FakeProvider([plan(BUCKET)]), settings, caller=aws,
                 auto_approve=True)
    assert buckets(aws) == ["unit-test-bucket"]
    assert cleanup(result.run_id, settings, caller=aws) == []
    assert buckets(aws) == []
