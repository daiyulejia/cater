# Cater Driver Examples

Examples in this folder show how to wrap external tools without letting them leak into Cater core.

## Web API Client

Start Cater locally:

```bash
cater web
```

Then, from another terminal:

```bash
python examples/api_client.py
```

The example creates a task through `POST /api/tasks`, runs one collaboration
round through `POST /api/tasks/<task_id>/run`, and verifies `/api/chain`.

## NDJSON Driver Contract

A CLI driver reads one JSON request per line and writes one JSON response per line.

Probe request:

```json
{"id":"req-1","type":"ping"}
```

Probe response:

```json
{"id":"req-1","ok":true,"pong":true}
```

Completion request:

```json
{"id":"req-2","type":"complete","role":"developer","prompt":"...","context":{"task_id":"...","goal":"..."}}
```

Completion response:

```json
{
  "id": "req-2",
  "ok": true,
  "text": "{\"stance\":\"assert\",\"option_id\":\"A\",\"claim\":\"...\",\"evidence\":[],\"confidence\":0.8,\"text\":\"...\"}",
  "tokens": 123,
  "meta": {"adapter": "example"}
}
```

The nested `text` JSON is the agent message payload. It may include optional
worker-to-worker reply metadata when the backend is genuinely continuing,
correcting, or handing off another agent's output:

```json
{
  "stance": "assert",
  "option_id": "A",
  "claim": "Continue the implementation plan from dev",
  "evidence": ["uses prior worker output"],
  "confidence": 0.8,
  "text": "I will continue from the referenced implementation note.",
  "target": "dev",
  "reply_to": 12
}
```

`target` must be a configured role id. `reply_to` must point to an event sequence
that was visible to the agent in the prompt. Cater drops invalid reply metadata
instead of storing speculative links.

`examples/ndjson_tool_driver.py` includes a tiny deterministic version of this:
when the prompt contains a visible worker line such as
`alpha/assert: Draft the plan (#12)`, it replies with `target="alpha"` and
`reply_to=12`. Real adapters should do this only when they are genuinely
continuing, correcting, or handing off visible work.

## Integration Rule

Do not infer behavior from package names, function names, or framework marketing.

Before running a configured external driver through a real task, use the
machine-readable doctor output:

```bash
cater --drivers examples/tool_driver_config.json doctor --probe --json
```

Before wrapping a tool:

- run a probe
- inspect its real input and output shape
- decide how timeout, crash, malformed output, auth failure, and rate limit should be represented
- keep secrets out of events
- normalize the result into Cater's small JSON response shape
- add a fixture or deterministic test when possible

See [../docs/integration-review.md](../docs/integration-review.md).
