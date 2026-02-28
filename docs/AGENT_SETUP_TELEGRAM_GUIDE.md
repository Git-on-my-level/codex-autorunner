# CAR Telegram Setup Add-On (for Agents)

Use this guide after `docs/AGENT_SETUP_GUIDE.md` when a user wants the interactive Telegram bot surface.

---

## Instructions for the Agent

You are helping a user enable Telegram for CAR. Keep this as an optional add-on path.

### Step 1: Confirm Telegram Is the Right Add-On

Ask the user if they want:

1. **Interactive Telegram bot** (this guide) - chat with CAR from Telegram, run commands, and work from mobile.
2. **Notifications only** - one-way alerts to Telegram/Discord without interactive chat.

Use this guide only for the interactive bot flow.

### Step 2: Check Telegram Prerequisites

Verify the user has:

1. Completed base setup from `docs/AGENT_SETUP_GUIDE.md`.
2. A Telegram account.
3. A bot token from BotFather.
4. Optional dependencies installed if needed:

```bash
pip install "codex-autorunner[telegram]"
# local dev checkout
pip install -e ".[telegram]"
```

If they installed CAR with `pipx`, reinstall with extras:

```bash
pipx uninstall codex-autorunner
pipx install "codex-autorunner[telegram]"
```

### Step 3: Create Bot and Configure BotFather Privacy

In BotFather:

1. Run `/newbot` and create the bot.
2. Run `/setprivacy`, pick the bot, then choose **Disable**.

Why this matters:
- In group chats, privacy mode ON usually allows commands but blocks normal messages.
- CAR needs normal messages for non-command chat turns.

### Step 4: Decide Chat Topology (DM vs Supergroup Topics)

Ask the user which pattern they want:

1. **DM with the bot** - fastest to set up.
2. **Supergroup + topics** - better for team routing and per-topic isolation.

If using supergroups/topics:
- Enable Topics in Telegram group settings.
- Set `telegram_bot.require_topics: true` so CAR only handles topic messages.
- Consider `telegram_bot.trigger_mode: mentions` to reduce accidental runs in busy groups.

### Step 5: Add Minimal Telegram Config

In `codex-autorunner.yml` (or repo/hub override), add:

```yaml
telegram_bot:
  enabled: true
  bot_token_env: CAR_TELEGRAM_BOT_TOKEN
  allowed_chat_ids:
    - -1001234567890
  allowed_user_ids:
    - 123456789
  require_topics: false
  trigger_mode: all
```

Set environment variables:

```bash
export CAR_TELEGRAM_BOT_TOKEN="<bot-token>"
# Optional override if codex is not on PATH for the bot process:
# export CAR_TELEGRAM_APP_SERVER_COMMAND="/absolute/path/to/codex app-server"
```

ID discovery notes:
- If IDs are unknown, start the bot once and send a message, then read `telegram.allowlist.denied` in logs to capture `chat_id` and `user_id`.
- After allowlisting works, run `/ids` in Telegram to confirm IDs and copy the exact allowlist snippet.

### Step 6: Start and Verify First Run

Run:

```bash
car doctor
car telegram health --path <hub_or_repo_root>
car telegram start --path <hub_or_repo_root>
```

In Telegram, verify this sequence:

1. `/help` responds.
2. `/ids` shows expected chat/user/thread values.
3. `/bind <repo_id|path>` binds the topic/chat to a workspace.
4. `/new` starts a fresh thread.
5. Send a normal non-command message and confirm agent output.

If `trigger_mode: mentions`, also verify a message without mention does not trigger, while `@<bot_username> ...` does trigger.

### Step 7: PMA Usage and Discovery (Optional)

Telegram supports PMA mode for hub-scoped turns and PMA delivery target management.

1. Enable PMA mode in a chat/topic:
   - `/pma on`
2. Verify PMA mode and targets:
   - `/pma status`
   - `/pma targets`
3. Manage PMA delivery targets:
   - `/pma target add <ref>`
   - `/pma target rm <ref>`
   - `/pma target clear`
   - Canonical target refs:
     - `here`
     - `web`
     - `local:<path>`
     - `telegram:<chat_id>[:<thread_id>]`
     - `discord:<channel_id>`
     - `chat:telegram:<chat_id>[:<thread_id>]`
     - `chat:discord:<channel_id>`
4. Exit PMA mode:
   - `/pma off`

Discovery shortcuts:
- `/help` (Telegram command catalog)
- `/pma` (prints PMA usage + accepted target refs)
- `car pma --help`
- `car pma targets --help`
- `car pma targets list --json --path <hub_root>` (for discoverability + validation)

Notes:
- If PMA is disabled in hub config, `/pma` returns an actionable error (`pma.enabled: true`).
- In PMA mode (or unbound context), `/flow status` and `/flow runs` show hub-wide flow overview.
- `/pma target add` and `/pma target rm` validate against canonical refs and return actionable format errors.

### Step 8: Group Permission Checklist

Required or likely-needed permissions in group contexts:

1. Bot can send messages in the target chat/topic.
2. Bot is allowed to receive regular messages (privacy mode OFF via `/setprivacy`).
3. If the group has restricted media/attachments, permit bot media sends so long responses and attachments can be delivered.

Most setups do not require full admin rights unless your group policy enforces tighter restrictions.

---

## Troubleshooting

### Commands work, normal messages do not

Check these first:

1. BotFather privacy mode is still ON:
   - Re-run `/setprivacy` and disable for this bot.
2. `telegram_bot.trigger_mode` is `mentions`:
   - Mention the bot or reply to a bot message, or switch back to `all`.
3. Topic requirement mismatch:
   - If `telegram_bot.require_topics: true`, send messages inside a forum topic (not the root chat).
4. Allowlist mismatch:
   - Confirm both `allowed_chat_ids` and `allowed_user_ids` include current values from `/ids`.

### No response at all

1. Validate token/env:
   - `CAR_TELEGRAM_BOT_TOKEN` must be set for the running process.
2. Re-run:
   - `car doctor`
   - `car telegram health --path <hub_or_repo_root>`
3. Confirm workspace is bound:
   - `/bind <repo_id|path>`

### App-server or runtime failures

If turns fail or hang:
- Check `telegram.turn.failed`, `telegram.turn.timeout`, and `telegram.app_server.start_failed`.
- Verify `codex app-server` is available or set `CAR_TELEGRAM_APP_SERVER_COMMAND`.

---

## Logs and Events to Check First

Default hub log path:
- `.codex-autorunner/codex-autorunner-hub.log`

High-signal Telegram events:
- `telegram.update.received`
- `telegram.allowlist.denied`
- `telegram.turn.completed`
- `telegram.turn.failed`
- `telegram.turn.timeout`
- `telegram.app_server.start_failed`

Quick grep:

```bash
rg -n "telegram\\.(allowlist\\.denied|update\\.received|turn\\.completed|turn\\.failed|turn\\.timeout|app_server\\.start_failed)" .codex-autorunner/codex-autorunner-hub.log
```

---

## Related Docs

- `docs/ops/telegram-bot-runbook.md`
- `docs/ops/telegram-debugging.md`
- `docs/telegram/architecture.md`
