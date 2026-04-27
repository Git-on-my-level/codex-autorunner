# Discord Surface

Discord bot surface and adapters.

The Discord interaction runtime (ingress, command runner, interaction dispatch) lives in `integrations/discord/`. This surface package re-exports the Discord-facing surface for layering purposes.

## Interaction Lifecycle

All Discord interactions follow a two-phase lifecycle:

1. **Ingress** (`InteractionIngress`): normalizes the interaction, checks
   authorization via collaboration policy, and emits the initial response or
   defer.  Ingress runs on the gateway hot path and **never** executes business
   logic.  Timing is recorded from `interaction_created_at` through ack.

2. **Background execution** (`CommandRunner`): after ingress acknowledges the
   interaction, the runner dispatches it off the gateway hot path with optional
   per-command timeout enforcement (disabled by default), stall warnings (default 60 s), and full
   lifecycle telemetry.

Timeout and stall thresholds are configurable under `discord_bot.dispatch`:

```yaml
discord_bot:
  dispatch:
    handler_timeout_seconds: null
    handler_stalled_warning_seconds: 60
```

## Setup

1. Create a Discord application and bot in the Discord Developer Portal.
2. Copy the bot **Token** and **Application ID**.
3. Invite the bot to your server with OAuth2 scopes:
   - `bot`
   - `applications.commands`
   - Recommended permissions integer: `2322563695115328`
4. Configure `discord_bot.enabled: true` and allowlists in `codex-autorunner.yml`.
   - For a personal dedicated channel, legacy allowlists are enough.
   - For shared-guild collaboration, prefer `collaboration_policy.discord` with
     `default_mode: command_only` and explicit `destinations`.
5. Configure command registration:
   - development: `command_registration.scope: guild` with at least one `guild_id`
   - production: `command_registration.scope: global`
6. Start the bot:
   - `car discord start`
   - Startup auto-syncs application commands when `command_registration.enabled: true`.
   - Invalid registration config (for example `scope: guild` with empty `guild_ids`) fails startup fast.
7. Optional manual sync:
   - `car discord register-commands`

Recommended during development: use guild-scoped command registration so command updates propagate quickly.

## Flow Commands

Use `/flow ...` for ticket flow actions:

- `/flow status [run_id]`
- `/flow runs [limit]`
- `/flow issue issue_ref:<issue#|url>`
- `/flow plan text:<plan>`
- `/flow start [force_new]`
- `/flow restart [run_id]`
- `/flow resume [run_id]`
- `/flow stop [run_id]`
- `/flow recover [run_id]`
- `/flow archive [run_id]`
- `/flow reply text:<message> [run_id]`

In PMA mode (or when unbound), `/flow status` and `/flow runs` default to a hub-wide overview.

## Common Failure Mode: Slash Works, Messages Do Not

If `/car ...` works but normal channel messages do not get replies, check both
Discord permissions and CAR collaboration state:

1. Re-invite bot with scopes `bot` + `applications.commands`.
2. Use permissions integer `2322563695115328`.
3. Ensure channel permissions allow `View Channels`, `Send Messages`, and `Read Message History`.
4. Run `/car status` to confirm the channel is bound or PMA-enabled.
5. Run `/car admin ids` to inspect the effective collaboration mode and generate a
   copy-paste `collaboration_policy.discord` snippet.
6. Restart the Discord bot process and retest.

Unbound but allowlisted channels now stay quiet for ordinary conversation.
This is intentional: plain-text turns only start when the channel is both
collaboration-allowed and ready to execute through a bound workspace or PMA.

## Migration guidance

- Existing dedicated-channel installs can keep the legacy `discord_bot`
  allowlists and binding flow unchanged.
- Shared guilds should migrate to `collaboration_policy.discord` when operators
  want explicit `active`, `command_only`, and `silent` channels.
- The recommended migration pattern is `default_mode: command_only` plus explicit
  destinations captured from `/car admin ids`.

## Integration Harness

For cross-surface PMA regressions, use
`tests/chat_surface_integration/README.md`. It drives Discord message ingress
through `DiscordBotService` with a subprocess-backed Hermes fixture so surface
progress and final delivery can be asserted together.

## Example config

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

collaboration_policy:
  discord:
    allowed_guild_ids:
      - "123456789012345678"
    default_mode: command_only
    destinations:
      - guild_id: "123456789012345678"
        channel_id: "234567890123456789"
        mode: active
        plain_text_trigger: mentions
      - guild_id: "123456789012345678"
        channel_id: "345678901234567890"
        mode: silent
```

## Example environment variables

```bash
export CAR_DISCORD_BOT_TOKEN="<discord-bot-token>"
export CAR_DISCORD_APP_ID="<discord-application-id>"
```
