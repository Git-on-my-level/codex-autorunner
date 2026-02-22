# Discord Surface

Discord bot surface and adapters.

## Setup

1. Create a Discord application and bot in the Discord Developer Portal.
2. Copy the bot **Token** and **Application ID**.
3. Invite the bot to your server with OAuth2 scopes:
   - `bot`
   - `applications.commands`
4. Configure `discord_bot.enabled: true` and allowlists in `codex-autorunner.yml`.
5. Configure command registration:
   - development: `command_registration.scope: guild` with at least one `guild_id`
   - production: `command_registration.scope: global`
6. Register commands:
   - `car discord register-commands`
7. Start the bot:
   - `car discord start`

Recommended during development: use guild-scoped command registration so command updates propagate quickly.

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
