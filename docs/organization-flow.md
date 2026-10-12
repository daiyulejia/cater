# Cater Organization Flow

Cater has three paths. They are intentionally not the same size.

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

## Choosing A Path

- If one role can execute safely, use direct assignment.
- If the work needs coordination, use cabinet dispatch.
- If outputs conflict or confidence drops, let governance enter.
- If governance gets heavy without improving results, simplify the mechanism.

## Why This Matters

Cater should not become a machine for producing meetings. The organization exists to improve task quality, preserve traceability, and recover from drift. When a layer stops doing that, it should be reviewed, merged, bypassed, or removed.
