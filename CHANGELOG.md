# Changelog

## 0.1.1

- Fixed the LocalStack integration suite: it called a state-reset endpoint that
  does not exist on current LocalStack images, then a race where the health
  endpoint reported ready before individual services had finished restarting.
  All 15 integration tests now pass in CI, on every push.

## 0.1.0

First release.

- LangGraph pipeline: plan, safety, dry run, approval, execute, repair, rollback, verify
- Claude and OpenAI providers behind one interface
- Every AWS call validated against botocore's service model before it is sent
- Deterministic safety rules for public S3 access, wildcard and admin IAM grants,
  open SSH/RDP, account-level services, and service and region allowlists
- JSONL audit log and `cleanup` from the log
- 15 workflow recipes with hand-written ground-truth checks
- Benchmark runner that scores runs against those checks
- Offline `demo` command
