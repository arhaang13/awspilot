from __future__ import annotations

import pytest

from awspilot.aws.caller import AwsCaller, CallError, has_templates, referenced_steps, resolve
from awspilot.models import Call


def test_rejects_unknown_operation(aws: AwsCaller) -> None:
    with pytest.raises(CallError, match="no operation"):
        aws.validate(Call(service="s3", operation="make_bucket"))


def test_rejects_unknown_service(aws: AwsCaller) -> None:
    with pytest.raises(CallError, match="unknown AWS service"):
        aws.validate(Call(service="s4", operation="create_bucket"))


def test_rejects_bad_params(aws: AwsCaller) -> None:
    with pytest.raises(CallError, match="Bucket"):
        aws.validate(Call(service="s3", operation="create_bucket", params={"Name": "b"}))


def test_shape_check_allows_templates(aws: AwsCaller) -> None:
    call = Call(service="sqs", operation="delete_queue",
                params={"QueueUrl": "${steps.q.QueueUrl}"})
    aws.validate_shape(call)
    with pytest.raises(CallError, match="missing required"):
        aws.validate_shape(Call(service="sqs", operation="delete_queue"))


def test_invoke_round_trip(aws: AwsCaller) -> None:
    aws.invoke(Call(service="s3", operation="create_bucket", params={"Bucket": "bucket-one"}))
    listed = aws.invoke(Call(service="s3", operation="list_buckets"))
    assert [b["Name"] for b in listed["Buckets"]] == ["bucket-one"]
    assert isinstance(listed["Buckets"][0]["CreationDate"], str)
    assert "ResponseMetadata" not in listed


def test_service_error_is_reported(aws: AwsCaller) -> None:
    with pytest.raises(CallError) as err:
        aws.invoke(Call(service="s3", operation="head_bucket", params={"Bucket": "missing"}))
    assert err.value.code == "404"


def test_resolve_keeps_types_and_interpolates() -> None:
    outputs = {"a": {"Count": 3, "Items": [{"Arn": "arn:x"}]}}
    assert resolve("${steps.a.Count}", outputs) == 3
    assert resolve({"k": ["${steps.a.Items[0].Arn}"]}, outputs) == {"k": ["arn:x"]}
    assert resolve('{"t":"${steps.a.Items[0].Arn}"}', outputs) == '{"t":"arn:x"}'


def test_resolve_errors() -> None:
    with pytest.raises(CallError, match="has not run"):
        resolve("${steps.nope.X}", {})
    with pytest.raises(CallError, match="nothing at"):
        resolve("${steps.a.Missing}", {"a": {}})


def test_template_detection() -> None:
    value = {"a": ["${steps.one.X}", {"b": "pre-${steps.two.Y}"}], "c": 1}
    assert has_templates(value)
    assert referenced_steps(value) == {"one", "two"}
    assert not has_templates({"a": "plain"})
