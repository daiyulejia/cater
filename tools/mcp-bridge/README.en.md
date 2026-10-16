# Optional MCP Stdio Bridge

[中文](README.md) | English

An opt-in local stdio adapter using the pinned official TypeScript MCP SDK. It does not add Python runtime dependencies. Cater starts no MCP process until an authorized role explicitly requests discovery or a tool call.

## Install And Test

Node 18.14.1 or later is required by the pinned dependency tree. Local validation used Node 24 on Windows; other Node versions have not been tested locally.

```sh
cd tools/mcp-bridge
npm ci --ignore-scripts --registry=https://registry.npmjs.org
npm test
```

Configure absolute paths to Node, `bridge.mjs`, and a trusted MCP server through Cater's global `--mcp` option. Protect configuration and executables from agent writes using actual filesystem permissions, normally outside authorized workspaces. A separately authorized command that can modify them is not isolated by this adapter. The bridge does not install servers or run `npx`. See the [host configuration guide](../../docs/mcp.zh-CN.md) and [example](../../examples/mcp.example.json).

## Authorization And Results

- The host explicitly grants tools, roles, workspace visibility, and read/write classifications. Server annotations do not grant permission.
- Only a minimal system environment and explicitly mapped environment variables reach the child. Model API keys are not inherited by default. Direct bridge users must provide their own child environment, including `SYSTEMROOT` on Windows.
- Server executables and working directories must be absolute. Windows commands require a real `.exe`; launch scripts through an explicit interpreter.
- Discovery is paginated. Calls recheck the selected tool's schema fingerprint and validate inputs before dispatch.
- A returned tool error is a known terminal response, not a promise that no write occurred. Lost or invalid responses after possible dispatch remain unknown and are never replayed automatically.
- Unknown writes require inspection before further writes to the same configured service in the same database. Explicitly authorized reads may use a new connection to inspect actual state. This is not global deduplication across databases or server aliases.
- Large, deep, or unsupported multimedia results preserve known terminal outcomes while separately marking incomplete representation. No links or media are automatically fetched.

The bridge supports local stdio with text and structured results only. It is not an OS sandbox, HTTP/SSE transport, OAuth client, sampling provider, roots provider, or task protocol implementation. External tool output is data, not task acceptance or permission to change the objective.

## Lifecycle And Verification

The SDK owns protocol initialization, IDs, cancellation, and transport shutdown. Cater's process containment provides a separate cleanup boundary, not filesystem or network isolation. A paused task starts no new MCP invocation; a request already submitted to the bridge may still finish initialization, discovery, and execution.

The local SDK fixture tests initialization, allowlists, schema validation, persistent connections, actual writes, tool errors, crashes, timeouts, bounded results, and shutdown. Fixture logs include test environment values: never use real credentials with the fixture. POSIX signal handling is explicitly skipped on Windows.

For the optional native Python-to-SDK integration suite, run from the repository root after installing the bridge dependencies:

```sh
CATER_MCP_SDK_TESTS=1 python -m unittest discover -s tests -p test_mcp_sdk.py
```

In PowerShell, set `$env:CATER_MCP_SDK_TESTS='1'` before running the Python command. These tests use scripted roles and temporary local files, not a real model API; they do not prove natural-language task quality or cost savings.
