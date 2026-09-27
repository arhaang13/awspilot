from __future__ import annotations

import json
from typing import Any


def call(service: str, operation: str, **params: Any) -> dict[str, Any]:
    return {"service": service, "operation": operation, "params": params}


def plan(*steps: dict[str, Any], summary: str = "test plan") -> str:
    return json.dumps({"summary": summary, "steps": list(steps)})


def step(id: str, c: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": id, "description": id, "call": c, **extra}


BUCKET = step(
    "create_bucket",
    call("s3", "create_bucket", Bucket="unit-test-bucket"),
    rollback=call("s3", "delete_bucket", Bucket="unit-test-bucket"),
    verify=[{"call": call("s3", "head_bucket", Bucket="unit-test-bucket"), "expect": []}],
)
