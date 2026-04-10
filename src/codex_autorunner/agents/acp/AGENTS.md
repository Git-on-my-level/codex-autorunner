# ACP Module — Agent Guide

This package is CAR's generic ACP client/supervisor layer.

Use this guide before changing `client.py`, `events.py`, `protocol.py`, or
`supervisor.py`.

## Why this guide exists

Hermes now uses the official ACP session protocol only.

- session creation uses `session/new`
- prompts run via `session/prompt`
- streaming progress arrives through `session/update`
- approvals arrive through `session/request_permission`
- turn completion is anchored in explicit terminal evidence, with the
  `session/prompt` RPC response acting as the reconciliation fallback

Do not reintroduce legacy notification-terminal heuristics into `client.py`
without a concrete supported runtime and dedicated coverage.

## Invariants to preserve

### Official ACP turn completion

For official ACP, CAR must not invent completion from plain progress or
assistant text alone.

- `session/update` is progress, not terminal completion.
- Explicit terminal notifications such as `prompt/completed`,
  `prompt/failed`, `prompt/cancelled`, `session.idle`, or idle
  `session.status` may close the local turn early when they can be bound to the
  active turn.
- The `session/prompt` response remains the fallback completion boundary and
  reconciliation source when notifications arrive first.

If `session/prompt` never returns and no explicit terminal lifecycle evidence
arrives, the turn is still open from ACP's point of view, even if many
`session/update` notifications have arrived.

### Sparse official `session/load` responses

Official ACP `session/load` responses may be sparse.

- `null` should be treated as a missing-session failure.
- `{}` or other object responses without a session identifier should still be
  accepted when the caller already knows the requested `session_id`.

Do not require `sessionId` in official `session/load` success payloads unless
the protocol itself changes and the code/tests are updated together.

### Session-scoped turn rebinding

Hermes may omit `turnId` on official session-scoped streaming, approval, and
terminal lifecycle events.

- `session/update`, `session/request_permission`, and explicit terminal
  lifecycle events are rebound to the active local turn when they omit
  `turnId`.
- Busy `session.status` notifications must not be treated as terminal.

Do not broaden this fallback set unless the protocol requires it and the tests
prove the need.

## File responsibilities

| File | Responsibility |
|---|---|
| `client.py` | Transport, request/response tracking, official session protocol, prompt state |
| `events.py` | Notification normalization into CAR event types |
| `protocol.py` | Coercion/parsing of ACP initialize/session/prompt payloads |
| `supervisor.py` | Workspace-scoped client lifecycle and higher-level handle management |
| `errors.py` | ACP-specific error surface |

## Before changing behavior

Answer these questions explicitly:

1. Is the completion boundary still RPC-response-driven?
2. Are you changing official protocol interpretation, or just hardening around malformed
   adapters?
3. Does Hermes still need this fallback, or is it dead compatibility logic?
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

These tests cover real official-path failure modes:

- session-scoped updates rebinding onto the active turn
- session-scoped permission requests rebinding onto the active turn
- official `session/prompt` hang remaining non-terminal until RPC return
- official sparse `session/load` success payloads
- official `null` `session/load` failure payloads

## When integrating a new ACP-backed agent

Before relying on the shared ACP client, determine:

1. Does it advertise the official ACP session protocol (`agentInfo` +
   `agentCapabilities`)?
2. Does `session/prompt` always return terminally?
3. Are `session/load` responses sparse?
4. Does the agent emit any non-standard terminal notifications anyway?

Add a fixture scenario for any non-default behavior before wiring the agent into
Discord/Telegram/PMA surfaces.
