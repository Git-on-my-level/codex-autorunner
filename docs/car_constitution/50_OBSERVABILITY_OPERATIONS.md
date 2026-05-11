# Observability & Operations

Principle: observability replaces safety prompts. YOLO is acceptable if behavior is reconstructible from artifacts.

## Run identity
Every run must have:
- stable run_id
- timestamps (start/end/last progress)
- actor identity (agent/backend/surface)
- execution mode (yolo/safe/review)

## Canonical event + log stream
- Logs are append-only and attributable to a run.
- Prefer structured “run events” for phase transitions and key decisions.
- Normalize streaming differences across backends into a consistent representation.

## State transitions
Record transitions and blocking points:
- phase changes
- waiting on IO/subprocess/agent
- retries and backoffs
- timeout boundaries

## Failure is first-class
- Timeouts are failures with evidence.
- Partial output must be explicitly labeled.
- Swallowed exceptions and silent fallbacks are disallowed.

## Debugging order (operational heuristic)
1) Identify run_id
2) Read run metadata
3) Scan state transitions/events
4) Inspect stdout/stderr/stream artifacts
5) Only then inspect code

## Minimum “explainability” bar
From artifacts alone, a debugger must be able to answer:
- what happened?
- why did it happen?
- where did it fail/stall?
- what is the safest replay/retry path?

## Web read-model diagnostics
- Projection lag, snapshot latency, event journal latency, stream lag, cursor
  gaps, and virtualized DOM row counts are part of the Web Hub responsiveness
  budget.
- Use `docs/ops/web-read-models.md` for the concrete rebuild and diagnostic
  commands before changing projection code.
- A stream or snapshot regression must identify the failing family: chat index,
  chat detail, repo/worktree topology, repo/worktree runtime, ticket detail, or
  a newer documented family.
