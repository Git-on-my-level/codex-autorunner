# Environment variables and defaults

This document centralizes runtime environment variables and default behaviors.

## Precedence

1. Built-in defaults
2. `codex-autorunner.yml`
3. `codex-autorunner.override.yml`
4. `.codex-autorunner/config.yml` (generated)
5. Environment variables

Repo-local env files are loaded (when present) in this order:

- `<repo>/.env`
- `<repo>/.codex-autorunner/.env`

## Behavior overrides

| Env var | Purpose | Default / config key |
| --- | --- | --- |
| `CODEX_AUTORUNNER_SKIP_UPDATE_CHECKS=1` | Skip `./scripts/check.sh` during system updates. | Overrides `update.skip_checks` (default `false`). |
| `CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS` | Disables app-server auto-restart (test-only escape hatch). | Overrides `app_server.auto_restart` (default `true`). |
| `CAR_GLOBAL_STATE_ROOT` | Override the global CAR state root for shared caches/locks. | Overrides `state_roots.global` (default `~/.codex-autorunner`). |
| `CODEX_HOME` | Default base directory for Codex global cache if not set in config. | Used when `usage.global_cache_root` is `null` (default `~/.codex`). |

## Editor selection

Editor precedence for `car edit`:

1. `VISUAL`
2. `EDITOR`
3. `ui.editor` (config; default `vi`)

If none are set, `vi` is used.

## Notifications

Defaults and envs are driven by config keys in `notifications.*`:

- `notifications.timeout_seconds` (default `5.0`)
- `notifications.discord.webhook_url_env` (default `CAR_DISCORD_WEBHOOK_URL`)
- `notifications.telegram.bot_token_env` (default `CAR_TELEGRAM_BOT_TOKEN`)
- `notifications.telegram.chat_id_env` (default `CAR_TELEGRAM_CHAT_ID`)
- `notifications.telegram.thread_id_env` (default `CAR_TELEGRAM_THREAD_ID`)

Set the referenced env vars to deliver notifications.

## Telegram bot

Env overrides that take precedence over config:

- `CAR_OPENCODE_COMMAND` overrides `telegram_bot.opencode_command`
- `CAR_TELEGRAM_APP_SERVER_COMMAND` overrides `telegram_bot.app_server_command`
  - The env var name can be changed via `telegram_bot.app_server_command_env`.

Telegram auth envs (names configurable via `telegram_bot.*_env`):

- `CAR_TELEGRAM_BOT_TOKEN`
- `CAR_TELEGRAM_CHAT_ID`
- `CAR_TELEGRAM_THREAD_ID` (optional, for topic/thread routing)

## Voice input

Env vars override `voice.*` config values:

- `CODEX_AUTORUNNER_VOICE_ENABLED`
- `CODEX_AUTORUNNER_VOICE_PROVIDER`
- `CODEX_AUTORUNNER_VOICE_LATENCY`
- `CODEX_AUTORUNNER_VOICE_CHUNK_MS`
- `CODEX_AUTORUNNER_VOICE_SAMPLE_RATE`
- `CODEX_AUTORUNNER_VOICE_WARN_REMOTE`
- `CODEX_AUTORUNNER_VOICE_MAX_MS`
- `CODEX_AUTORUNNER_VOICE_SILENCE_MS`
- `CODEX_AUTORUNNER_VOICE_MIN_HOLD_MS`

Provider credentials:

- `OPENAI_API_KEY` (default for `voice.providers.openai_whisper.api_key_env`)

## OpenCode server credentials

- `OPENCODE_SERVER_USERNAME`
- `OPENCODE_SERVER_PASSWORD`

Used when authenticating to an OpenCode server endpoint.

## Server auth token

The auth token env var name is defined by config:

- `server.auth_token_env` (default `""`)

Example: set `server.auth_token_env: CAR_AUTH_TOKEN`, then export `CAR_AUTH_TOKEN`.
