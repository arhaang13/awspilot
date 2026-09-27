from __future__ import annotations

import json

import pytest

from awspilot.config import Settings
from awspilot.models import Plan, Risk
from awspilot.safety import review
from awspilot.safety.classify import classify
from tests.helpers import call, plan, step

SETTINGS = Settings(target="moto", endpoint_url=None)


def check(*steps: dict) -> list[str]:  # type: ignore[type-arg]
    report = review(Plan.model_validate_json(plan(*steps)), SETTINGS)
    return [f.rule for f in report.blocked]


@pytest.mark.parametrize(
    ("operation", "risk"),
    [
        ("describe_instances", Risk.READ),
        ("get_bucket_policy", Risk.READ),
        ("create_bucket", Risk.CREATE),
        ("run_instances", Risk.CREATE),
        ("put_bucket_versioning", Risk.MODIFY),
        ("update_table", Risk.MODIFY),
        ("some_new_operation", Risk.MODIFY),
        ("delete_bucket", Risk.DESTROY),
        ("terminate_instances", Risk.DESTROY),
    ],
)
def test_classify(operation: str, risk: Risk) -> None:
    assert classify(operation) == risk


def test_clean_plan_passes() -> None:
    assert check(step("a", call("s3", "create_bucket", Bucket="b"))) == []


def test_blocks_admin_policy() -> None:
    arn = "arn:aws:iam::aws:policy/AdministratorAccess"
    rules = check(step("a", call("iam", "attach_role_policy", RoleName="r", PolicyArn=arn)))
    assert rules == ["iam-admin-policy"]


@pytest.mark.parametrize("as_string", [True, False])
def test_blocks_wildcard_policy(as_string: bool) -> None:
    doc = {"Version": "2012-10-17",
           "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
    value = json.dumps(doc) if as_string else doc
    rules = check(step("a", call("iam", "create_policy", PolicyName="p", PolicyDocument=value)))
    assert rules == ["iam-wildcard-policy"]


def test_allows_scoped_policy() -> None:
    doc = json.dumps({"Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"],
                                     "Resource": "arn:aws:s3:::b/*"}]})
    assert check(step("a", call("iam", "create_policy", PolicyName="p", PolicyDocument=doc))) == []


def test_blocks_public_acl() -> None:
    assert check(step("a", call("s3", "create_bucket", Bucket="b", ACL="public-read"))) == [
        "s3-public-access"
    ]


def test_blocks_weakened_public_access_block() -> None:
    cfg = {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": False,
           "RestrictPublicBuckets": True}
    rules = check(step("a", call("s3", "put_public_access_block", Bucket="b",
                                 PublicAccessBlockConfiguration=cfg)))
    assert rules == ["s3-public-access"]


def test_blocks_public_bucket_policy() -> None:
    doc = json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*",
                                     "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"}]})
    assert check(step("a", call("s3", "put_bucket_policy", Bucket="b", Policy=doc))) == [
        "s3-public-access"
    ]


def test_blocks_ssh_from_anywhere() -> None:
    perms = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
              "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    rules = check(step("a", call("ec2", "authorize_security_group_ingress", GroupId="sg-1",
                                 IpPermissions=perms)))
    assert rules == ["ec2-open-admin-port"]


def test_allows_https_from_anywhere() -> None:
    perms = [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
              "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    assert check(step("a", call("ec2", "authorize_security_group_ingress", GroupId="sg-1",
                                IpPermissions=perms))) == []


def test_blocks_service_outside_allowlist() -> None:
    assert check(step("a", call("organizations", "create_account", Email="a@b.c",
                                AccountName="x"))) == ["service-allowlist", "account-level"]


def test_blocks_other_region() -> None:
    cfg = {"LocationConstraint": "eu-west-1"}
    assert check(step("a", call("s3", "create_bucket", Bucket="b",
                                CreateBucketConfiguration=cfg))) == ["region-allowlist"]


def test_blocks_verify_that_changes_state() -> None:
    bad = step("a", call("s3", "create_bucket", Bucket="b"),
               verify=[{"call": call("s3", "delete_bucket", Bucket="b"), "expect": []}])
    assert check(bad) == ["verify-read-only"]


def test_rollback_is_checked_too() -> None:
    bad = step("a", call("s3", "create_bucket", Bucket="b"),
               rollback=call("s3", "put_bucket_acl", Bucket="b", ACL="public-read"))
    assert check(bad) == ["s3-public-access"]
