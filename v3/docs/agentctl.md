# agentctl integration

CAR launches work through `agentctl` for anything it kicks off itself (triage
`run_action` templates, reply-back continuations — DESIGN.md §8 `agentctl-run`), and
separately wants to hear about *all* agentctl-tracked executions, including ones CAR
didn't launch. Two mechanisms, both DESIGN.md §2:

1. **`agentctl subscribe create`** — CAR registers a webhook subscription on each
   execution it cares about (mainly ones it launched itself); at-least-once delivery
   to CAR's ingest endpoint.
2. **`agentctl recent --unreconciled`** — a periodic belt-and-braces sweep for
   anything that fell through (execution finished, subscription never fired/was never
   created, host-local journal still has it).

## 1. Webhook subscription

Exact flags below are copied from `agentctl help subscribe` on this machine (Aug
2026) — confirm against your own `agentctl` install if it's a different version, since
CAR relies on the vendor CLI capability probe (DESIGN.md §2) rather than trusting
docs blindly:

```
agentctl subscribe create --execution ID --destination webhook --target target
  [--authority direct|multica] [--kind kind] [--ttl duration] [--keep-after-terminal]
```

Defaults (per `agentctl help subscribe`): a bare `create` filters to terminal,
attention, and artifact events; 24h TTL; expires once the terminal delivery is
acknowledged.

To subscribe CAR's ingest endpoint to a specific execution:

```bash
agentctl subscribe create \
  --execution exec-01ABCDEF... \
  --destination webhook \
  --target http://127.0.0.1:7171/v1/ingest/agentctl
```

Add `--kind` to narrow which semantic event kinds are delivered (see
`agentctl help events` for the current kind vocabulary — `terminal`, `attention`,
`artifact` are the ones this integration cares about; omit `--kind` to keep the
default filter). Add `--keep-after-terminal` if you want delivery to keep running
past the execution's terminal event (useful if you expect follow-up artifact events).

**When CAR creates the subscription itself**: for any execution CAR launches via
`agentctl run --label car-triage` or `agentctl run --label car-continuation`
(DESIGN.md §5, §8), the actions layer subscribes immediately after `run` returns the
execution id — no manual step needed. The manual invocation above is for wiring CAR
into agentctl work you launch **outside** CAR (your own `agentctl run` calls, CI, other
tooling) that you still want showing up in CAR's inbox.

## 2. Ingest side

CAR's `/v1/ingest/agentctl` normalizer maps an agentctl callback payload onto
`car.event.v1`:
- `session.vendor = "agentctl"`, `session.native_id` ← the execution id.
- `idempotency_key` includes the execution id + event sequence so at-least-once
  webhook delivery is safe to retry (duplicate POSTs resolve to the same event row —
  DESIGN.md §2).
- `kind: terminal` → `session.ended` (+ `attention.error` if the execution failed).
- `kind: attention` → `attention.question` / `attention.idle` depending on the
  specific attention reason agentctl reports.
- `kind: artifact` → `artifact`.
- If the execution wraps a *nested* native id (e.g. an agentctl exec wrapping a codex
  process), the normalizer also calls `linkSessionRef` so the codex session and the
  agentctl execution resolve to the same `car_session_id` (DESIGN.md §2, "session
  identity").

## 3. Unreconciled sweep

Belt-and-braces for subscriptions that never fired (process died before the webhook
went out, target was unreachable, etc.). Read-only discovery, safe to run on a timer:

```bash
agentctl recent --unreconciled
```

Per `agentctl help recent`: this lists terminal executions whose result has never been
acknowledged (older terminals that predate acknowledgement tracking are excluded, so
this won't flood you retroactively on a fresh agentctl install). CAR's watchdog
scheduler (DESIGN.md §4, loop 4) runs this sweep and files any execution it hasn't
already seen an event for as a synthetic `attention.idle`/`session.ended` — the same
path the silence watchdog uses for missed heartbeats.

## Reply-back: `agentctl-run`

The other direction — CAR answering into agentctl-launched work — is a
`response_channel.kind: "agentctl-run"`: replying to a session backed by an agentctl
execution is a **new bounded execution**, not an injection into a live process:

```bash
agentctl run --label car-continuation -- <vendor resume argv from session_refs>
```

CAR immediately `subscribe create`s on the new execution (step 1, above) so its
outcome flows back into the same incident. See DESIGN.md §8 for the full adapter list
and `docs/replies-file-contract.md` for the universal fallback when no adapter can
reach a session at all.
