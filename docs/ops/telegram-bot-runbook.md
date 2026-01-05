# Telegram Bot Runbook

## Purpose

Operate and troubleshoot the Telegram polling bot that proxies Codex app-server sessions.

## Prerequisites

- Set env vars in the bot environment:
  - `CAR_TELEGRAM_BOT_TOKEN`
  - `CAR_TELEGRAM_CHAT_ID`
  - `OPENAI_API_KEY` (or the Codex-required key)
  - `CAR_TELEGRAM_APP_SERVER_COMMAND` (optional full command, e.g. `/opt/homebrew/bin/codex app-server`)
- If the app-server command is a script (ex: Node-based `codex`), prefer an absolute path so the bot can prepend its directory to `PATH` under launchd.
- Configure `telegram_bot` in `codex-autorunner.yml` or `.codex-autorunner/config.yml`.
- Ensure `telegram_bot.allowed_user_ids` includes your Telegram user id.
- Enable `telegram_bot.shell.enabled` if you want `!<cmd>` shell support.

## Start

- `car telegram start --path <hub_root>`
- On startup, the bot logs `telegram.bot.started` with config details.

## Verify

- In the target topic, send `/status` and confirm the workspace and active thread.
- Send `/help` to confirm command handling.
- Send a normal message and verify a single agent response.
- Send an image with an optional caption and confirm a response (image is stored under the bound workspace).
- Send a voice note and confirm it transcribes (requires Whisper/voice config).

## Common Commands

- `/bind <repo_id|path>`: bind topic to a workspace.
- `/new`: start a new Codex thread for the bound workspace.
- `/resume`: list recent threads and resume one.
- `/interrupt`: stop the active turn.
- `/approvals yolo|safe`: toggle approval mode.
- `/update [both|web|telegram]`: update CAR and restart selected services.
- `!<cmd>`: run a bash command in the bound workspace (requires `telegram_bot.shell.enabled`).

## Media Support

- Telegram media handling is controlled by `telegram_bot.media` (enabled by default).
- Images are downloaded to `<workspace>/.codex-autorunner/uploads/telegram-images/` and sent to Codex as `localImage` inputs.
- Voice notes are transcribed via the configured Whisper provider and sent as text inputs.
- Ensure `voice` configuration (and API key env) is set if you want voice note transcription.

## Logs

- Primary log file: `config.log.path` (default `.codex-autorunner/codex-autorunner.log`).
- Telegram events are logged as JSON lines with `event` fields such as:
  - `telegram.update.received`
  - `telegram.send_message`
  - `telegram.turn.completed`
- App-server events are logged with `app_server.*` events.
- Startup retries log `telegram.app_server.start_failed` with the next backoff delay.

## Troubleshooting

- Resume preview missing assistant message:
  - The app-server thread metadata only includes a single `preview` field (often the first user message).
  - The Telegram bot augments resume previews by reading the rollout JSONL path when available.
  - Rollout JSONL lines wrap content under a top-level `payload` key; ensure preview extraction descends into `payload`.
  - If the rollout path is unavailable (remote app-server), consider adding assistant preview fields to the app-server `Thread` schema.

- No response in Telegram:
  - Confirm `CAR_TELEGRAM_BOT_TOKEN` and `CAR_TELEGRAM_CHAT_ID` are set.
  - Confirm `telegram_bot.allowed_user_ids` contains your user id.
  - Confirm the topic is bound via `/bind`.
- Updates ignored:
  - If `telegram_bot.require_topics` is true, use a topic and not the root chat.
  - Check `telegram.allowlist.denied` events for chat/user ids.
- Turns failing:
  - Check `telegram.turn.failed` and `app_server.*` logs.
  - Verify the Codex app-server is installed and in `telegram_bot.app_server_command` or `CAR_TELEGRAM_APP_SERVER_COMMAND`.
- App-server disconnect loops:
  - Look for repeated `app_server.disconnected` or `telegram.app_server.start_failed` events.
  - Confirm the `codex app-server` binary is healthy/compatible with this autorunner build.
- Approvals not appearing:
  - Ensure `/approvals safe` is set on the topic.
- Formatting not applied:
  - Ensure `telegram_bot.parse_mode` is set to `HTML` (or your preferred mode) and restart the bot.

## Stop

- Stop the process with Ctrl-C. The bot closes the Telegram client and app-server.
