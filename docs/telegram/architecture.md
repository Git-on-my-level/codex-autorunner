# Telegram Architecture

## Overview

The Telegram integration is a polling bot that bridges Telegram chats to the Codex
app-server. It runs as a long-lived process (`car telegram start`) and uses the
Telegram Bot API to fetch updates, route them through CAR, and stream responses
back to Telegram. This is separate from the lightweight `notifications.telegram`
settings (which only send one-way notifications).

## Configuration and inputs

Config lives under `telegram_bot` in `codex-autorunner.yml` and the generated
`.codex-autorunner/config.yml`:

- `telegram_bot.enabled`: turn the bot on.
- `telegram_bot.bot_token_env`: env var name that holds the bot token.
- `telegram_bot.allowed_chat_ids`: allowlist of chat ids.
- `telegram_bot.allowed_user_ids`: allowlist of Telegram user ids.
- `telegram_bot.require_topics`: if true, only accept messages in forum topics.
- `telegram_bot.trigger_mode`: `all` (default) or `mentions` (only start runs when explicitly invoked).
- `collaboration_policy.telegram`: optional shared-chat policy overlay for
  topic/root destinations, modes, and per-destination plain-text triggers.
- `telegram_bot.parse_mode`: `HTML`, `Markdown`, `MarkdownV2`, or null.
- `telegram_bot.debug.prefix_context`: when true, prefix outgoing messages with routing metadata.
- `telegram_bot.app_server_command(_env)`: how to launch `codex app-server`.
- `telegram_bot.app_server`: app-server tuning (`max_handles`, `idle_ttl_seconds`, `turn_timeout_seconds`).
- `telegram_bot.media`: image/voice handling limits and prompts.
- `telegram_bot.shell`: `!<cmd>` settings (enable flag, timeouts, output limits).
- `telegram_bot.cache`: in-memory TTLs for pending approvals, selections, and progress state.
- `telegram_bot.defaults`: approval/sandbox defaults for the app-server client.

Required env vars are typically:

- `CAR_TELEGRAM_BOT_TOKEN`
- `CAR_TELEGRAM_CHAT_ID` (optional convenience for allowed chat ids)
- `CAR_TELEGRAM_APP_SERVER_COMMAND` (optional override)

The allowlist must include both chat ids and user ids or the bot will ignore
messages. Those filters intersect, and `collaboration_policy.telegram.destinations`
can further narrow root-chat vs topic behavior.

## Runtime flow

1) `car telegram start --path <repo_or_hub>` starts the polling loop.
2) `TelegramUpdatePoller` fetches updates from the Bot API.
3) Updates are admitted through the shared collaboration policy, then routed by
   chat/topic to a consistent durable CAR thread under the selected resource
   (repo workspace, PMA, or an agent-workspace-backed thread).
4) Commands (`/bind`, `/new`, `/resume`, `/approvals`, `/interrupt`) run locally;
   normal messages are forwarded to the Codex app-server. `!<cmd>` runs a shell
   command in the bound workspace (if enabled).
5) Responses are streamed back to Telegram with edits/chunks based on length.

## State and persistence

Per-chat/topic delivery state is stored in `.codex-autorunner/telegram_state.sqlite3`.
Authoritative binding and durable-thread metadata live in hub
`.codex-autorunner/orchestration.sqlite3`. Telegram keeps the transport-local
state it still owns, such as delivery bookkeeping, approval mode, and cached
topic routing details. Each forum topic (or chat root when topics are disabled)
has its own routing key.

## Security and multi-user expectations

There is no auth beyond Telegram ids plus the configured collaboration policy.
For multi-user use, explicitly add each user id and chat id to the allowlist and
use topic-level destinations to decide which topics are active, command-only, or
silent. The simplest setup is for each operator to create their own Telegram bot
token and run their own instance.

## Observability

The bot logs structured events (e.g. `telegram.update.received`,
`telegram.turn.completed`, `telegram.allowlist.denied`) to the hub log path
(default `.codex-autorunner/codex-autorunner-hub.log`). See
`docs/ops/telegram-bot-runbook.md` for troubleshooting.

## Quickstart (high level)

1) Create a Telegram bot token (BotFather) and decide which chat/topic to use.
2) Find your Telegram user id and the chat id you want to allow.
3) Set env vars (`CAR_TELEGRAM_BOT_TOKEN`, optional `CAR_TELEGRAM_CHAT_ID`).
4) Enable `telegram_bot.enabled` and set `allowed_user_ids`/`allowed_chat_ids`.
5) Run `car telegram start --path <repo_or_hub>` and send `/status` or `/help`.

## Migration guidance

- Existing DM or single-topic installs can keep the legacy `telegram_bot`
  allowlists and trigger settings unchanged.
- Shared supergroups should migrate to explicit
  `collaboration_policy.telegram.destinations` so root-chat vs topic behavior is
  no longer implicit.
- For shared groups, use `/ids` in the root chat and each topic, then gate the
  root with either `require_topics=true` or an explicit root destination such as
  `mode: silent`.
