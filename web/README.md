# Cater Workroom

This is Cater's optional React workroom. The Python runtime remains usable with
zero third-party runtime dependencies; this package is the richer local UI for
people who want a conversation-first task surface.

## Run locally

Start the Cater API in one terminal:

```powershell
python -m cater.cli web --port 8766
```

Start the workroom in another terminal:

```powershell
cd web
pnpm install
pnpm dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` and `/tasks` to the Python
runtime at port `8766`.

## Product boundary

- The workroom is a projection of the event log, not a second source of truth.
- Every human direction, assignment, agent contribution, decision, and failure
  remains an event that can be inspected from the conversation.
- The UI defaults to mission dispatch and one collaboration round, while the
  engine still keeps governance conditional on actual disagreement or risk.
