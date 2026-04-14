# Glossary

## Core Concepts

- **Engine**: protocol-agnostic runtime semantics (runs, scheduling, state transitions).
- **Control plane**: filesystem-backed intent + artifacts; canonical state under `.codex-autorunner/`.
- **Adapter**: protocol translation layer (Telegram/Web/Codex/OpenCode/Hermes) into engine commands.
- **Surface**: user-facing UX (Telegram chat, web UI, terminal views).
- **Run**: a single execution with a unique identity and durable artifacts.
- **Run event**: structured record of a significant state transition/decision.
- **Artifact**: any durable file that explains intent, action, or output.
- **Ticket**: a numbered markdown work item under `.codex-autorunner/tickets/` (for example `TICKET-001.md`); the primary human–agent execution surface.
- **Contextspace**: durable agent context docs under `.codex-autorunner/contextspace/` (`active_context.md`, `decisions.md`, `spec.md`). Not the same as a disposable process working directory.
- **Workspace**: isolated filesystem scope for a task or runtime (often disposable), or the deprecated `.codex-autorunner/workspace/` directory replaced by contextspace (see migration doc).
- **YOLO mode**: default permissive execution posture; safety is opt-in.

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
