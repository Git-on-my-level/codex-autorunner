# Glossary

## Core Concepts

- **Attention router**: CAR v3 core; durable cross-vendor event, incident, human
  handoff, grant, effect, delivery, recovery, digest, watchdog, and audit semantics.
- **Capability provider**: replaceable implementation of one or more of `operator`,
  `policy`, and `memory`. A provider proposes or advises; it does not execute effects or
  own core lifecycle.
- **Operator provider**: proposes an incident disposition and typed effects.
- **Policy provider**: supplies contextual policy advice for a proposed effect. Core
  safety and grants remain authoritative.
- **Memory provider**: supplies scoped context and observes human/decision outcomes in
  provider-owned state.
- **Effect**: typed externally visible work proposed to CAR and authorized, persisted,
  executed, and audited by core.
- **Grant**: core-owned human authorization for a constrained effect and scope. Grants
  may be one-shot or reusable and may expire or be revoked.
- **Receipt**: durable per-channel record of delivery state, including queued,
  delivered, uncertain, failed, suppressed, no-target, abandoned, superseded, and
  expired outcomes.
- **Adapter**: translation between an external source/destination/provider transport and
  a core contract; it does not own routing policy.
- **Surface**: user-facing projection and input UX (Telegram, Web, later Discord); it
  does not own lifecycle or provider state.
- **Provider instance**: one resolved provider/profile/topology identity used for a
  capability invocation, such as `hermes:work`.

## Deprecated v2 concepts

- **Engine**: v2 protocol-agnostic runtime semantics (runs, scheduling, state transitions).
- **Control plane**: v2 filesystem-backed intent and artifacts under `.codex-autorunner/`.
- **Run**: a single execution with a unique identity and durable artifacts.
- **Run event**: structured record of a significant state transition/decision.
- **Artifact**: any durable file that explains intent, action, or output.
- **Ticket**: a numbered markdown work item under `.codex-autorunner/tickets/` (for example `TICKET-001.md`); the primary human–agent execution surface.
- **Contextspace**: durable agent context docs under `.codex-autorunner/contextspace/` (`active_context.md`, `decisions.md`, `spec.md`). Not the same as a disposable process working directory.
- **Workspace**: legacy term for isolated filesystem scope; the `.codex-autorunner/workspace/` directory was replaced by contextspace (see migration doc). Use "contextspace" for new work.
- **YOLO mode**: deprecated v2 permissive execution posture. It is not a v3 effect policy.

## Lifecycle Terms

- **Retire**: operator-facing lifecycle action that closes out live CAR state after preserving reviewable evidence. Use this word in commands, UI labels, runbooks, and help text when the user is intentionally finishing a thread, flow run, worktree, or local CAR state.
- **Retirement snapshot**: retained evidence produced by a retire action. These snapshots are stored under `.codex-autorunner/archive/`; the storage path remains named `archive` even when the user-facing action is retire.
- **Archive**: storage/retention mechanism, not the operator action. Use this word for literal archive directories, archive readers, artifact retention internals, dispatch/reply history, app-server protocol methods that are externally named `thread/archive`, and config keys that describe archive retention.
- **Delete**: remove without preserving reviewable CAR artifacts. Delete commands must make the loss of preserved evidence explicit.
- **Purge**: cleanup engine wording for reclaiming stale state selected by policy. Prefer retire/delete in user-facing command names unless the operation is specifically policy-driven pruning.
- **Cleanup**: umbrella maintenance operation that may retire, delete, prune, or reap different state families. Cleanup docs and output should name the concrete action when possible.
- **Reset**: clear volatile or local state so work can start fresh. Reset is not a synonym for retire; use retire when preservation and closeout are part of the operation.

## Agent-Human Communication

- **Dispatch**: Agent-to-human communication written to the outbox. Contains mode, title, body, and optional attachments. The umbrella term for all agent→human messages.
  - `mode: "notify"`: Informational dispatch; agent continues working.
  - `mode: "pause"`: Handoff dispatch; agent yields and awaits human reply.
- **Handoff**: A dispatch with `mode: "pause"`. Represents transfer of control from agent to human.
- **Reply**: Human-to-agent response written to the reply outbox. Resumes agent execution.
- **Inbox**: UI view showing the timeline of dispatches and replies for a conversation.
- **Notification**: External alert sent to Discord/Telegram when system events occur (run finished, tui idle, etc.). Distinct from dispatches—notifications are delivery infrastructure, not agent communication.

## Filesystem Paths

Per run, under the repo-local runs tree (for example `runs/<run_id>/`):

- `dispatch/` → dispatch staging directory (attachments before archival)
- `dispatch_history/` → dispatch archive directory
- `DISPATCH.md` → dispatch file
