"""The single tool the agent uses to touch AWS.

Every call is checked against botocore's own service model before it is sent,
so a hallucinated operation or parameter fails locally with a useful message
instead of as an opaque API error.
"""

from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache
from typing import Any

import boto3
import jmespath
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError
from botocore.model import OperationModel
from botocore.validate import validate_parameters

from awspilot.config import Settings
from awspilot.models import Call

# A fake in-process target still needs syntactically valid credentials.
_DUMMY_CREDS = {"aws_access_key_id": "test", "aws_secret_access_key": "test"}

TEMPLATE = re.compile(r"\$\{steps\.([A-Za-z0-9_\-]+)\.([^}]+)\}")


class CallError(RuntimeError):
    """An AWS call was invalid or was rejected by the service."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class AwsCaller:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            kwargs: dict[str, Any] = {"region_name": self.settings.region}
            if self.settings.endpoint_url:
                kwargs["endpoint_url"] = self.settings.endpoint_url
            if not self.settings.is_real_aws:
                kwargs.update(_DUMMY_CREDS)
            try:
                self._clients[service] = boto3.client(service, **kwargs)  # type: ignore[call-overload]
            except BotoCoreError as exc:
                raise CallError(f"unknown AWS service {service!r}: {exc}") from exc
        return self._clients[service]

    def operation_model(self, call: Call) -> OperationModel:
        client = self.client(call.service)
        api_name = client.meta.method_to_api_mapping.get(call.operation)
        if api_name is None:
            raise CallError(f"{call.service} has no operation {call.operation!r}")
        model: OperationModel = client.meta.service_model.operation_model(api_name)
        return model

    def validate(self, call: Call) -> None:
        """Validate operation and params offline. Params must be fully resolved."""
        model = self.operation_model(call)
        if model.input_shape is None:
            if call.params:
                raise CallError(f"{call.service}.{call.operation} takes no parameters")
            return
        try:
            validate_parameters(call.params, model.input_shape)
        except ParamValidationError as exc:
            raise CallError(f"{call.service}.{call.operation}: {exc}") from exc

    def validate_shape(self, call: Call) -> None:
        """Looser check for calls that still contain ${steps...} references."""
        model = self.operation_model(call)
        shape = model.input_shape
        members = set(shape.members) if shape is not None else set()
        unknown = sorted(set(call.params) - members)
        if unknown:
            raise CallError(f"{call.service}.{call.operation}: unknown parameters {unknown}")
        required = set(shape.required_members) if shape is not None else set()
        missing = sorted(required - set(call.params))
        if missing:
            raise CallError(f"{call.service}.{call.operation}: missing required {missing}")

    def supports_dry_run(self, call: Call) -> bool:
        shape = self.operation_model(call).input_shape
        return shape is not None and "DryRun" in shape.members

    def invoke(self, call: Call) -> dict[str, Any]:
        self.validate(call)
        try:
            response = getattr(self.client(call.service), call.operation)(**call.params)
        except ClientError as exc:
            err = exc.response.get("Error", {})
            raise CallError(
                f"{call.service}.{call.operation} failed: "
                f"{err.get('Code', 'Unknown')}: {err.get('Message', exc)}",
                code=str(err.get("Code", "")),
            ) from exc
        except BotoCoreError as exc:
            raise CallError(f"{call.service}.{call.operation} failed: {exc}") from exc
        response.pop("ResponseMetadata", None)
        cleaned: dict[str, Any] = _jsonable(response)
        return cleaned


def has_templates(value: Any) -> bool:
    if isinstance(value, str):
        return bool(TEMPLATE.search(value))
    if isinstance(value, dict):
        return any(has_templates(v) for v in value.values())
    if isinstance(value, list):
        return any(has_templates(v) for v in value)
    return False


def referenced_steps(value: Any) -> set[str]:
    if isinstance(value, str):
        return {m.group(1) for m in TEMPLATE.finditer(value)}
    if isinstance(value, dict):
        return set().union(*(referenced_steps(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(referenced_steps(v) for v in value), set())
    return set()


def resolve(value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
    """Replace ${steps.<id>.<jmespath>} with values from earlier step responses."""
    if isinstance(value, dict):
        return {k: resolve(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs) for v in value]
    if not isinstance(value, str):
        return value

    def lookup(match: re.Match[str]) -> Any:
        step_id, path = match.group(1), match.group(2)
        if step_id not in outputs:
            raise CallError(f"reference to step {step_id!r}, which has not run")
        found = jmespath.search(path, outputs[step_id])
        if found is None:
            raise CallError(f"step {step_id!r} response has nothing at {path!r}")
        return found

    whole = TEMPLATE.fullmatch(value)
    if whole:  # keep the referenced value's type (int, list, ...)
        return lookup(whole)
    return TEMPLATE.sub(lambda m: str(lookup(m)), value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if hasattr(value, "read"):
        return "<stream>"
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


@lru_cache(maxsize=1)
def available_services() -> frozenset[str]:
    return frozenset(boto3.session.Session().get_available_services())
