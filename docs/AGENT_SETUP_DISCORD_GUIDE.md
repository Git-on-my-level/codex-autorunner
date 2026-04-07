# CAR Discord Setup Add-On (for Agents)

Use this guide after `docs/AGENT_SETUP_GUIDE.md` when a user wants the interactive Discord bot surface.

---

## Interaction Runtime Architecture

Discord interactions follow a two-phase lifecycle: **ingress** then **background execution**.

### Ingress (fast, on the gateway hot path)

`InteractionIngress` is the single entry point for all interaction types (slash
commands, component interactions, modal submits, autocomplete).  Ingress owns:

- interaction parsing and normalization
- collaboration/authz checks
- ack policy lookup and initial response or defer emission
- timing telemetry (`interaction_created_at -> ingress_started -> authz -> ack -> ingress_finished`)

Ingress **must not** run command business logic.  After ingress acknowledges the
interaction (either by deferring or responding immediately), control returns to
the gateway worker immediately.

### Background command runner

`CommandRunner` dispatches ingressed interactions off the gateway hot path.
Each interaction runs as an independent `asyncio.Task` with:

- guaranteed background execution (gateway never blocks on a handler)
- arrival-order preservation for events that go through the FIFO queue
- per-command handler timeout (default 120 s, configurable via
  `discord_bot.dispatch.handler_timeout_seconds`)
- stall warning at 60 s (configurable via
  `discord_bot.dispatch.handler_stalled_warning_seconds`)
- grace-period shutdown that drains in-flight work

Handler timeout, stalled warnings, and lifecycle telemetry are logged under
event keys `discord.runner.*` and `discord.ingress.*`.

### Telemetry event keys

High-signal telemetry for the interaction lifecycle:

| Event key | Phase |
|---|---|
| `discord.ingress.completed` | Ingress finished (includes `ack_delta_ms`, `gateway_to_ingress_ms`) |
| `discord.runner.execute.start` | Handler started (includes `queue_wait_ms`, `ingress_elapsed_ms`) |
| `discord.runner.stalled` | Handler running past stall warning threshold |
| `discord.runner.timeout` | Handler cancelled after timeout |
| `discord.runner.execute.done` | Handler completed (includes `total_lifecycle_ms`, `gateway_to_completion_ms`) |

---

## Instructions for the Agent

You are helping a user enable Discord for CAR. Keep this as an optional add-on path.

### Step 1: Confirm Discord Is the Right Add-On

Ask the user if they want:

1. **Interactive Discord bot** (this guide) - slash-command control from Discord channels.
2. **Notifications only** - one-way alerts via webhooks.

Use this guide only for the interactive Discord bot flow.

### Step 2: Check Discord Prerequisites

Verify the user has:

1. Completed base setup from `docs/AGENT_SETUP_GUIDE.md`.
2. A Discord server where they can create/manage bots.
3. Optional dependencies installed:

```bash
pip install "codex-autorunner[discord]"
# local dev checkout
pip install -e ".[discord]"
```

If they installed CAR with `pipx`, reinstall with extras:

```bash
pipx uninstall codex-autorunner
pipx install "codex-autorunner[discord]"
```

If the user wants Hermes as the backend agent for Discord, they should also
complete `docs/ops/hermes-acp.md` first and ensure `hermes` is available on
`PATH`. Discord then discovers Hermes through the normal agent registry and
keeps unsupported actions capability-gated.

### Step 3: Create Discord App and Bot Credentials

In Discord Developer Portal:

1. Create an application.
2. Add a bot user.
3. Copy:
   - Bot Token
   - Application ID
4. Invite the bot to the server with OAuth scopes:
   - `bot`
   - `applications.commands`
5. In `OAuth2 -> URL Generator`, set Bot Permissions (permissions integer):
   - `2322563695115328`
   - Re-invite the bot after changing permissions/scope settings.

Set env vars:

```bash
export CAR_DISCORD_BOT_TOKEN="<bot-token>"
export CAR_DISCORD_APP_ID="<application-id>"
```

### Step 4: Decide Command Registration Scope

Set command registration strategy:

1. **Development:** `guild` scope (fast propagation, recommended while iterating).
2. **Production:** `global` scope (can take longer for command changes to appear).

Also decide the operating mode for the Discord surface itself:

1. **Personal setup** - one operator in a dedicated channel or thread.
2. **Collaborative setup** - a shared guild where some channels should be active,
   some command-only, and some silent.

For the personal path, legacy `discord_bot` allowlists plus `/car bind` or
`/pma on` remain valid. Use explicit `collaboration_policy.discord` only when
you need intentional shared-channel behavior.

### Step 5: Add Minimal Discord Config

In `codex-autorunner.yml` (or repo/hub override), add:

```yaml
discord_bot:
  enabled: true
  bot_token_env: CAR_DISCORD_BOT_TOKEN
  app_id_env: CAR_DISCORD_APP_ID
  allowed_guild_ids:
    - "123456789012345678"
  allowed_channel_ids: []
  allowed_user_ids: []
  command_registration:
    enabled: true
    scope: guild
    guild_ids:
      - "123456789012345678"
  shell:
    enabled: true
    timeout_ms: 120000
    max_output_chars: 3800
  dispatch:
    handler_timeout_seconds: 120
    handler_stalled_warning_seconds: 60
  media:
    enabled: true
    voice: true
    max_voice_bytes: 10000000
```

When `discord_bot.media.voice: true`, inbound Discord audio attachments are transcribed through the configured `voice.provider` and injected into attachment context.

### Voice Provider Setup (OpenAI vs Local Whisper / MLX Whisper)

You can run voice transcription with either OpenAI Whisper (API) or local Whisper (on-device).

OpenAI Whisper (API):

1. Set an API key env var (default key name):
   - `OPENAI_API_KEY=...`
2. Set provider:
   - `voice.provider: openai_whisper`

Local Whisper (on-device, faster-whisper):

1. Install local voice dependencies:
   - `pip install "codex-autorunner[voice-local]"`
   - Ensure `ffmpeg` is installed and on PATH (for macOS: `brew install ffmpeg`)
   - macOS launchd path (`scripts/install-local-mac-hub.sh` / `scripts/safe-refresh-local-mac-hub.sh`) now installs this automatically when voice provider resolves to `local_whisper` or `mlx_whisper`.
2. Set provider:
   - `voice.provider: local_whisper`
   - or env override: `CODEX_AUTORUNNER_VOICE_PROVIDER=local_whisper`

MLX Whisper (on-device, Apple Silicon):

1. Install MLX voice dependencies:
   - `pip install "codex-autorunner[voice-mlx]"`
   - Ensure `ffmpeg` is installed and on PATH (for macOS: `brew install ffmpeg`)
   - macOS launchd setup auto-selects this for new Apple Silicon installs.
2. Set provider:
   - `voice.provider: mlx_whisper`
   - or env override: `CODEX_AUTORUNNER_VOICE_PROVIDER=mlx_whisper`

Provider selection and precedence:

- Only one provider is active at runtime.
- `CODEX_AUTORUNNER_VOICE_PROVIDER` overrides `voice.provider` in config.
- It is normal to keep multiple provider blocks (for example `openai_whisper`, `local_whisper`, `mlx_whisper`) in config; only the selected provider is used.
- There is no automatic provider fallback. If the selected provider fails, transcription fails for that request.

Example config:

```yaml
voice:
  enabled: true
  provider: local_whisper # or mlx_whisper / openai_whisper
  providers:
    openai_whisper:
      api_key_env: OPENAI_API_KEY
      model: whisper-1
    local_whisper:
      model: small
      device: auto
      compute_type: default
    mlx_whisper:
      model: small
```

Allowlist behavior:
- At least one allowlist must be configured.
- Any non-empty allowlist acts as a required filter.
- Example: if both `allowed_guild_ids` and `allowed_user_ids` are set, both must match.

For a quick personal setup, the legacy allowlists above are enough.
For shared guild collaboration, prefer an explicit `collaboration_policy.discord`
block so plain-text turns only happen where you intend them to:

```yaml
collaboration_policy:
  discord:
    allowed_guild_ids:
      - "123456789012345678"
    allowed_user_ids:
      - "222222222222222222"
    default_mode: command_only
    destinations:
      - guild_id: "123456789012345678"
        channel_id: "333333333333333333"
        mode: active
        plain_text_trigger: mentions
      - guild_id: "123456789012345678"
        channel_id: "444444444444444444"
        mode: silent
```

Recommended starting point for shared servers:
- `default_mode: command_only` keeps slash commands usable in other allowlisted channels without passive replies.
- Use `mode: active` for channels or threads where CAR should answer plain-text messages.
- Use `mode: silent` for human-only channels where CAR should ignore both plain text and commands.
- Run `/car admin ids` inside a channel to get exact IDs plus a copy-paste collaboration snippet.

Migration notes:
- Existing dedicated-channel installs do not need to migrate if the current
  `/car bind` or `/pma on` workflow already matches operator expectations.
- Existing shared-guild installs should migrate to explicit
  `collaboration_policy.discord` destinations instead of assuming every
  allowlisted/bound channel should answer normal messages.
- The safest migration pattern for a shared guild is:
  1. keep current allowlists
  2. set `default_mode: command_only`
  3. mark only intended collaboration channels as `mode: active`
  4. mark human-only channels as `mode: silent`

### Step 6: Register Commands and Verify First Run

Run:

```bash
car doctor
car discord start --path <hub_or_repo_root>
```

`car discord start` now auto-syncs Discord application commands at startup (including `/car` and `/pma`) when `discord_bot.command_registration.enabled: true`.
If command registration config is invalid (for example `scope: guild` without `guild_ids`), startup exits with an actionable error.
Use manual sync only when needed:

```bash
car discord register-commands --path <hub_or_repo_root>
```

For macOS launchd-managed installs (`scripts/install-local-mac-hub.sh` / `scripts/safe-refresh-local-mac-hub.sh`):
- Discord is auto-managed when `discord_bot.enabled: true` and both credential env vars are set.
- Auto-detection uses `discord_bot.bot_token_env` and `discord_bot.app_id_env` (defaults: `CAR_DISCORD_BOT_TOKEN`, `CAR_DISCORD_APP_ID`).
- Optional overrides:
  - `ENABLE_DISCORD_BOT=auto|true|false`
  - `DISCORD_LABEL` / `DISCORD_PLIST_PATH`
  - `HEALTH_CHECK_DISCORD=auto|true|false` (safe refresh)

In an allowed Discord channel:

1. Run `/car status`.
2. Bind the channel to the repo workspace root with
   `/car bind path:<workspace-path>`.
3. Run flow commands:
   - `/flow status [run_id]`
   - `/flow runs [limit]`
   - `/flow issue issue_ref:<issue#|url>`
   - `/flow plan text:<plan>`
   - `/flow resume [run_id]`
   - `/flow stop [run_id]`
   - `/flow recover [run_id]`
   - `/flow archive [run_id]`
   - `/flow reply text:<message> [run_id]`
4. In PMA mode (or unbound channel), `/flow status` and `/flow runs` show a hub-wide flow overview.

Note: `/car` and `/pma` responses are ephemeral (visible only to the invoking user).

If the bot replies with an authorization error, check allowlists first.

### Step 7: Discord Permission Checklist

Required or likely-needed permissions:

1. Bot present in target server.
2. `applications.commands` scope granted (for slash commands).
3. Bot can view and reply in target channels:
   - `View Channels`
   - `Send Messages`
   - `Read Message History`
   - `Send Messages in Threads` (if using threads)
4. Operator can run slash commands in that channel.

You usually do not need broad admin permissions for baseline CAR Discord usage.

---

## PMA (Proactive Mode Agent) Support

Discord supports PMA mode for routing PMA output to a channel, managing PMA state with slash commands, and running free-text turns from channel messages.

### Enabling PMA Mode

In any allowlisted Discord channel:

1. Run `/pma on` to enable PMA mode for the channel.
2. If the channel was previously bound to a repo workspace and durable thread,
   that binding is saved and restored when PMA is disabled.
3. PMA output from the agent will be delivered to the channel.

### How to Actually Chat With an Agent Today

Telegram-style back-and-forth is now supported in Discord.

For repo/workspace mode:

1. Run `/car bind path:<workspace-path>` to select the repo workspace root CAR
   should use for this channel.
2. Optional: set agent/model with `/car agent ...` and `/car model ...`.
3. Confirm the effective collaboration policy with `/car status` or `/car admin ids`.
4. Send a normal channel message (do not start with `/`) only in a destination whose collaboration mode is `active`.
5. The bot routes the message to this channel's current durable CAR thread under
   that resource and replies in-channel (non-ephemeral).

For PMA mode:

1. Run `/pma on`.
2. Send a normal channel message (do not start with `/`).
3. The bot runs a PMA turn and replies in-channel.
4. Run `/pma off` to return to the previous repo/workspace-thread binding.

Notes:
- Slash command responses remain ephemeral.
- In PMA mode, `/flow status` and `/flow runs` report hub-wide flow status (manifest-driven) without requiring `/pma off`.
- If a ticket flow run is paused in repo mode, the next free-text message is treated as the flow reply and resumes that run.
- If PMA itself answers a paused ticket-flow dispatch, CAR mirrors that PMA reply back into the repo's bound Discord/Telegram chat so the user can see that no manual reply is needed.
- If PMA must escalate instead, CAR sends that escalation to a single PMA-bound chat for the repo. The selection rule is: freshest matching PMA Discord binding first, Telegram only as fallback.
- PMA should escalate when ticket flow cannot finish with a clean post-ticket commit, including cases where the ticket is marked done but unrelated dirty files remain and ownership is ambiguous.
- `/car ...` and `/pma ...` slash commands are normalized through CAR's shared command-ingress parser before dispatch.
- Plain-text turns now require both collaboration-policy approval and an active execution target (`/car bind ...` or `/pma on`).
- `plain_text_trigger: mentions` uses Discord bot mentions such as `<@bot-id>` and is the recommended trigger for shared channels.
- Unbound but allowlisted channels stay quiet for ordinary conversation instead of replying with repeated "not bound" notices.
- `!<cmd>` runs a local non-interactive shell command in the bound workspace when `discord_bot.shell.enabled` is `true`.

### PMA Commands

| Command | Description |
|---------|-------------|
| `/pma on` | Enable PMA mode for this channel |
| `/pma off` | Disable PMA mode and restore previous binding |
| `/pma status` | Show current PMA mode status |

### PMA Prerequisites

1. PMA must be enabled in hub config (`pma.enabled: true`).
2. The user must be authorized via allowlists.

### Disabling PMA in Discord

If PMA is disabled globally in hub config, `/pma` commands will return an actionable error message indicating how to enable it.

---

## Troubleshooting

### Slash commands do not appear

1. Restart the Discord bot so startup auto-sync runs.
2. Re-run `car discord register-commands --path <hub_or_repo_root>` if you need to force an immediate sync.
3. For development, prefer `guild` scope with explicit `guild_ids`.
4. Verify `CAR_DISCORD_BOT_TOKEN` and `CAR_DISCORD_APP_ID` are set in the process environment.
5. If using launchd on macOS, confirm the Discord agent is loaded:
   - `launchctl print "gui/$(id -u)/com.codex.autorunner.discord"`

### "Not authorized" responses

1. Check allowlists in `discord_bot`:
   - `allowed_guild_ids`
   - `allowed_channel_ids`
   - `allowed_user_ids`
2. Remember all non-empty allowlists must match the interaction.
3. If you use `collaboration_policy.discord`, check `/car status` or `/car admin ids` in the channel to see the effective mode and trigger behavior.

### Bot running but no useful flow output

1. Ensure the channel is bound:
   - `/car bind path:<workspace-path>` to restore/select the repo workspace root
2. Confirm workspace path exists on the CAR host.
3. Run `car doctor` and check Discord check results for missing deps/env/state file issues.

### Migrating an existing Discord setup to collaboration mode

Use this when a legacy Discord install worked for one operator but now needs to
support a shared guild safely:

1. Keep the existing `discord_bot.allowed_*` filters; they remain the admission gate.
2. Run `/car admin ids` in each channel or thread you care about and collect the exact IDs.
3. Add `collaboration_policy.discord` with:
   - `default_mode: command_only`
   - explicit `mode: active` destinations for collaboration channels
   - explicit `mode: silent` destinations for human-only channels
4. Run `/car status` in those channels to verify the effective mode and
   plain-text trigger.
5. Re-run `car doctor` and check the compiled collaboration summary and any
   `default_mode=active` warning.

### Slash commands work, but normal messages get no response

This usually means one of three things:
- the bot user cannot actually access guild/channel message events
- the channel is not bound and PMA is not enabled
- collaboration policy is set to `command_only`, `silent`, or `mentions` and the current message does not satisfy that policy

1. Re-invite the bot with both scopes:
   - `bot`
   - `applications.commands`
2. Use permissions integer:
   - `2322563695115328`
3. Confirm channel overrides do not deny:
   - `View Channels`
   - `Send Messages`
   - `Read Message History`
4. Restart Discord bot process after re-invite/permission changes.
5. Re-test with:
   - `/car status` (slash path)
   - `/car admin ids` (effective IDs + suggested collaboration snippet)
   - plain text message (non-slash turn path)

High-signal diagnostics for this failure mode:
- REST responses like `Missing Access (50001)` for channel lookups.
- REST responses like `Unknown Guild (10004)` for expected guild IDs.

---

## Logs and Events to Check First

Default hub log path:
- `.codex-autorunner/codex-autorunner-hub.log`

High-signal Discord events/logs:
- `discord.bot.starting`
- `discord.commands.sync.overwrite`
- `discord.ingress.completed` (ingress latency and ack timing per interaction)
- `discord.runner.stalled` (handler past stall warning threshold)
- `discord.runner.timeout` (handler cancelled after timeout)
- `discord.runner.execute.done` (full lifecycle timing)
- `discord.pause_watch.notified`
- `discord.pause_watch.scan_failed`
- `discord.outbox.send_failed`
- `Discord gateway error; reconnecting`
- `Discord API request failed ... Missing Access`
- `Discord API request failed ... Unknown Guild`

Quick grep:

```bash
rg -n "discord\\.(bot\\.starting|commands\\.sync\\.overwrite|ingress\\.(completed|ack)|runner\\.(stalled|timeout|execute)|pause_watch\\.(notified|scan_failed)|outbox\\.send_failed)|Discord gateway error" .codex-autorunner/codex-autorunner-hub.log
```

Trace a specific failed Discord turn by conversation ID:

```bash
car discord trace "Turn failed: ... (conversation discord:<channel_id>:<guild_id|->)" --path <repo_or_hub_root>
car discord trace --conversation=discord:<channel_id>:<guild_id|-> --path <repo_or_hub_root>
car discord trace --conversation=<channel_id>:<guild_id|-> --path <repo_or_hub_root> --json
```

`car discord trace` scans log files under `.codex-autorunner/`, filters by the
conversation id, and highlights likely error lines with nearby context.

---

## Related Docs

- `src/codex_autorunner/surfaces/discord/README.md`
- `docs/ops/env-and-defaults.md`
