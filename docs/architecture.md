# Architecture

## The pieces

| File | Role |
|---|---|
| `models.py` | `Plan`, `Step`, `Call`, `Check` and the result types, as pydantic models |
| `llm/` | Provider interface with Claude, OpenAI and scripted-fake implementations |
| `aws/caller.py` | The only code that talks to AWS. Validates, invokes, resolves references |
| `safety/` | Risk classification and the blocking rules |
| `engine.py` | Each pipeline stage as a plain function |
| `graph.py` | The LangGraph state machine that connects the stages |
| `runner.py` | Runs one request; handles the approval pause; cleanup |
| `recipes/` | Recipe loader and the YAML library |
| `bench/` | Benchmark runner and report |
| `audit.py` | JSONL audit log |

## One tool instead of hundreds

The agent has a single tool: `aws_call(service, operation, params)`. It maps directly
onto boto3, so every AWS API is reachable without writing a wrapper per operation.
What keeps that safe is that botocore ships a machine-readable model of every
operation. Before a call is sent, the operation name and parameters are validated
against that model.

## Passing values between steps

A step can refer to an earlier response with `${steps.<id>.<JMESPath>}`:

```yaml
QueueUrl: ${steps.create_queue.QueueUrl}
```

A reference that is the whole value keeps its type. A reference inside a longer string
is interpolated, which is how ARNs get into JSON policy strings. The dry run rejects
references to steps that run later or do not exist.

## The model interface

Providers implement one method that returns text. `complete_json` wraps it: it asks for
JSON matching a pydantic schema, validates the reply, and on failure sends the
validation error back for another attempt. Nothing else in the codebase knows which
provider is in use.

## Approval

The approval stage uses LangGraph's `interrupt`. The graph pauses with the plan and
safety report as its payload, the CLI asks the question, and the graph resumes with the
answer. If no approver is supplied, the answer is no.

## Repair and rollback

When a call fails, the model is asked for a corrected version of that one step. The
revised plan goes back through the safety rules and the dry run before execution
resumes from the failed step. Steps that already ran are not repeated. On a real
account, a repair that changes the call also goes back for approval.

If repairs run out, or a repair is blocked by a safety rule, completed steps are undone
in reverse order using rollback calls that were resolved at the time each step ran.
