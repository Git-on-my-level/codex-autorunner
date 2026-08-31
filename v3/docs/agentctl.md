# agentctl integration

CAR treats agentctl as a transport and execution authority, not as the identity
of the native agent. Cursor, OMP, Codex, and other adapters remain distinct in
the operator UI while routing and policy continue to use the authenticated
agentctl source identity.

There are two independent integration paths:

1. Callback ingest receives semantic events from explicit agentctl webhook
   subscriptions.
2. The optional observer builds a compact, read-only projection of selected
   local executions for the Runs view.

Neither path calls `agentctl result` or `agentctl await`. Reading CAR must never
acknowledge or consume another client's agentctl result.

## Callback ingest

`POST /v1/ingest/agentctl` accepts authenticated callback deliveries.
Continuation subscriptions use an execution-scoped capability URL because the
agentctl webhook client cannot attach a bearer header.

The normalizer:

- keeps `source.vendor = "agentctl"` and the agentctl execution as the session
  ref;
- preserves the native adapter in `payload.agentctl.adapter` for display;
- uses the stable journal event id, or execution and sequence fallback, for
  at-least-once idempotency;
- maps attention, artifact, terminal, health, and progress into the closed CAR
  event vocabulary;
- stores model, profile, runtime, config fingerprint, or worktree only when the
  callback actually reports them.

An agentctl callback does not contain a trustworthy native resume id. CAR must
not turn opaque source-binding aliases into native session refs.

CAR currently creates a subscription only after it launches a bounded
`car-continuation` execution. Subscription creation is best-effort and audited.
External subscriptions must target an authenticated execution-scoped callback
URL; the bare ingest URL requires a configured bearer credential.

## Optional local observer

The daemon-owned observer is off by default so the no-dependency native CAR
configuration stays valid. Enable it with an explicit scope:

```toml
[agentctl_observer]
enabled = true
required_labels = ["car-observe"]
observe_all = false
interval_seconds = 15
discovery_limit = 100
retention_days = 30
retention_max_terminal = 2000
```

`discovery_limit` is bounded to `1..200`, matching agentctl's public `recent`
limit. Values above 200 are rejected at configuration load instead of creating
false confidence about discovery breadth.

To observe every host-local execution, make that breadth explicit:

```toml
[agentctl_observer]
enabled = true
required_labels = []
observe_all = true
```

Each tick reads two bounded views:

- recent matching executions;
- matching nonterminal executions, so active work cannot fall out of the recent
  window.

CAR-launched continuations carry both `car-continuation` and `car-observe`, so
the default observer scope includes them. External work must opt in with the
exact `car-observe` label (or another configured scope). A descriptive label
becomes the compact Runs title; `title:<slug>` wins when callers need an
explicit display name. Labels remain discovery metadata and must not contain
prompts, results, or secrets.

The projection records native agent, lifecycle, liveness, labels, duration, and
only metadata agentctl reports. High-volume progress is compacted into the run
row instead of flooding Inbox. A truncated active query becomes visible
incomplete-coverage health. A truncated recent query means only that older
terminal history is partial; it does not make the active count suspect. Missing
agentctl degrades only this optional observer.

Nonterminal rows are never removed by projection cleanup. Terminal rows are
bounded by age and count because agentctl remains lifecycle authority; cleanup
is audited and cannot erase unresolved work.

`recent --unreconciled` is deliberately not used. In agentctl, unreconciled
means a terminal result has not been collected; it is not evidence that a
callback was missed, and another client can change that set by collecting the
result.

The Runs page is a read model. Agentctl remains lifecycle authority.
It refreshes while visible, pins active work ahead of paginated terminal
history, and treats missing or unrefreshed nonterminal rows as last-seen
evidence rather than current liveness.

## Reply-back honesty

`response_channel.kind = "agentctl-run"` means CAR may try a new bounded native
continuation. It does not prove that continuation is possible.

Today the adapter can construct native resume commands only when the CAR
session has a verified Codex or Claude Code ref. Cursor and OMP executions do
not provide a verified resume ref through agentctl; their replies are recorded
and staged through the replies-file fallback. The incident UI states this
boundary explicitly instead of claiming Telegram resumed the agent.
