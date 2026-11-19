# Round 3 implementation and final check

Date: 2026-08-30

## Implemented

- Restructured navigation around Inbox, Incidents, Runs, and Digests; moved
  policy and v2 compatibility under System.
- Removed raw protocol vocabulary and duplicated metadata from primary rows.
- Added dense summary bars, responsive cards, compact filters, a two-column
  digest layout, and a visual core authority chain.
- Added a durable `agent_runs` projection and an opt-in, exact-label-scoped
  agentctl observer. Agentctl stays lifecycle authority; CAR never calls
  `result` or `await` from the observer.
- Added a Runs page that keeps high-volume progress out of Inbox, preserves
  Cursor/OMP/Codex identity, shows liveness and duration, and says “not
  reported” instead of guessing model or profile.
- Made incident handoff text capability-aware. Telegram is identified as the
  response surface, while Cursor/OMP answers are described as staged rather
  than falsely claiming native continuation.

## Visual verification

Fresh captures are stored outside the repository under:

`/Users/dazheng/.codex/visualizations/2026/08/27/01a043d3-ef1f-7a11-bb4a-ce6a9f2051a3/v3-ux-audit/round-3-after/`

The audit covered 1440 px desktop, 768 px tablet, 390 px mobile, and 320 px
narrow mobile across Inbox, Incidents, incident detail, Digests, Safety &
policy, and Legacy compatibility. The final mobile Runs and incident captures
have no document-level horizontal overflow at 390 px.

## Verification

- Web route suite includes native-agent filtering, truthful incident handoff,
  Runs health/empty/degraded states, and existing UI flows.
- Observer tests cover disabled-by-default behavior, bounded discovery,
  progress compaction, degraded coverage, and stale liveness.
- Store tests cover quiet refreshes and rejection of stale run snapshots.
- Full TypeScript and Bun test results are recorded in the final task handoff.

## Evidence limits

Screenshot review can assess hierarchy, density, wrapping, and visible contrast,
but it does not establish complete accessibility conformance. Keyboard order,
screen-reader announcement quality, browser zoom behavior, and real Telegram
round trips still require interactive testing in the target environment.
