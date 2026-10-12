# Cater Architecture Review

This note is for reviewing Cater's own organization mechanics, not a user's task result.

## Review Question

Are the distribution, governance, cabinet, supreme fallback, review, and human-command mechanisms still earning their cost?

## Core Risk

Multi-agent systems can become expensive bureaucracy. Every layer can add prompts, events, waiting, duplicated reasoning, and token cost. Cater should keep the organization useful without making every task pass through every institution.

## Current Position

Cater supports both lightweight and full paths:

- Simple work can be assigned directly to one worker role.
- Complex work can be dispatched through the pyramid: orchestrator mission, cabinet decomposition, worker execution, review.
- Governance should appear when risk signals justify it: challenge, option split, low confidence, driver failure, or review findings.
- Human commands can redirect the system, but humans are not the default decision engine.

## Review Lenses

Use these questions during recurring self-review:

1. Distribution cost: did dispatch add clarity, or only extra events and prompt text?
2. Agent count: could fewer roles produce the same result with less duplication?
3. Token budget: are assignments, briefs, memory, and review text growing faster than task value?
4. Governance trigger quality: are cabinet rounds catching real risk, or firing because the policy is too sensitive?
5. Direct assignment safety: when humans bypass upper layers, do low confidence, challenge, or failures still bring oversight back?
6. Replay usefulness: can a maintainer understand why a task went through direct assignment, cabinet dispatch, or governance?
7. Integration evidence: did any new tool or framework get adopted because of verified behavior rather than names and assumptions?
8. Worker exchange quality: are agents responding, handing off, or correcting one another when useful, or just producing parallel monologues?
9. Removal pressure: which mechanism would we delete first if the project felt slow or heavy?

## Decision Rule

Prefer the lightest path that preserves ownership, observability, and recovery.

- If one worker can finish safely, assign directly.
- If ownership or dependencies are unclear, dispatch.
- If outputs conflict or confidence drops, use governance.
- If cabinet cannot resolve it, use supreme fallback.
- If the system cannot safely decide, ask the human.

## Warning Signs

- Every new task creates assignments for every role by default.
- Review text is longer than the work it critiques.
- Multiple agents restate the same idea without adding evidence.
- Multi-agent output never contains explicit handoff or reply links, even when work clearly depends on another role.
- Agents add reply links to every message and turn collaboration into chatter instead of progress.
- Cabinet votes happen on obvious implementation details.
- Token cost grows mainly because of framework ceremony.
- Human direct assignments never receive later oversight, even when execution fails.
- A third-party tool is trusted because its function names sound right, without contract or failure-mode inspection.

## Desired Shape

Cater should feel like an autonomous team with proportional process:

- direct when obvious
- collaborative by default
- governed when risky
- reviewable after the fact
- easy to simplify when a layer stops helping
