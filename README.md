# Cater

**Language:** English | [中文](README.zh-CN.md)

> Autonomous multi-agent organization runtime.

Cater turns multiple AI agents into a small, inspectable organization. Agents collaborate on work, exchange outputs, surface real disagreements when needed, vote or decide only when the task calls for it, and escalate to humans only when the organization cannot resolve the task itself.

## Why

Most agent demos optimize for the final answer. Cater treats the process as the product.

- Every important step is an append-only event.
- Task state is folded from the event log.
- Collaboration is the default; battle and decision ladders are tools for meaningful disagreement.
- Human commands and manual overrides are normal events.
- The CLI exposes the same surface future Web and mobile clients will use.

## Positioning

Cater is not trying to replace LangChain or LangGraph.

LangChain and LangGraph can be useful inside driver implementations or advanced execution backends. Cater keeps its own event log, task model, decision ladder, and human command semantics so the collaboration process remains inspectable and portable.

## Quick Start

Run a replayable demo first:

```bash
cater demo
cater demo --direct
cater review <task_id> --plain
cater chain --json
cater web
```

`cater demo` shows the governance-heavy path. `cater demo --direct` shows a lightweight direct-worker path where governance stays idle unless risk appears. Demo commands use deterministic built-in roles and drivers.

For the conversation-first local workroom, keep `cater web --port 8766` running,
then start the optional React UI in [`web/`](web/README.md). The Python runtime
remains zero-dependency; the workroom has its own frontend dependencies.

Then explore the organization surface:

```bash
cater drivers
cater show
cater inbox
cater policy
cater path <task_id>
cater path <task_id> --json
cater doctor
cater doctor --probe
cater doctor --json
cater dispatch <task_id>
cater run <task_id>
cater export <task_id>
cater events <task_id>
cater review --next
cater decide <task_id> --resolution done --reason "Ship the smallest loop first"
```

By default Cater writes to `./.cater/cater.db`.

From a source checkout, the equivalent form is `python -m cater.cli ...`.

## Verification

```bash
python scripts/check.py
```

On Windows, if `python` points to the Microsoft Store placeholder, use:

```powershell
.\scripts\check.ps1
```

## Commands

```bash
cater new "Fix the CLI typo" --assign-to dev --run
cater run <task_id>
cater battle <task_id>
cater dispatch <task_id>
cater assign <task_id> dev --objective "Implement the smallest useful path"
cater show [task_id]
cater events [task_id]
cater export [task_id] --format json
cater export [task_id] --format jsonl
cater review [task_id] --record
cater review [task_id] --plain
cater review [task_id] --next
cater inbox
cater profiles
cater policy
cater doctor
cater doctor --probe
cater doctor --probe --json
cater decide <task_id> --resolution done --reason "Accepted"
cater chain
cater chain --json
cater drivers
cater --drivers drivers.json probe-driver product
cater --driver-attempts 3 --circuit-breaker-threshold 4 run <task_id>
cater web --host 127.0.0.1 --port 8765
cater --audit-key "dev-secret" demo
cater --audit-key "dev-secret" chain
cater --audit-key "dev-secret" chain --json
```

`run` is the normal collaboration loop. It records agent contributions and only activates governance when there is a challenge, option split, low confidence, or another risk signal. `battle` forces a governance round when you intentionally want cabinet review.

`dispatch` records the pyramid-style task distribution path: the top layer frames the mission, the cabinet decomposes it into execution lanes, and worker agents receive assigned work before the next collaboration round. This is optional; small tasks can go straight to a worker with `assign` or `new --assign-to dev --run`.

`path` summarizes how one task moved through distribution, collaboration, governance, and decision steps.

`--driver-attempts` and `--circuit-breaker-threshold` tune retry containment without changing driver code.

`doctor --json` returns the same health check in a machine-readable shape for CI, scheduled review, and pre-integration probes.

See [docs/organization-flow.md](docs/organization-flow.md) for when to use direct assignment, cabinet dispatch, or governance.

## Local Web API

When `cater web` is running, local tools can read machine-friendly state from:

```text
POST /api/tasks
POST /api/tasks/<task_id>/run
POST /api/tasks/<task_id>/battle
POST /api/tasks/<task_id>/dispatch
POST /api/tasks/<task_id>/assign
POST /api/tasks/<task_id>/command
POST /api/tasks/<task_id>/decide
GET /api/tasks
GET /api/tasks/<task_id>
GET /api/chain
```

`POST /api/tasks` accepts JSON with `goal`, optional `acceptance`, and optional booleans `dispatch`, `run`, or `battle`.
Task action endpoints return the same replay payload as `GET /api/tasks/<task_id>`.
See [examples/api_client.py](examples/api_client.py) for a zero-dependency client.

## Current Scope

Implemented now:

- SQLite append-only event store with hash-chain verification
- task creation, collaboration rounds, risk-triggered governance, targeted battle, cabinet vote, supreme fallback, and rare human command/override
- optional pyramid-style task distribution with mission framing, cabinet decomposition, direct worker assignment, and replayable plans
- lightweight worker-to-worker replies through role targets and event-sequence references
- deterministic scripted drivers for repeatable demos and tests
- configurable roles and drivers from JSON
- long-lived NDJSON CLI drivers with probe and crash recovery
- configurable cabinet vote tally with approval threshold, quorum, veto roles, recommendation, and open concerns
- local Web workbench for task creation, collaboration rounds, visible decision ladder, rare human input, replay, cabinet tally, and command override
- agent profile projection, task-local collaboration memory, and same-round output sharing
- driver failure containment with retry attempts, failure events, and an in-process circuit breaker
- optional HMAC audit signatures on top of the event hash chain
- pure fold from events to task snapshots
- CLI commands for process inspection
- zero third-party runtime dependencies

## Driver Config

Drivers are replaceable. The default package uses deterministic scripted drivers, but you can override any driver with a local CLI process:

```json
{
  "drivers": [
    {
      "kind": "cli",
      "name": "product",
      "command": ["python", "my_driver.py"],
      "timeout": 120,
      "restart_limit": 1
    }
  ]
}
```

The CLI child process speaks newline-delimited JSON. Cater sends:

```json
{"id":"...","type":"ping"}
{"id":"...","type":"complete","role":"product","prompt":"...","context":{"task_id":"...","goal":"..."}}
```

The driver replies:

```json
{"id":"...","ok":true,"pong":true}
{"id":"...","ok":true,"text":"{\"stance\":\"assert\",\"option_id\":\"A\",\"claim\":\"...\",\"evidence\":[],\"text\":\"...\"}","tokens":123,"meta":{}}
```

This is intentionally small. LangChain, LangGraph, Codex CLI, Claude Code, Gemini CLI, or a custom API wrapper can live behind this process boundary without leaking into Cater's core runtime.

The same boundary is where skills and MCP belong: wrap them in a driver process, let that adapter call the tool, and return Cater's small JSON shape. See `examples/ndjson_tool_driver.py` for a dependency-free adapter skeleton. If your Windows `python` command points to the Microsoft Store placeholder, replace it with the full path to a real Python interpreter in `examples/tool_driver_config.json`.

## Cabinet Policy

Roles can be configured together with voting policy:

```json
{
  "cabinet": {
    "approval_threshold": 2,
    "quorum": 3,
    "veto_roles": ["test"]
  },
  "governance": {
    "trigger_on_challenge": true,
    "trigger_on_option_split": true,
    "low_confidence_threshold": 0.5
  },
  "roles": [
    {"id": "product", "name": "Product", "role": "product", "driver": "product"},
    {"id": "dev", "name": "Developer", "role": "developer", "driver": "dev"},
    {"id": "test", "name": "Tester", "role": "tester", "driver": "test"}
  ]
}
```

This keeps the organization model visible. A project can start with simple majority voting, then add quorum, veto roles, or a supreme fallback as reliability needs grow.

Governance triggers decide when normal collaboration should involve the cabinet. Teams can be strict and review every option split, or let agents keep exchanging work until a challenge or low-confidence signal appears.

## Audit

The event log always has a hash chain. If you provide an audit key, new events also receive an HMAC signature:

```bash
cater --audit-key "dev-secret" demo
cater --audit-key "dev-secret" chain
```

You can also use `--audit-key-file` or the `CATER_AUDIT_KEY` environment variable.

Use `cater export` to produce a machine-readable replay bundle, or `cater export --format jsonl` to stream raw events.

## Self Review

```bash
cater review --plain
cater review <task_id> --record
```

`review` inspects the work from product, design, testing, UI, logic, reliability, and adoption angles. Use `--plain` for a quick human-readable pass. Without `--record` it is a pure projection; with `--record` it appends a `review.generated` event so the critique itself becomes replayable.

Architecture-level review lives in [docs/architecture-review.md](docs/architecture-review.md). It asks whether Cater's own distribution and governance mechanisms are still worth their token, latency, and complexity cost.

## Roadmap

Next iterations:

1. Product entry: make `cater demo`, `cater new`, `cater dispatch`, `cater run`, `cater command`, and `cater drivers` feel like a coherent first-run workflow.
2. Distribution engine: make cabinet decomposition richer while keeping plans event-derived and inspectable.
3. Collaboration modes: separate normal production exchange from explicit disagreement rounds in CLI/Web.
4. Web replay: add live refresh and filters.
5. Memory governance: keep near-term context explainable, bounded, and event-derived; leave deeper learning systems as a future research layer.
6. Reliability: expose recovery controls in CLI/Web and make failure policies easier to inspect.
7. Audit: add key rotation metadata, unsigned-history policy, and exportable audit reports.
8. Distribution: add examples for wrapping popular third-party CLIs while keeping the core dependency-free.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the local check command, driver-boundary rules, and commit expectations.

## Docs

See [docs/README.md](docs/README.md) for organization flow and architecture review notes.

## Iteration Lens

Cater should grow through small usable loops. Each iteration should be reviewed from multiple angles:

- product: does this make the next user action obvious?
- design: can the process be understood at a glance?
- testing: can failures be replayed and reproduced?
- UI: is the human command surface calm and actionable?
- logic: are events, projections, worker exchanges, and driver boundaries still simple?
- adoption: can a new contributor run it without extra services?

## Design Principles

1. Process first. The collaboration process must be visible.
2. Events first. State is a view over append-only history.
3. Drivers are replaceable. Models, CLIs, and APIs are not hardwired.
4. Collaboration first. Disagreement is normal, but it should be surfaced when it improves the task rather than performed by default.
5. Distribution should be proportional. Simple work can go straight to a worker; complex work can use cabinet dispatch.
6. Oversight is event-driven. Upper layers re-enter when risk signals, low confidence, failure, or review findings justify it.
7. Humans command and override. Humans can redirect the organization at any time, but are not the default decision engine.
