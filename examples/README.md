# Cater Driver Examples

Examples in this folder show how to wrap external tools without letting them leak into Cater core.

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

## Integration Rule

Do not infer behavior from package names, function names, or framework marketing.

Before wrapping a tool:

- run a probe
- inspect its real input and output shape
- decide how timeout, crash, malformed output, auth failure, and rate limit should be represented
- keep secrets out of events
- normalize the result into Cater's small JSON response shape
- add a fixture or deterministic test when possible

See [../docs/integration-review.md](../docs/integration-review.md).
