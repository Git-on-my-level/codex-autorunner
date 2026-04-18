# Shared Chat UX Runtime Foundation

This document defines the repo-local source of truth for the shared chat-surface
UX runtime that Telegram, Discord, and future chat transports will converge on.
It is intentionally aligned with `docs/ARCHITECTURE_BOUNDARIES.md`:

- durable operation truth belongs to the control plane under
  `src/codex_autorunner/core/orchestration/`
- shared chat UX semantics belong to the adapter layer under
  `src/codex_autorunner/integrations/chat/`
- transport-specific modules continue to own protocol parsing, delivery, and
  platform limitations only

This document started as the foundation contract for the shared chat UX
initiative. Later tickets did change runtime behavior, but they are expected to
stay within the ownership boundaries and state-machine contract documented here.

## Scope

The shared chat UX runtime covers the user-visible lifecycle for one inbound
chat operation after a Telegram or Discord event has been normalized into the
shared chat adapter contract.

In scope:

- one shared state machine for chat operation progress
- one control-plane boundary for durable operation snapshots
- one adapter-layer metadata contract for presentation semantics
- module ownership rules for follow-on implementation tickets

Out of scope:

- changing current Discord or Telegram behavior
- migrating existing per-platform stores in this ticket
- adding new persistence tables or recovery loops in this ticket
- changing web or CLI surfaces

## Problem Statement

CAR already has a strong shared chat adapter contract for normalized events,
transport capabilities, and command dispatch. What it does not yet have is a
single cross-platform contract for "what state is this user-visible operation
in right now?".

That gap causes three long-term risks:

- Telegram and Discord can drift in how they describe queued, blocked, running,
  and terminal work.
- Recovery logic can end up depending on transport-local placeholders instead
  of control-plane truth.
- Future UX work can accidentally place durable state in the adapter layer.

This foundation closes that gap by splitting responsibilities cleanly.

## Ownership Boundaries

### Control Plane ownership

The control plane owns durable chat operation truth. Future persistence and
recovery must be satisfiable from control-plane rows plus existing managed
thread and execution records.

Authoritative modules:

- `src/codex_autorunner/core/orchestration/chat_operation_state.py`
  State enum, allowed transitions, durable snapshot shape, and store boundary
- existing orchestration records under `src/codex_autorunner/core/orchestration/`
  Managed threads, execution history, runtime bindings, and runtime outcomes

Control-plane rules:

- state names and transition rules are authoritative here
- adapter code may render these states, but must not redefine them
- transport-local placeholders are mirrors only, never the recovery source of
  truth

### Adapter ownership

The adapter layer owns cross-platform chat UX semantics derived from the
control-plane state machine.

Authoritative modules:

- `src/codex_autorunner/integrations/chat/ux_contract.py`
  Shared presentation metadata for state labels, phases, spinner policy, and
  user-facing affordances
- existing adapter contracts under `src/codex_autorunner/integrations/chat/`
  Normalized event, renderer, transport, and callback contracts

Adapter rules:

- adapters translate control-plane states into consistent UX behavior
- adapters may add transport-specific delivery context, but that context must
  not become durable state authority
- Telegram and Discord may format or rate-limit differently, but they must
  describe the same shared operation state

### Transport-specific ownership

Telegram and Discord continue to own:

- inbound protocol parsing
- ack/defer mechanics
- message editing, attachments, and callback delivery
- platform-specific limitations such as payload sizes and threading models

Telegram and Discord do not own:

- authoritative operation lifecycle state
- cross-platform state names
- durable recovery truth for managed-thread execution progress
- durable managed-thread final-delivery intent creation, claims, or retry state

## Shared State Machine

The shared chat UX runtime uses one control-plane state machine per
surface-visible operation.

### States

| State | Meaning | Typical source |
| --- | --- | --- |
| `received` | A normalized chat event has been admitted and assigned an operation id. | shared ingress |
| `acknowledged` | The transport-specific ack, defer, or equivalent immediate acceptance step completed. | adapter immediate-feedback path |
| `visible` | The user has a visible placeholder, anchor, or equivalent progress affordance. | adapter immediate-feedback path |
| `routing` | CAR is resolving the target, busy policy, and execution path. | orchestration ingress |
| `blocked` | Work is waiting on explicit user or operator input, such as approval or a required answer. | orchestration + chat handlers |
| `queued` | Work has been accepted but is waiting for execution ownership. | orchestration queue |
| `running` | Execution is active and may emit progress. | runtime-backed worker |
| `interrupting` | An interrupt request has been accepted, but execution has not yet settled into a terminal state. | adapter + orchestration coordination |
| `delivering` | Execution finished and CAR is finalizing the visible reply or terminal artifact. | adapter delivery path |
| `completed` | Work finished successfully and terminal delivery succeeded. | control plane |
| `interrupted` | Work stopped due to an explicit interrupt or equivalent cancellation of active execution. | control plane |
| `failed` | Work reached a non-recoverable error after admission. | control plane |
| `cancelled` | Work was cancelled before successful completion, usually while pending or queued. | control plane |

### Allowed transitions

The state machine is intentionally conservative:

- `received -> acknowledged | visible | routing | queued | running | interrupting | cancelled | failed`
- `acknowledged -> visible | queued | running | interrupting | delivering | completed | interrupted | failed | cancelled`
- `visible -> queued | running | interrupting | delivering | completed | interrupted | failed | cancelled`
- `routing -> acknowledged | visible | blocked | queued | running | interrupting | failed | cancelled`
- `blocked -> routing | queued | failed | cancelled`
- `queued -> visible | running | interrupting | interrupted | failed | cancelled`
- `running -> blocked | visible | delivering | interrupting | completed | interrupted | failed`
- `interrupting -> delivering | interrupted | failed | cancelled`
- `delivering -> visible | completed | interrupted | failed | cancelled`
- `completed`, `interrupted`, `failed`, and `cancelled` are terminal

Notes:

- `acknowledged` and `visible` are separate by design. Some transports can ack
  immediately but create the visible placeholder later, and recovery must not
  collapse those events into one step.
- `blocked` is the shared state for approval waits, questions, or other
  explicit user actions. Adapters can render different controls, but they must
  map back to the same shared state.
- `delivering` exists because reply delivery can fail independently of business
  execution. Future recovery work may retry this phase without replaying the
  entire execution.

## Durable Snapshot Contract

Each state-machine instance is represented by one control-plane snapshot.

Required durable fields:

- `operation_id`
- `surface_kind`
- `surface_operation_key`
- `thread_target_id`
- `state`

Expected bridge fields for future tickets:

- `conversation_id`
- `execution_id`
- `backend_turn_id`
- `status_message`
- `blocking_reason`
- `ack_completed_at`
- `first_visible_feedback_at`
- `anchor_ref`
- `delivery_state`
- `delivery_cursor`
- `updated_at`
- adapter-safe metadata for display hints that can be recomputed or ignored

The snapshot must stay small and authoritative. It is not a replacement for
execution history or transcript mirrors.

## Module Plan

The implementation path for future tickets is constrained to the following
modules.

### Phase 0: foundation

- `src/codex_autorunner/core/orchestration/chat_operation_state.py`
  Introduce the authoritative enum, transition table, snapshot dataclass, and
  store protocol
- `src/codex_autorunner/core/orchestration/chat_operation_ledger.py`
  Provide the first orchestration-backed store implementation and recovery
  planning boundary without moving lifecycle authority out of the control plane
- `src/codex_autorunner/integrations/chat/ux_contract.py`
  Introduce shared presentation metadata derived from the control-plane states
- `src/codex_autorunner/integrations/chat/immediate_feedback.py`
  Use the shared control-plane states when adapters ack, create anchors, queue,
  and interrupt, while keeping transport mechanics local

### Phase 1: persistence wiring

- add a concrete control-plane implementation behind the new store protocol
- persist operation snapshots alongside existing orchestration data
- ensure startup recovery can reconstruct visible progress from control-plane
  state rather than transport-local placeholders

### Phase 2: adapter adoption

- teach shared chat runtime paths to create and advance operation snapshots
- update Telegram and Discord adapter flows to render from shared UX metadata
- keep transport-specific rate limiting and delivery retries local

### Phase 3: surface parity and diagnostics

- expose shared operation state to web or CLI surfaces where useful
- add diagnostics that compare adapter-visible status against control-plane
  snapshots
- document any transport-specific exceptions explicitly

## Boundary Check

This foundation respects `docs/ARCHITECTURE_BOUNDARIES.md`:

- the new control-plane module imports only stdlib types
- the new adapter module depends on the control-plane enum but not vice versa
- no surface-layer modules are introduced
- no transport package becomes a durable state owner

Future tickets must continue to pass:

```bash
.venv/bin/python -m pytest tests/test_architecture_boundaries.py -q
```
