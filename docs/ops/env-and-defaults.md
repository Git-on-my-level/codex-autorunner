# Environment variables and defaults

This document is the single source of truth for environment variables consumed by Codex Autorunner (CAR). The companion `.env.example` at the repo root lists the same names for quick reference.

User-authored env vars are primarily for secrets and private identifiers. Ordinary runtime behavior belongs in YAML config (`codex-autorunner.yml`, overrides, `.codex-autorunner/config.yml`) unless no config path exists.

## Precedence

1. Built-in defaults
2. `codex-autorunner.yml`
3. `codex-autorunner.override.yml`
4. `.codex-autorunner/config.yml`
5. Environment variables

Generated `.codex-autorunner/config.yml` stays intentionally sparse. CAR keeps defaults in code and YAML, then writes only explicit local state and pinned overrides.

Repo-local env files are loaded, when present, in this order:

- `<repo>/.env`
- `<repo>/.codex-autorunner/.env`

## Policy

User-authored env vars are reserved for secrets and private identifiers. Ordinary runtime behavior belongs in config.

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

Deprecated or legacy `.env` knobs for those settings are no longer part of the supported contract.

## Editor selection

Editor precedence for `car edit`:

1. `VISUAL`
2. `EDITOR`
3. `ui.editor` (config; default `vi`)

If none are set, `vi` is used.

## Core / Hub

| Env var | Purpose | Default / config key |
| --- | --- | --- |
| `CAR_HUB_ROOT` | Explicit path to the hub root directory. Set by CLI `--hub` flag. | `null` |
| `CAR_GLOBAL_STATE_ROOT` | Overrides `state_roots.global`: global CAR state for caches, locks, and durable CAR metadata (not the Codex CLI data directory). | Default `~/.codex-autorunner` when unset. |
| `CODEX_HOME` | Codex CLI data directory (`auth.json`, session logs, and the default base for usage-derived caches when `usage.global_cache_root` is unset). **Not interchangeable** with `CAR_GLOBAL_STATE_ROOT`. | Default `~/.codex` when unset. |

When `usage.global_cache_root` is omitted from config, CAR defaults it to match the Codex CLI data directory: `CODEX_HOME` if set, otherwise `~/.codex` (same resolution as session logs). CAR global state (`CAR_GLOBAL_STATE_ROOT` / `state_roots.global`) remains separate and is not used for this default.

## Agent binaries

| Env var | Purpose | Default / config key |
| --- | --- | --- |
| `CAR_CODEX_APP_SERVER_COMMAND` | Override the command used to launch Codex app-server subprocesses for Codex-backed runtimes such as ticket-flow. Checked before `CAR_APP_SERVER_COMMAND` and config. | Falls back to `("codex", "app-server")`. |
| `CAR_APP_SERVER_COMMAND` | Legacy generic override for Codex app-server subprocesses. Checked after `CAR_CODEX_APP_SERVER_COMMAND` and before config. | Falls back to `("codex", "app-server")`. |
| `CAR_TELEGRAM_APP_SERVER_COMMAND` | Legacy per-Telegram override for the Telegram app-server command. Ignored by non-Telegram runtimes, including ticket-flow. | Configurable via `telegram_bot.app_server_command_env`. |
| `CAR_OPENCODE_COMMAND` | Override the OpenCode binary command path for the Telegram integration. | Overrides `telegram_bot.opencode_command`. |
| `CODEX_BIN` | Explicit path to the Codex CLI binary (protocol schema generation). | Falls back to PATH resolution of `codex`. |
| `OPENCODE_BIN` | Explicit path to the OpenCode binary (protocol schema generation). | Falls back to PATH resolution of `opencode`. |

## Telegram bot

Env overrides that take precedence over config:

- `CAR_OPENCODE_COMMAND` overrides `telegram_bot.opencode_command`
- `CAR_CODEX_APP_SERVER_COMMAND` overrides `app_server.command` for Codex-backed runtimes (checked first)
- `CAR_APP_SERVER_COMMAND` is still honored as a generic fallback when `CAR_CODEX_APP_SERVER_COMMAND` is unset
- `CAR_TELEGRAM_APP_SERVER_COMMAND` is only honored by Telegram-specific config/runtime paths
  - The preferred Telegram env var name is configurable via `telegram_bot.app_server_command_env` (default `CAR_APP_SERVER_COMMAND`).

Telegram auth envs (names configurable via `telegram_bot.*_env`):

| Env var | Purpose | Default |
| --- | --- | --- |
| `CAR_TELEGRAM_BOT_TOKEN` | Telegram Bot API token. Required for Telegram integration. | Must be set. |
| `CAR_TELEGRAM_CHAT_ID` | Telegram Chat ID for the target chat. | Must be set. |
| `CAR_TELEGRAM_THREAD_ID` | Telegram thread/topic ID within a forum chat. Optional, for topic routing. | `null` |

## Discord bot

Discord auth envs (names configurable via `discord_bot.*_env`):

| Env var | Purpose | Default |
| --- | --- | --- |
| `CAR_DISCORD_BOT_TOKEN` | Discord Bot Token. Required for Discord integration. | Must be set. |
| `CAR_DISCORD_APP_ID` | Discord Application ID. Required for Discord integration. | Must be set. |
| `CAR_DISCORD_WEBHOOK_URL` | Discord webhook URL for notification delivery. | Must be set for Discord notifications. |

Dispatch and timeout defaults:

- `discord_bot.dispatch.handler_timeout_seconds` (default `null`, disabled)
- `discord_bot.dispatch.handler_stalled_warning_seconds` (default `60`)

## Notifications

Defaults and envs are driven by config keys in `notifications.*`:

- `notifications.timeout_seconds` (default `5.0`)
- `notifications.discord.webhook_url_env` (default `CAR_DISCORD_WEBHOOK_URL`)
- `notifications.telegram.bot_token_env` (default `CAR_TELEGRAM_BOT_TOKEN`)
- `notifications.telegram.chat_id_env` (default `CAR_TELEGRAM_CHAT_ID`)
- `notifications.telegram.thread_id_env` (default `CAR_TELEGRAM_THREAD_ID`)

Set the referenced env vars to deliver notifications.

## Server / Web UI

| Env var | Purpose | Default / config key |
| --- | --- | --- |
| `server.auth_token_env` | Config key: name of the env var holding the bearer token for API/websocket auth. Default `""` (disabled). | Set in config, then export the named env var. Example: `server.auth_token_env: CAR_SERVER_TOKEN` then export `CAR_SERVER_TOKEN`. |

## OpenCode server credentials

| Env var | Purpose | Default |
| --- | --- | --- |
| `OPENCODE_SERVER_USERNAME` | Username for OpenCode server HTTP Basic auth. | `"opencode"` (when password is set but username is not). |
| `OPENCODE_SERVER_PASSWORD` | Password for OpenCode server HTTP Basic auth. | `null` |

## API keys

| Env var | Purpose | Default |
| --- | --- | --- |
| `OPENAI_API_KEY` | OpenAI API key. Used by voice (Whisper provider) and passed through to Docker containers. Auto-enables voice when present. | Configurable via `voice.providers.openai_whisper.api_key_env`. |

## Voice input

Env vars override `voice.*` config values:

| Env var | Purpose | Default |
| --- | --- | --- |
| `CODEX_AUTORUNNER_VOICE_ENABLED` | Explicitly enable/disable voice input. | Auto-enables if API key is present. |
| `CODEX_AUTORUNNER_VOICE_PROVIDER` | Voice provider: `openai_whisper`, `local_whisper`, `mlx_whisper`. | `"local_whisper"` |
| `CODEX_AUTORUNNER_VOICE_LATENCY` | Voice latency mode (`"balanced"`, `"low"`). | `"balanced"` |
| `CODEX_AUTORUNNER_VOICE_CHUNK_MS` | Recording chunk duration in milliseconds. | `600` |
| `CODEX_AUTORUNNER_VOICE_SAMPLE_RATE` | Audio sample rate in Hz. | `16000` |
| `CODEX_AUTORUNNER_VOICE_WARN_REMOTE` | Warn before using a remote voice API. | Auto-determined. |
| `CODEX_AUTORUNNER_VOICE_MAX_MS` | Push-to-talk max recording duration (ms). | `15000` |
| `CODEX_AUTORUNNER_VOICE_SILENCE_MS` | Silence auto-stop duration (ms). | `1200` |
| `CODEX_AUTORUNNER_VOICE_MIN_HOLD_MS` | Push-to-talk minimum hold duration (ms). | `150` |

Local provider:

- `voice.provider: local_whisper` uses on-device transcription via `faster-whisper`.
- Install local deps with `pip install "codex-autorunner[voice-local]"`.
- `voice.provider: mlx_whisper` uses on-device transcription via `mlx-whisper` (Apple Silicon).
- Install MLX deps with `pip install "codex-autorunner[voice-mlx]"`.
- Local voice providers also require `ffmpeg` available on PATH.

## App server

| Env var | Purpose | Default / config key |
| --- | --- | --- |
| `CODEX_DISABLE_APP_SERVER_AUTORESTART_FOR_TESTS` | Disable app-server auto-restart (test-only escape hatch). | Overrides `app_server.auto_restart` (default `true`). |

## Orchestration / PMA tuning

| Env var | Purpose | Default |
| --- | --- | --- |
| `CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS` | SQLite busy timeout (milliseconds) for the orchestration database. | `30000` |
| `CAR_PMA_INBOX_AUTO_DISMISS_GRACE_SECONDS` | Grace period (seconds) before auto-dismissing PMA inbox items. | `3600` |
| `CAR_LEGACY_MIRROR_ENABLED` | Enable legacy one-way sync mirror from orchestration SQLite to old `threads.sqlite3` schema. Set to `"true"` to enable. | `""` (disabled). |

## ZeroClaw agent runtime

| Env var | Purpose | Default |
| --- | --- | --- |
| `ZEROCLAW_WORKSPACE` | Workspace root for the ZeroClaw runtime. Set at launch to the managed workspace path. | Set by managed runtime launcher. |
| `ZEROCLAW_CONFIG_DIR` | Configuration directory for the ZeroClaw runtime. | `$HOME/.zeroclaw` |

## Test / CI tuning (`scripts/check.sh`)

| Env var | Purpose | Default |
| --- | --- | --- |
| `CODEX_FAST_TEST_ENFORCE_BUDGET` | Make fast-test budget violations fatal instead of report-only. | `0` |
| `CODEX_FAST_TEST_MAX_DURATION_SECONDS` | Maximum allowed duration (seconds) for individual fast tests. | `0.2` |
| `CODEX_FAST_TEST_REPORT_LIMIT` | Maximum number of slow-test violations to report. | `20` |
| `CODEX_FAST_TEST_VERIFY_NODEIDS` | Verify collected test node IDs match the JUnit report. | `0` |
| `CODEX_LOCAL_CHECK_INCLUDE_DEADCODE` | Enable dead-code checks during local development (normally only in CI). | `0` |

## Workspace PATH bootstrap

For app-server-backed agent runtimes (web terminal/PMA and Telegram) and Discord `!<shell command>` passthrough, CAR prepends workspace-local paths to `PATH`:

- `<workspace>/.codex-autorunner/bin`
- `<workspace>` (only when `<workspace>/car` exists)

This makes `car` resolve to the workspace shim/runtime for that workspace without requiring a global shell install.

CAR also augments PATH with common non-interactive install locations (for example, `~/.local/bin`) so pipx-installed `car` remains discoverable in daemon contexts.

## Runtime-managed env vars

These may exist at runtime but are often set by tooling rather than hand-edited `.env` files:

- `CAR_HUB_ROOT` from CLI `--hub`
- `CAR_DEV_INCLUDE_ROOT_REPO` for repo-development flows
- `ZEROCLAW_WORKSPACE` and `ZEROCLAW_CONFIG_DIR` for ZeroClaw launches
- `VISUAL` and `EDITOR` for `car edit`
- `CODEX_BIN` and `OPENCODE_BIN` for protocol/schema tooling
