# Real-work dogfood: Cursor and OMP through agentctl

Date: 2026-08-30
Scope: repository-read-only investigations launched through the installed `agentctl` adapters. No CAR daemon, launchd service, production system, or repository implementation was changed. Retrieving each terminal result acknowledged it in agentctl's local operational state, as noted below.

> Implementation status: the gaps below describe the pre-fix build that was
> dogfooded. Round 3 added an opt-in, scoped, read-only observer projection, a
> compact Runs page, native Cursor/OMP display identity, and truthful reply
> capability copy. The original evidence is preserved here as the reason for
> those changes.

## Executive finding

The local agents completed useful work, but CAR v3 would not currently give an operator a trustworthy view of those runs. The most important gap is not visual styling: external `agentctl` work has no implemented automatic path into CAR. If a callback is present, the normalizer preserves the native adapter only inside an unrendered JSON payload while the UI labels both Cursor and OMP as **Agentctl**. Model/profile, repository, useful progress, result content, and recovery state are absent or hidden.

The product should model an **agent run** as a first-class read model, then let Inbox show only the run transitions that deserve attention. Feeding raw native progress into the current event table would create noise rather than observability: the OMP run generated 3,879 agentctl revisions during a five-minute read-only investigation.

## Runs performed

Preflight:

```sh
agentctl doctor
agentctl help run
agentctl help fanout
```

`agentctl doctor` reported both `cursor` and `omp` ready. Cursor advertised supported launch/events/result content; OMP advertised supported launch/result content and degraded same-process events. The journal and callback supervisor were healthy.

### Cursor

```sh
agentctl run --timeout 8m --label car-v3-dogfood --label cursor -- cursor-agent --print --output-format stream-json --mode ask --trust "Read-only investigation in this repository. A user reports that an externally launched agentctl task completed but is missing from the CAR v3 Inbox. Trace the implemented path from agentctl execution/subscription or reconciliation through CAR ingest, persistence, and web queries. Compare shipped docs to executable code. Do not edit files or run mutating commands. Return a concise verdict with exact file/line evidence, the likely real-world failure mode, and safe read-only verification commands."
```

- Execution: `exec-spot-below-juice-salmon-turtle-dilemma`
- Adapter/source: Cursor; backend `2026.08.25-3e8eec8`
- Started: `2026-08-31T00:26:44.703802Z`
- Terminal: completed at `2026-08-31T00:28:44.646347Z` (about 2 minutes)
- Final agentctl revision: 591

### OMP

```sh
agentctl run --timeout 8m --label car-v3-dogfood --label omp -- omp -p --mode json "Read-only investigation in this repository. A user runs Cursor and OMP tasks concurrently in the same repo and expects CAR v3 to show which agent is doing what, its model/profile, meaningful progress, attention requests, terminal outcome, and recovery state. Trace the implemented agentctl ingest normalizer, session/event persistence, web queries, and rendered views. Do not edit files or run mutating commands. Report exactly which identity and lifecycle fields are preserved versus hidden or conflated, with file/line evidence and severity-ranked operator UX gaps."
```

- Execution: `exec-flavor-catch-enlist-shoot-title-near`
- Adapter/source: OMP; backend `omp/17.4.0`
- Started: `2026-08-31T00:29:38.040849Z`
- Terminal: completed at `2026-08-31T00:35:14.078597Z` (about 5 minutes 36 seconds)
- Final agentctl revision: 3,879

I inspected both runs with `agentctl recent --state nonterminal --label car-v3-dogfood`, `agentctl status <execution-id>`, `agentctl events <execution-id>`, and finally `agentctl result <execution-id>`. Fetching each result acknowledged that local journal result; it did not change repository or CAR state.

## What real work exposed

### 1. The operator gets no useful live progress

Both foreground `agentctl run` commands produced no visible output until the terminal JSON envelope, although `agentctl status` showed the executions alive and changing. The OMP execution moved through `turn_start`, `message_update`, and `tool_execution_start`, but the default 100-event page was almost entirely token/message-level progress from the first seconds of work. This is too granular for a human feed and too opaque when hidden.

CAR's mapping confirms the problem: progress becomes a title containing only `source_state` and an empty body (`v3/src/ingest/agentctl.ts:271-289`). The Inbox renders every stored event as a row (`v3/src/surfaces/web/queries.ts:37-77`, `v3/src/surfaces/web/views.tsx:48-72`). A raw integration would therefore oscillate between silence and thousands of low-value rows.

**Adopt:** compact native activity into one durable run row with status, elapsed time, latest semantic phase, last meaningful update, and a small activity sparkline/count. Keep raw events behind a technical disclosure.

### 2. External local agent work is not actually observed

The shipped integration document says CAR hears about all agentctl-tracked executions and sweeps `agentctl recent --unreconciled` (`v3/docs/agentctl.md:3-13,70-84`). The daemon loop list contains no agentctl observer or sweep (`v3/src/daemon.ts:103-117`), and the only source reference to `unreconciled` is a comment in the continuation adapter. Subscription is created only after a CAR-launched continuation (`v3/src/actions/adapters/agentctl.ts:121-159`).

The live preview at `http://127.0.0.1:7194/ui` remained seeded and showed neither execution ID. There was no real CAR daemon listening on the default port, so I did not create a callback subscription or pretend this was an end-to-end ingest test.

**Adopt:** implement one explicit observation authority. Either maintain scoped subscriptions for every opted-in agentctl execution, or run the documented bounded reconciliation loop. Surface observer health, last sweep, and missed-callback recovery in the UI. Do not leave docs claiming an authority the daemon does not have.

### 3. Cursor and OMP collapse into the same visible identity

The callback normalizer hardcodes `vendor: "agentctl"` and stores the native adapter only under `payload.agentctl.adapter` (`v3/src/ingest/agentctl.ts:98-162`). Inbox selects `source_adapter`, but renders only `source_vendor` as the **Agent** column (`v3/src/surfaces/web/queries.ts:66-75`, `v3/src/surfaces/web/views.tsx:68-69`). The adapter value in `source_adapter` is itself the transport name `agentctl-subscribe`, not Cursor or OMP. The source filter offers Cursor and OMP, but those values cannot match agentctl-normalized rows.

Agentctl itself knew the distinction: status showed `adapter: cursor` versus `adapter: omp`, separate native session bindings, and different backend versions. CAR currently hides that useful identity.

**Adopt:** separate transport from worker identity:

- Transport: agentctl
- Agent: Cursor or OMP
- Runtime/model/profile: recorded when known; explicitly “not reported” otherwise
- Execution: stable agentctl execution ID
- Scope: repository/worktree, host, and user-selected profile

Show agent + run state together in lists (for example, `OMP · Running 5m`) and keep the opaque execution ID available on detail, not as the primary label.

### 4. Repository and model/profile are missing

The normalizer looks for top-level `cwd` and `repo` (`v3/src/ingest/agentctl.ts:136-142`), but the observed journal events carried neither. Agentctl status exposed adapter/backend metadata but no selected model/profile. The CAR event/session contract has no first-class model or profile for the observed worker. Repo filtering therefore cannot reliably find these runs, and a continuation can fall back to the daemon's working directory because session `cwd` is absent (`v3/src/actions/adapters/agentctl.ts:90-103`).

**Adopt:** capture launch metadata at the execution boundary rather than hoping every callback repeats it. Persist a configuration fingerprint plus human-readable agent/profile, cwd, canonical repo, and worktree. Never infer the model from the adapter name.

### 5. Attention can be displayed even when CAR cannot continue the worker

Agentctl attention maps to `requires_response: true` and an `agentctl-run` response channel (`v3/src/ingest/agentctl.ts:233-246`). The incident screen then tells the user to reply in Telegram (`v3/src/surfaces/web/views.tsx:88-107`). The actual continuation adapter supports only Codex and Claude references; Cursor and OMP fall back because they have no resume argv (`v3/src/actions/adapters/agentctl.ts:25-39,75-85`).

**Adopt:** make the response affordance capability-aware. Show “Reply and continue” only when the current run has a verified continuation route. Otherwise show the supported next action plainly (open native session, copy response, or stage a replies-file entry) and never imply Telegram will reach the agent.

### 6. Terminal and recovery truth are too thin

Terminal callbacks preserve failure code/reason for bad states but discard successful result content (`v3/src/ingest/agentctl.ts:214-230`). There is no implemented missed-terminal reconciliation, and session lifecycle state is not transitioned by production code. A successful run becomes another low-value event; a lost callback may leave no CAR evidence at all.

**Adopt:** store terminal outcome before optional result retrieval, show a short result/artifact summary when available, and represent `possibly finished`, `callback missed`, `stale`, and `recovered` separately. A run detail should answer: what happened, did CAR observe it durably, and what evidence is missing?

## Severity-ranked recommendations

### P0 — required for honest dogfooding

1. Implement and health-report the external agentctl observation/reconciliation path, or narrow the product claim.
2. Add a first-class run read model that preserves transport, native agent, execution, scope, model/profile, lifecycle, and terminal evidence.
3. Render Cursor and OMP as distinct agents; make source/agent filters match stored semantics.
4. Gate reply controls on a verified continuation capability; do not promise Telegram reply-back for Cursor/OMP today.

### P1 — makes the product feel operational rather than log-like

5. Aggregate progress into semantic phases and activity density; do not create a row per native message update.
6. Add an Active work section or Runs page: agent, task/title, repo, elapsed time, latest phase, attention state, and last update.
7. Persist terminal outcomes and recovery evidence; transition session state from active to ended/stale/recovered.
8. Capture launch scope and model/profile once, then carry the immutable fingerprint through every callback.

### P2 — final polish

9. Use friendly host/profile labels in normal views; put opaque host and execution IDs in technical details.
10. In a run detail, compare requested agent/profile with the observed backend and mark missing identity fields explicitly.
11. Summarize high-frequency native activity visually (phase steps, counts, or a small timeline) while retaining raw event replay for diagnostics.

## Cross-agent comparison

| Dimension | Cursor run | OMP run | What CAR should communicate |
|---|---|---|---|
| Identity available to agentctl | `adapter: cursor`, Cursor session binding | `adapter: omp`, OMP session binding | Distinct agent names, same agentctl transport |
| Runtime metadata | backend build, no model/profile | `omp/17.4.0`, no model/profile | Show known runtime; say model/profile not reported |
| Duration | about 2m | about 5m36s | Elapsed/terminal time without reading raw events |
| Activity volume | 591 revisions | 3,879 revisions | One compact semantic run, not hundreds of Inbox rows |
| Foreground experience | blank until terminal | blank until terminal | Live run state and last meaningful activity |
| CAR preview visibility | absent | absent | Honest observer/disconnected state, then scoped run visibility |

## Verification limits

- This was real local Cursor and OMP work through agentctl, but not an end-to-end CAR callback exercise because only the deterministic preview was running. Starting or reconfiguring the user's persistent daemon was outside this read-only audit.
- I did not induce a permission request, crash, timeout, lost callback, or restart. Attention and recovery findings are implementation traces, not live failure demonstrations.
- The preview uses seeded in-memory state; absence there proves it is not wired to these executions, not that a correctly configured daemon could never ingest a manually subscribed callback.
- The repository was concurrently changing during the audit. Findings were rechecked against the current files immediately before writing; exact line numbers may drift as the polish implementation continues.
