# Safety

These checks are ordinary code. The model is not consulted, and it cannot talk its way
past them.

## Blocking rules

A plan is rejected before approval if any step, or any step's rollback, does one of
these:

| Rule | Blocks |
|---|---|
| `service-allowlist` | Any service not on the allowed list |
| `region-allowlist` | Creating resources in a region that is not allowed |
| `account-level` | Organizations, account, billing and identity services; account-wide IAM settings |
| `iam-admin-policy` | Attaching `AdministratorAccess` or `IAMFullAccess` |
| `iam-wildcard-policy` | A policy allowing action `*` on resource `*` |
| `s3-public-access` | Public ACLs, weakening or removing a public access block, bucket policies open to any principal |
| `ec2-open-admin-port` | Security group rules opening SSH, RDP or all ports to the internet |
| `verify-read-only` | A verification check that would change state |

## Risk levels

Every step is labelled `read`, `create`, `modify` or `destroy` from its operation name.
Anything unrecognised is treated as `modify`. The label is shown in the plan.

## Approval

- Nothing runs without approval.
- `--yes` skips the prompt on LocalStack and moto.
- On real AWS, `--yes` does not apply to plans that modify or delete anything.

## What this does not cover

The rules catch well-known mistakes. They do not prove a plan is safe, they do not
estimate cost, and they do not replace reading the plan. For real accounts, run
awspilot with credentials scoped to what you want it to be able to do.
