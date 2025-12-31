# Telegram Bot Runbook

## Purpose

Operate and troubleshoot the Telegram polling bot that proxies Codex app-server sessions.

## Prerequisites

- Set env vars in the bot environment:
  - `CAR_TELEGRAM_BOT_TOKEN`
  - `CAR_TELEGRAM_CHAT_ID`
  - `OPENAI_API_KEY` (or the Codex-required key)
- Configure `telegram_bot` in `codex-autorunner.yml` or `.codex-autorunner/config.yml`.
- Ensure `telegram_bot.allowed_user_ids` includes your Telegram user id.

## Start

- `car telegram start --path <hub_root>`
- On startup, the bot logs `telegram.bot.started` with config details.

## Verify

- In the target topic, send `/status` and confirm the workspace and active thread.
- Send `/help` to confirm command handling.
- Send a normal message and verify a single agent response.

## Common Commands

- `/bind <repo_id|path>`: bind topic to a workspace.
- `/new`: start a new Codex thread for the bound workspace.
- `/resume`: list recent threads and resume one.
- `/interrupt`: stop the active turn.
- `/approvals yolo|safe`: toggle approval mode.

## Logs

- Primary log file: `config.log.path` (default `.codex-autorunner/codex-autorunner.log`).
- Telegram events are logged as JSON lines with `event` fields such as:
  - `telegram.update.received`
  - `telegram.send_message`
  - `telegram.turn.completed`
- App-server events are logged with `app_server.*` events.

## Troubleshooting

- No response in Telegram:
  - Confirm `CAR_TELEGRAM_BOT_TOKEN` and `CAR_TELEGRAM_CHAT_ID` are set.
  - Confirm `telegram_bot.allowed_user_ids` contains your user id.
  - Confirm the topic is bound via `/bind`.
- Updates ignored:
  - If `telegram_bot.require_topics` is true, use a topic and not the root chat.
  - Check `telegram.allowlist.denied` events for chat/user ids.
- Turns failing:
  - Check `telegram.turn.failed` and `app_server.*` logs.
  - Verify the Codex app-server is installed and in `telegram_bot.app_server_command`.
- Approvals not appearing:
  - Ensure `/approvals safe` is set on the topic.

## Stop

- Stop the process with Ctrl-C. The bot closes the Telegram client and app-server.
