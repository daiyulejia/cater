# Cater Organization Flow

Cater has three paths. They are intentionally not the same size.

A separate cabinet is optional: `examples/compact_roles.json` explicitly sets
`organization.cabinet_members` to `[]`. Existing configurations retain their
behavior. With no cabinet, the leader publishes dispatch plans and records
requests for evidence/rework when governance is triggered; no empty cabinet vote
is produced. This leader is a local policy, not a model-driven reviewer.

## 1. Direct Worker Path

Use this when the task is small, obvious, or already scoped by the human.

```text
human command -> direct assignment -> worker output -> review
```

Example:

```bash
python -m cater.cli new "Fix the CLI typo" --assign-to dev --run
python -m cater.cli assign <task_id> dev --objective "Implement the smallest useful path"
```

This path should be cheap. It avoids cabinet decomposition unless the worker raises a challenge, reports low confidence, fails, or later review finds a gap.

## 2. Cabinet Dispatch Path

Use this when ownership, sequencing, or acceptance is unclear.

```text
human command -> orchestrator mission -> cabinet decomposition -> worker lanes -> collaboration round -> review
```

Example:

```bash
python -m cater.cli dispatch <task_id>
python -m cater.cli run <task_id>
```

This path is heavier but more inspectable. It creates replayable mission, decomposition, assignment, and plan events.

Each assigned lane should include a concrete deliverable and explicit
dependencies. The default lane chain is scope -> implementation -> verification:
product clarifies value and acceptance, development turns it into an
implementation path, and testing reviews risk and confidence. If dispatch does
not add this kind of sequencing clarity, prefer direct assignment.

Dependencies currently describe the plan; they do not yet gate execution on
independent acceptance of upstream artifacts. Agreement and a generated plan
must not be treated as proof that the work has passed verification.

## 3. Governance Path

Use this when execution shows risk.

```text
worker outputs -> challenge / option split / low confidence / failure -> cabinet vote -> supreme fallback -> rare human override
```

Example:

```bash
python -m cater.cli battle <task_id>
```

Governance is not the default work mode. It is the anti-drift mechanism.

## Worker Communication

Worker agents should normally collaborate before governance enters. A worker can
continue, correct, or hand off another worker's output by returning:

```json
{"target": "dev", "reply_to": 12}
```

`target` names the role being addressed. `reply_to` points at a prior event
sequence number, so the exchange stays replayable through the append-only event
log. This keeps ordinary third-layer communication cheap while still making real
disagreement visible enough for governance to trigger when needed.

## Choosing A Path

- If one role can execute safely, use direct assignment.
- If the work needs coordination, use cabinet dispatch.
- If outputs conflict or confidence drops, let governance enter.
- If governance gets heavy without improving results, simplify the mechanism.

## Why This Matters

Cater should not become a machine for producing meetings. The organization exists to improve task quality, preserve traceability, and recover from drift. When a layer stops doing that, it should be reviewed, merged, bypassed, or removed.
