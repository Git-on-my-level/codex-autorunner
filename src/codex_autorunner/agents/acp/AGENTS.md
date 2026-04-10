# ACP Module — Agent Guide

This package is CAR's generic ACP client/supervisor layer.

Use this guide before changing `client.py`, `events.py`, `protocol.py`, or
`supervisor.py`.

## Why this guide exists

ACP has two materially different shapes in this codebase:

- Legacy / notification-heavy adapters
- Official ACP session protocol adapters

Several Hermes regressions came from treating those two shapes as interchangeable.
The main risk is assuming a notification-based terminal contract in places where
official ACP is actually request/response based.

## Core split: legacy vs official ACP

`ACPClient._uses_official_session_protocol()` is the branch point.

- Legacy ACP:
  - session creation uses `session/create`
  - prompts start via `prompt/start`
  - prompt lifecycle can be driven by terminal notifications such as
    `prompt/completed`, `prompt/failed`, `prompt/cancelled`
- Official ACP session protocol:
  - session creation uses `session/new`
  - prompts run via `session/prompt`
  - streaming progress arrives through `session/update`
  - turn completion is the `session/prompt` RPC response

Do not assume fixes for one side automatically apply to the other.

## Invariants to preserve

### Official ACP turn completion

For official ACP, CAR must not invent a notification-based completion contract.

- `session/update` is progress, not terminal completion.
- The `session/prompt` response is the completion boundary.
- CAR synthesizes a local terminal event only after that RPC returns.

If `session/prompt` never returns, the turn is still open from ACP's point of
view, even if many `session/update` notifications have arrived.

### Sparse official `session/load` responses

Official ACP `session/load` responses may be sparse.

- `null` should be treated as a missing-session failure.
- `{}` or other object responses without a session identifier should still be
  accepted when the caller already knows the requested `session_id`.

Do not require `sessionId` in official `session/load` success payloads unless
the protocol itself changes and the code/tests are updated together.

### Legacy terminal fallback logic

Legacy adapters may emit prompt-scoped terminal notifications without `turnId`,
or may emit idle/status before a canonical completion event.

- Session-level fallback mapping for missing `turnId` is legacy-only defensive
  behavior.
- Idle-terminal grace logic exists to prefer explicit completion when both idle
  and prompt completion signals appear close together.

Do not copy those heuristics into official ACP logic unless you have a concrete
protocol reason.

## File responsibilities

| File | Responsibility |
|---|---|
| `client.py` | Transport, request/response tracking, official-vs-legacy branching, prompt state |
| `events.py` | Notification normalization into CAR event types |
| `protocol.py` | Coercion/parsing of ACP initialize/session/prompt payloads |
| `supervisor.py` | Workspace-scoped client lifecycle and higher-level handle management |
| `errors.py` | ACP-specific error surface |

## Before changing behavior

Answer these questions explicitly:

1. Is this change for legacy ACP, official ACP, or both?
2. Is the completion boundary notification-driven or RPC-response-driven?
3. Are you changing protocol interpretation, or just hardening around malformed
   adapters?
4. What test proves the exact boundary being changed?

If the answer is unclear, stop and add a fixture/test first.

## Required tests for ACP behavior changes

At minimum, update or add focused tests in:

- `tests/agents/acp/test_client.py`

Use `tests/fixtures/fake_acp_server.py` to model protocol behavior directly.

If the change affects surface behavior, add or update a cross-surface test such
as:

- `tests/chat_surface_integration/test_hermes_pma_official_timeout.py`

## Existing regression coverage to preserve

These tests cover real historical failure modes:

- legacy terminal notification without `turnId`
- idle-status then explicit completion ordering
- official `session/prompt` hang remaining non-terminal until RPC return
- official sparse `session/load` success payloads
- official `null` `session/load` failure payloads

If you remove or weaken one of these, document why in the PR.

## When integrating a new ACP-backed agent

Before relying on the shared ACP client, determine:

1. Does it advertise the official ACP session protocol (`agentInfo` +
   `agentCapabilities`)?
2. Does `session/prompt` always return terminally?
3. Are `session/load` responses sparse?
4. Does the agent emit any non-standard terminal notifications anyway?

Add a fixture scenario for any non-default behavior before wiring the agent into
Discord/Telegram/PMA surfaces.
