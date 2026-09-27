"""Classify an operation by what it does to the account."""

from __future__ import annotations

from awspilot.models import Risk

_READ = ("describe", "get", "list", "head", "lookup", "search", "scan", "query", "batch_get")
_DESTROY = (
    "delete", "terminate", "remove", "deregister", "purge", "destroy", "revoke",
    "disable", "deactivate", "release", "cancel", "stop", "detach", "disassociate",
)
_CREATE = ("create", "run", "allocate", "register", "import", "publish", "request")


def classify(operation: str) -> Risk:
    if operation.startswith(_READ):
        return Risk.READ
    if operation.startswith(_DESTROY):
        return Risk.DESTROY
    if operation.startswith(_CREATE):
        return Risk.CREATE
    # put/update/modify/attach/tag/... and anything unrecognised: treat as a change
    return Risk.MODIFY
