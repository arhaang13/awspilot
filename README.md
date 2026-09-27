# awspilot

Describe an AWS setup in plain English. awspilot plans the API calls, checks the plan
against safety rules, shows it to you for approval, runs it, and then reads the
resources back to confirm they match what you asked for.

```
$ awspilot demo sqs-queue-with-dlq
Request: Create an SQS queue named orders with a dead-letter queue named orders-dlq.
         Messages go to the dead-letter queue after 5 failed receives.

 #  Step          Call                       Risk
 1  create_dlq    sqs.create_queue           create
 2  dlq_arn       sqs.get_queue_attributes   read
 3  create_queue  sqs.create_queue           create

Run this plan? [y/N]: y
  pass  check: main queue exists
  pass  check: dead-letter queue exists
  pass  check: redrive policy points at the dead-letter queue

succeeded in 0.3s
Cleaned up.
```

That is the offline demo, which runs a stored plan against an in-process fake AWS
(table trimmed for width). With a model configured, `awspilot run "<request>"` writes
the plan itself.

## Why I built it

Setting things up in the AWS console is slow and easy to get subtly wrong. Letting a
language model loose on an AWS account is fast and easy to get badly wrong. I wanted to
see how much of the speed you can keep while making the dangerous parts deterministic.

The design rule: **the model proposes, plain code decides.** The model only ever writes
a plan. Whether that plan is valid, safe, approved, and successful is decided by code
that does not ask the model's opinion.

## How it works

```
request ─▶ plan ─▶ safety ─▶ dry run ─▶ approve ─▶ execute ─▶ verify
           (LLM)   (rules)   (botocore)  (you)        │
             ▲                  │                     ▼ on failure
             └── replan ◀───────┘            repair (LLM) ─▶ safety ─▶ …
                                                      │ out of attempts
                                                      ▼
                                                  roll back
```

| Stage | What it does | Uses the model? |
|---|---|---|
| Plan | Turns the request into ordered boto3 calls, each with checks and a rollback | Yes |
| Safety | Blocks public buckets, wildcard or admin IAM grants, SSH/RDP open to the internet, account-level services, and anything outside the allowed services and regions | No |
| Dry run | Validates every call against botocore's service model, so invented operations and parameters fail before anything runs | No |
| Approve | Shows the plan and waits for a yes | No |
| Execute | Runs the calls in order, passing values between steps | No |
| Repair | On a failed call, asks for a corrected step. The fix goes back through safety | Yes |
| Roll back | Undoes completed steps in reverse if the run can't finish | No |
| Verify | Reads resources back and compares them with what was requested | No |

Every call is written to an audit log (`.awspilot/runs/<run-id>.jsonl`), and
`awspilot cleanup <run-id>` can undo a run from that log alone.

More detail: [architecture](docs/architecture.md), [safety rules](docs/safety.md).

## Try it without an AWS account or API key

```bash
git clone https://github.com/arhaang13/awspilot && cd awspilot
uv sync --all-extras
uv run awspilot recipes                     # list the built-in workflows
uv run awspilot demo sqs-queue-with-dlq     # run one on an in-process fake AWS
uv run pytest                               # unit tests
```

`demo` runs a recipe's hand-written reference plan, so it shows the pipeline rather
than the planner. To see the planner, use a model:

## Use it with a model

```bash
export ANTHROPIC_API_KEY=...                # or: AWSPILOT_PROVIDER=openai OPENAI_API_KEY=...
docker compose up -d                        # LocalStack, a local AWS emulator

uv run awspilot plan "Create an encrypted, versioned S3 bucket named team-reports"
uv run awspilot run  "Create an encrypted, versioned S3 bucket named team-reports"
```

| Setting | Default | Meaning |
|---|---|---|
| `AWSPILOT_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `AWSPILOT_MODEL` | `claude-opus-5-5` / `gpt-4o` | Model id |
| `AWSPILOT_TARGET` | `localstack` | `localstack`, `moto` (in-process), or `aws` |
| `AWSPILOT_REGION` | `us-east-1` | Region to work in |
| `AWSPILOT_ALLOWED_SERVICES` | 15 common services | Comma-separated allowlist |
| `AWSPILOT_MAX_REPAIRS` | `2` | Repair attempts per run |

With `AWSPILOT_TARGET=aws` it uses your normal AWS credentials. `--yes` will not skip
approval there for any plan that modifies or deletes something.

## Benchmark

```bash
uv run awspilot bench --runs 3
```

Each recipe is run several times against a freshly reset backend. A run counts as a
pass only if the recipe's own checks pass afterwards. Those checks are written by hand
and never shown to the planner, so a plan that reports success but skipped a step still
fails. Results go to `bench/results/` and [docs/benchmark.md](docs/benchmark.md).

**No results are published yet.** I will add them here, with the date, model and run
count, once the benchmark has been run with a live model.

## Workflow recipes

15 so far, across S3, SQS, SNS, DynamoDB, IAM, KMS, Secrets Manager, Systems Manager,
CloudWatch, EventBridge and VPC networking. Each recipe
([example](src/awspilot/recipes/library/sqs-queue-with-dlq.yaml)) holds the request,
a few planning notes, the ground-truth checks, and a reference plan. The test suite
runs every reference plan and confirms the checks fail before it and pass after it.

## Limitations

- Tested against moto (in-process) only so far. The LocalStack test suite exists but
  has not been run yet, and nothing has been run against real AWS.
- One API call per step, with JSON parameters. Workflows that need to upload files,
  such as deploying Lambda code, are not supported yet.
- Waiting for slow resources to become ready is not handled yet.
- Emulators are more forgiving than AWS, especially about IAM permissions.
  A workflow that passes on LocalStack can still fail on a real account.
- The safety rules are a denylist of well-known mistakes, not a proof of safety.
  Read the plan before approving it.

## Roadmap

- Run and publish the benchmark
- Grow the recipe library and add file-based workflows (Lambda deploys)
- Waiters for slow resources
- Browser automation (Playwright) for the few console-only tasks

## License

MIT
