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
- **Codex app-server**: The bot proxies messages to a local Codex app-server
  process that executes turns and tools on the host.
- **Local filesystem**: The bot reads/writes state and downloads media into the
  bound workspace.

## Authentication and allowlists

- The bot requires both `allowed_user_ids` and `allowed_chat_ids`. If either is
  empty, it refuses to handle messages.
- Allowlists are enforced for both messages and callback queries.
- `telegram_bot.require_topics` can force use of forum topics to avoid accidental
  handling in the root chat.

## Execution surface

- Normal messages are forwarded to the Codex app-server for tool execution.
- `/approvals` controls the approval mode and policies per topic.
- The default approval mode is `yolo`, which is equivalent to:
  - `approval_policy = never`
  - `sandbox_policy = dangerFullAccess`
- If `telegram_bot.shell.enabled` is true, `!<cmd>` runs `bash -lc` in the bound
  workspace through the app-server.
- `/update` triggers an update workflow that can restart services and pull code
  from a remote repo.

## Workspace binding

- `/bind <repo_id|path>` lets an allowed user bind a topic to a workspace.
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

- Per-topic state is stored in `.codex-autorunner/telegram_state.json`, including
  workspace paths, thread IDs, and preview snippets.
- Logs include chat IDs, user IDs, and event metadata; review your log retention
  and access controls accordingly.

## Recommendations

- Treat Telegram as a convenience interface, not a secure enclave.
- Keep allowlists narrow and avoid adding group chats unless you want every
  member to have bot access.
- Prefer `approval_mode = safe` and a restrictive sandbox for day-to-day use.
- Disable `telegram_bot.shell.enabled` unless you explicitly need `!<cmd>`.
- Use per-user bot tokens for multi-operator setups when possible.
- Monitor logs for `telegram.allowlist.denied` and `telegram.turn.failed` events.

## References

- `docs/telegram/architecture.md`
- `docs/ops/telegram-bot-runbook.md`
