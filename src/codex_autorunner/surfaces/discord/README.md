# Discord Surface

Discord bot surface and adapters.

## Setup

1. Create a Discord application and bot in the Discord Developer Portal.
2. Copy the bot **Token** and **Application ID**.
3. Invite the bot to your server with OAuth2 scopes:
   - `bot`
   - `applications.commands`
   - Recommended permissions integer: `2322563695115328`
4. Configure `discord_bot.enabled: true` and allowlists in `codex-autorunner.yml`.
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

Use `/car flow ...` for ticket flow actions:

- `/car flow status [run_id]`
- `/car flow runs [limit]`
- `/car flow issue issue_ref:<issue#|url>`
- `/car flow plan text:<plan>`
- `/car flow start [force_new]`
- `/car flow restart [run_id]`
- `/car flow resume [run_id]`
- `/car flow stop [run_id]`
- `/car flow recover [run_id]`
- `/car flow archive [run_id]`
- `/car flow reply text:<message> [run_id]`

In PMA mode (or when unbound), `/car flow status` and `/car flow runs` default to a hub-wide overview.

## Common Failure Mode: Slash Works, Messages Do Not

If `/car ...` works but normal channel messages do not get replies, the bot usually lacks effective guild/channel access.

1. Re-invite bot with scopes `bot` + `applications.commands`.
2. Use permissions integer `2322563695115328`.
3. Ensure channel permissions allow `View Channels`, `Send Messages`, and `Read Message History`.
4. Restart the Discord bot process and retest.

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
```

## Example environment variables

```bash
export CAR_DISCORD_BOT_TOKEN="<discord-bot-token>"
export CAR_DISCORD_APP_ID="<discord-application-id>"
```
