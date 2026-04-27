# Environment variables and defaults

This document is the source of truth for CAR's supported user-authored
environment variables.

## Precedence

1. Built-in defaults
2. `codex-autorunner.yml`
3. `codex-autorunner.override.yml`
4. `.codex-autorunner/config.yml`
5. Environment variables

Generated `.codex-autorunner/config.yml` stays intentionally sparse. CAR keeps
defaults in code and YAML, then writes only explicit local state and pinned
overrides.

Repo-local env files are loaded, when present, in this order:

- `<repo>/.env`
- `<repo>/.codex-autorunner/.env`

## Policy

User-authored env vars are reserved for secrets and private identifiers.
Ordinary runtime behavior belongs in config.

Use config instead of env vars for these values:

- `app_server.command`
- `telegram_bot.app_server_command`
- `telegram_bot.opencode_command`
- `voice.*`
- `update.skip_checks`
- `app_server.auto_restart`
- `pma.inbox_auto_dismiss_grace_seconds`
- `usage.global_cache_root`
- `state_roots.global`

Deprecated or legacy `.env` knobs for those settings are no longer part of the
supported contract.

## Telegram bot

Names are configurable via `telegram_bot.*_env`.

| Env var | Purpose | Default |
| --- | --- | --- |
| `CAR_TELEGRAM_BOT_TOKEN` | Telegram Bot API token. | Must be set for Telegram integration. |
| `CAR_TELEGRAM_CHAT_ID` | Telegram Chat ID for the target chat. | Must be set when referenced by config. |
| `CAR_TELEGRAM_THREAD_ID` | Telegram thread/topic ID within a forum chat. | `null` |

## Discord bot and notifications

Names are configurable via `discord_bot.*_env` and `notifications.discord.*`.

| Env var | Purpose | Default |
| --- | --- | --- |
| `CAR_DISCORD_BOT_TOKEN` | Discord Bot Token. | Must be set for Discord integration. |
| `CAR_DISCORD_APP_ID` | Discord Application ID. | Must be set for Discord integration. |
| `CAR_DISCORD_WEBHOOK_URL` | Discord webhook URL for notification delivery. | Used by notifications when referenced. |

## Server / Web UI auth

`server.auth_token_env` stores the env var name, not the token value itself.

Example:

```yaml
server:
  auth_token_env: CAR_SERVER_TOKEN
```

Then set `CAR_SERVER_TOKEN` in the process environment or `.env`.

## OpenCode server credentials

| Env var | Purpose | Default |
| --- | --- | --- |
| `OPENCODE_SERVER_USERNAME` | Username for OpenCode server HTTP Basic auth. | `"opencode"` when password is set but username is not. |
| `OPENCODE_SERVER_PASSWORD` | Password for OpenCode server HTTP Basic auth. | `null` |

## API keys

| Env var | Purpose | Default |
| --- | --- | --- |
| `OPENAI_API_KEY` | OpenAI API key used by remote Whisper transcription and Docker passthrough. | Referenced by `voice.providers.openai_whisper.api_key_env` by default. |

`OPENAI_API_KEY` can still auto-enable `voice.provider: openai_whisper` when the
provider is configured but `voice.enabled` is not explicitly turned on.

## Runtime-managed env vars

These may exist at runtime, but they are not part of the normal `.env.example`
surface for user-authored configuration:

- `CAR_HUB_ROOT` from CLI `--hub`
- `CAR_DEV_INCLUDE_ROOT_REPO` for repo-development flows
- `ZEROCLAW_WORKSPACE` and `ZEROCLAW_CONFIG_DIR` for ZeroClaw launches
- `VISUAL` and `EDITOR` for `car edit`
- `CODEX_BIN` and `OPENCODE_BIN` for protocol/schema tooling

## Workspace PATH bootstrap

For app-server-backed runtimes and Discord `!<shell command>` passthrough, CAR
prepends workspace-local paths to `PATH`:

- `<workspace>/.codex-autorunner/bin`
- `<workspace>` when `<workspace>/car` exists

CAR also augments `PATH` with common non-interactive install locations such as
`~/.local/bin`.
