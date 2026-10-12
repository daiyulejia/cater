# Integration Review

Cater should not integrate a third-party tool, framework, CLI, MCP server, or agent runtime by reading names and guessing behavior.

## Principle

Evidence before integration.

Variable names, function names, README slogans, and package popularity are hints, not proof. Before adopting a dependency or wrapping an external CLI, inspect the actual contract and failure behavior.

## Checklist

1. Contract: what exact input and output shape does the tool accept and return?
2. Failure modes: how does it signal timeout, malformed output, auth failure, partial success, rate limit, and crash?
3. State: does it keep hidden memory, cache, files, sessions, or credentials?
4. Cost: what token, latency, process, or network cost does one call add?
5. Isolation: can it live behind Cater's driver boundary without leaking framework assumptions into core runtime?
6. Observability: can Cater record enough events to replay what happened without storing secrets?
7. Degradation: what should Cater do when the tool is unavailable?
8. Tests: can a fixture or probe verify the adapter contract without the real service?

## For LangChain, LangGraph, MCP, And CLIs

These tools can be useful, but they should be treated as execution backends or adapters.

- Keep Cater's event log, task model, distribution flow, and governance semantics independent.
- Wrap the external tool in a driver process or adapter.
- Probe the adapter before trusting it.
- Normalize output into Cater's small JSON shape.
- Contain failures with retries, failure events, and circuit breaker policy.

## Warning Signs

- The integration is justified mainly by the tool's name.
- Cater core imports a heavy framework for one driver use case.
- Error paths are unknown or only tested manually.
- The adapter returns free-form text where Cater expects structured decisions.
- The tool adds hidden state that cannot be replayed from Cater events.

## Decision Rule

Use the smallest integration that can be observed, tested, replaced, and removed.
