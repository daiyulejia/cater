# Cater Workroom

This is Cater's optional React workroom. The Python runtime remains usable with
zero third-party runtime dependencies; this package is the richer local UI for
people who want a conversation-first task surface.

## Run locally

Build the workroom once:

```powershell
cd web
pnpm install
pnpm build
```

Then start Cater normally from the repository root:

```powershell
python -m cater.cli web --port 8766
```

Open `http://127.0.0.1:8766`. Cater serves the built workroom, its API, and the
full audit replay from one local origin.

For frontend development, run `python -m cater.cli web --port 8766` and
`pnpm dev` in separate terminals, then open `http://127.0.0.1:5173`. Vite
proxies development requests to Cater's API.

## Product boundary

- The workroom is a projection of the event log, not a second source of truth.
- Every human direction, assignment, agent contribution, decision, and failure
  remains an event that can be inspected from the conversation.
- The UI defaults to mission dispatch and one collaboration round, while the
  engine still keeps governance conditional on actual disagreement or risk.
