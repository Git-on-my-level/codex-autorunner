# CAR Discord Setup Add-On (for Agents)

Use this guide after `docs/AGENT_SETUP_GUIDE.md` when a user wants the interactive Discord bot surface.

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

Set env vars:

```bash
export CAR_DISCORD_BOT_TOKEN="<bot-token>"
export CAR_DISCORD_APP_ID="<application-id>"
```

### Step 4: Decide Command Registration Scope

Set command registration strategy:

1. **Development:** `guild` scope (fast propagation, recommended while iterating).
2. **Production:** `global` scope (can take longer for command changes to appear).

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
```

Allowlist behavior:
- At least one allowlist must be configured.
- Any non-empty allowlist acts as a required filter.
- Example: if both `allowed_guild_ids` and `allowed_user_ids` are set, both must match.

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
2. Bind workspace with `/car bind path:<workspace-path>`.
3. Run `/car flow runs` or `/car flow status`.

Note: `/car` and `/pma` responses are ephemeral (visible only to the invoking user).

If the bot replies with an authorization error, check allowlists first.

### Step 7: Discord Permission Checklist

Required or likely-needed permissions:

1. Bot present in target server.
2. `applications.commands` scope granted (for slash commands).
3. Bot can send messages in the target channel.
4. Operator can run slash commands in that channel.

You usually do not need broad admin permissions for baseline CAR Discord usage.

---

## PMA (Proactive Mode Agent) Support

Discord supports PMA mode for routing PMA output to a channel, managing PMA state with slash commands, and running free-text turns from channel messages.

### Enabling PMA Mode

In any allowlisted Discord channel:

1. Run `/pma on` to enable PMA mode for the channel.
2. If the channel was previously bound to a workspace, that binding is saved and restored when PMA is disabled.
3. PMA output from the agent will be delivered to the channel.

### How to Actually Chat With an Agent Today

Telegram-style back-and-forth is now supported in Discord.

For repo/workspace mode:

1. Run `/car bind path:<workspace-path>`.
2. Optional: set agent/model with `/car agent ...` and `/car model ...`.
3. Send a normal channel message (do not start with `/`).
4. The bot runs a turn and replies in-channel (non-ephemeral).

For PMA mode:

1. Run `/pma on`.
2. Send a normal channel message (do not start with `/`).
3. The bot runs a PMA turn and replies in-channel.
4. Run `/pma off` to return to the previous workspace binding.

Notes:
- Slash command responses remain ephemeral.
- If a ticket flow run is paused in repo mode, the next free-text message is treated as the flow reply and resumes that run.
- `/car ...` and `/pma ...` slash commands are normalized through CAR's shared command-ingress parser before dispatch.
- Direct-chat turns use the shared plain-text turn policy in `always` mode, so non-command messages trigger turns while slash commands stay command-only.

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

### Bot running but no useful flow output

1. Ensure the channel is bound:
   - `/car bind path:<workspace-path>`
2. Confirm workspace path exists on the CAR host.
3. Run `car doctor` and check Discord check results for missing deps/env/state file issues.

---

## Logs and Events to Check First

Default hub log path:
- `.codex-autorunner/codex-autorunner-hub.log`

High-signal Discord events/logs:
- `discord.bot.starting`
- `discord.commands.sync.overwrite`
- `discord.pause_watch.notified`
- `discord.pause_watch.scan_failed`
- `discord.outbox.send_failed`
- `Discord gateway error; reconnecting`

Quick grep:

```bash
rg -n "discord\\.(bot\\.starting|commands\\.sync\\.overwrite|pause_watch\\.(notified|scan_failed)|outbox\\.send_failed)|Discord gateway error" .codex-autorunner/codex-autorunner-hub.log
```

---

## Related Docs

- `src/codex_autorunner/surfaces/discord/README.md`
- `docs/ops/env-and-defaults.md`
