# Telegram Bot Runbook

## Purpose

Operate and troubleshoot the Telegram polling bot that routes Telegram traffic
through CAR's orchestration-managed chat runtime.

## Prerequisites

- Set env vars in the bot environment:
  - `CAR_TELEGRAM_BOT_TOKEN`
  - `CAR_TELEGRAM_CHAT_ID`
  - `OPENAI_API_KEY` (or the Codex-required key)
  - `CAR_TELEGRAM_APP_SERVER_COMMAND` (optional full command, e.g. `/opt/homebrew/bin/codex app-server`)
- If the app-server command is a script (ex: Node-based `codex`), prefer an absolute path so the bot can prepend its directory to `PATH` under launchd.
- Configure `telegram_bot` in `codex-autorunner.yml` or `.codex-autorunner/config.yml`.
- Ensure `telegram_bot.allowed_user_ids` includes your Telegram user id.
- `telegram_bot.shell.enabled` is off by default; set it to `true` to enable `!<cmd>` support.
- Enabling shell allows remote command execution gated only by the Telegram allowlist.
- In group chats where the bot is an admin, consider `telegram_bot.trigger_mode: mentions` to avoid reacting to every message.
- For shared supergroups, prefer `collaboration_policy.telegram.destinations` so each topic is explicitly `active`, `command_only`, or `silent`.
- Example:
  ```yaml
  telegram_bot:
    shell:
      enabled: true
  ```

## Start

- `car telegram start --path <hub_root>`
- On startup, the bot logs `telegram.bot.started` with config details.

## Verify

- In the target topic, send `/status` and confirm the selected resource/workspace
  root and active durable thread.
- Confirm `/status` also reports the expected collaboration mode and plain-text trigger for the current root chat or topic.
- Send `/help` to confirm command handling.
- Send `/ids` and confirm the chat/user/thread ids plus the generated collaboration snippet match the intended topic.
- Send a normal message and verify a single agent response.
- Confirm the turn is attached to the same durable thread on repeated messages
  unless `/new`, `/resume`, reset, or archive actions intentionally replace it.
- Send an image with an optional caption and confirm a response (image is stored under the bound workspace).
- Send a voice note and confirm it transcribes (requires Whisper/voice config).

## Common Commands

- `/bind <repo_id|path>`: bind the topic to a repo workspace root; CAR then
  keeps routing messages to that topic's durable thread under the bound
  resource.
- `/new`: start a new durable thread for the bound workspace.
- `/resume`: list recent threads and resume one.
- `/interrupt`: stop the active turn.
- `/approvals yolo|safe`: toggle approval mode.
- `/ids`: show chat/user/thread IDs plus copy-paste collaboration snippets.
- `/status`: show current workspace/runtime state plus the effective collaboration mode for the current destination.
- `/update [all|web|chat|telegram|discord]`: update CAR and restart selected services.
- `!<cmd>`: run a bash command in the bound workspace (controlled by `telegram_bot.shell.enabled`).

## Media Support

- Telegram media handling is controlled by `telegram_bot.media` (enabled by default).
- Images are downloaded to `<workspace>/.codex-autorunner/uploads/telegram-images/` and forwarded to the selected agent as attachment context.
- Voice notes are transcribed via the configured Whisper provider and sent as text inputs.
- Ensure `voice` configuration (and API key env) is set if you want voice note transcription.

## Logs

- Primary log file: `config.log.path` (default `.codex-autorunner/codex-autorunner-hub.log`).
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
  - If only some topics should be active, add explicit `collaboration_policy.telegram.destinations` and silence or deny the root chat.
  - Check `telegram.allowlist.denied` events for chat/user ids.
- Turns failing:
  - Check `telegram.turn.failed` and runtime logs for the selected backend.
  - Verify the configured backend runtime is installed and healthy. For Codex specifically, confirm the app-server is available via `telegram_bot.app_server_command` or `CAR_TELEGRAM_APP_SERVER_COMMAND`. For Hermes, also verify `hermes acp --help` and review `docs/ops/hermes-acp.md`.
  - If routing looks wrong, inspect orchestration bindings first; ordinary turns
    should not depend on Telegram-local thread identity as the authority.
- Turns hanging:
  - Look for `telegram.turn.timeout` in the hub log.
  - Adjust `telegram_bot.app_server.turn_timeout_seconds` if longer turns are expected.
- App-server disconnect loops:
  - Look for repeated `app_server.disconnected` or `telegram.app_server.start_failed` events.
  - Confirm the active backend runtime is healthy and compatible with this autorunner build. For Codex, specifically confirm `codex app-server` is healthy.
- Approvals not appearing:
  - Ensure `/approvals safe` is set on the topic.
- Formatting not applied:
  - Ensure `telegram_bot.parse_mode` is set to `HTML` (or your preferred mode) and restart the bot.

## Stop

- Stop the process with Ctrl-C. The bot closes the Telegram client and app-server.
