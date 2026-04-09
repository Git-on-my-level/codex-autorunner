# Telegram Bot Security Posture

This document describes the security surface and operational posture of the
interactive Telegram bot integration. It is intended for operators and agents
who want to understand what the bot can do, which controls exist, and the
tradeoffs involved.

## Scope and threat model

- The bot is a polling client for the Telegram Bot API. It is not a webhook and
  does not expose an inbound HTTP endpoint.
- Access control is allowlist-based. There is no additional auth layer beyond
  Telegram chat/user IDs.
- The bot can run Codex turns and, optionally, shell commands within a bound
  workspace. This can result in code execution on the host.
- Telegram bots do not have end-to-end encryption. Treat Telegram as a
  transport, not a secret store.

## Trust boundaries

- **Telegram Bot API**: All messages and media originate from Telegram. The bot
  trusts Telegram to authenticate users, but still enforces allowlists.
- **Orchestration-managed thread targets**: Ordinary chat turns are admitted
  through CAR orchestration first; that layer owns durable thread identity,
  queueing, interruption, and recovery.
- **Backend runtime**: After orchestration resolves the thread target, CAR uses
  the selected runtime (for example Codex app-server) to execute the turn and
  tools on the host.
- **Local filesystem**: The bot reads/writes state and downloads media into the
  bound workspace.

## Authentication and allowlists

- The bot requires both `allowed_user_ids` and `allowed_chat_ids`. If either is
  empty, it refuses to handle messages.
- Those actor and chat filters intersect. Adding a group chat id does not grant
  every member access by itself; the sender must still be in `allowed_user_ids`.
- `collaboration_policy.telegram.destinations` can further narrow behavior by
  topic or root chat and assign explicit destination modes:
  - `active`
  - `command_only`
  - `silent`
  - `denied`
- Allowlists are enforced for both messages and callback queries.
- `telegram_bot.require_topics` or an explicit root-chat destination can keep
  the group root from reaching CAR.

## Execution surface

- Normal messages are routed through shared orchestration ingress and then
  delivered to the resolved managed thread target for tool execution.
- `/approvals` controls the approval mode and policies per topic.
- The default approval mode is `yolo`, which is equivalent to:
  - `approval_policy = never`
  - `sandbox_policy = dangerFullAccess`
- If `telegram_bot.shell.enabled` is true, `!<cmd>` runs `bash -lc` in the bound
  workspace through the app-server.
- `/update` triggers an update workflow that can restart services and pull code
  from a remote repo.

## Workspace binding

- `/bind <repo_id|path>` lets an allowed user bind a topic to a repo workspace
  root and lets CAR keep a consistent durable thread for that topic.
- Paths can be absolute or relative to the configured root. If the path exists,
  it can be bound even if it is not a Git repo.
- Once bound, the bot can read files (e.g., `/mention`) and run commands in that
  workspace, subject to approvals/sandbox policy.

## Media handling

- Images and voice notes are downloaded from Telegram and stored under the bound
  workspace:
  - `.codex-autorunner/uploads/telegram-images/`
  - `.codex-autorunner/uploads/telegram-voice/`
- Media size limits are enforced, but contents are untrusted input.
- Voice notes are transcribed by the configured voice provider and then handled
  like normal text input.

## Data at rest and logs

- Per-topic transport state is stored in `.codex-autorunner/telegram_state.sqlite3`.
  Authoritative binding and durable-thread metadata live in hub
  `.codex-autorunner/orchestration.sqlite3`. Telegram state remains transport
  convenience state, not authoritative routing state.
- Logs include chat IDs, user IDs, and event metadata; review your log retention
  and access controls accordingly.

## Recommendations

- Treat Telegram as a convenience interface, not a secure enclave.
- Keep allowlists narrow and explicitly add only the users who should be able to
  talk to CAR.
- For shared supergroups, prefer topic-level destinations plus a silent or
  denied root-chat destination.
- Prefer `approval_mode = safe` and a restrictive sandbox for day-to-day use.
- Disable `telegram_bot.shell.enabled` unless you explicitly need `!<cmd>`.
- Use per-user bot tokens for multi-operator setups when possible.
- Monitor logs for `telegram.allowlist.denied` and `telegram.turn.failed` events.

## Migration guidance

- Legacy personal setups still use the same allowlists, but a DM or dedicated
  topic now needs `/bind` or `/pma on` before plain-text turns will run.
- Shared groups should migrate when they need intentional topic behavior. The
  security win is explicit scope: only the topics marked `active` or
  `command_only` can reach CAR, and the root chat can be made `silent` or
  topic-only.

## References

- `docs/telegram/architecture.md`
- `docs/ops/telegram-bot-runbook.md`
