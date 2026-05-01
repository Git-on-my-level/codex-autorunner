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

If the user wants Hermes as the backend agent for Telegram, they should also
complete `docs/ops/hermes-acp.md` first and ensure `hermes` is available on
`PATH`. Telegram then discovers Hermes through the normal agent registry and
keeps unsupported actions capability-gated.

### Step 3: Create Bot and Configure BotFather Privacy

In BotFather:

1. Run `/newbot` and create the bot.
2. Run `/setprivacy`, pick the bot, then choose **Disable**.

Why this matters:
- In group chats, privacy mode ON usually allows commands but blocks normal messages.
- CAR needs normal messages for non-command chat turns.

### Step 4: Decide Chat Topology (DM vs Supergroup Topics)

Ask the user which pattern they want:

1. **Personal setup** - DM with the bot, or one dedicated topic for one operator.
2. **Collaborative setup** - a shared supergroup where different topics may be active, command-only, or silent.

For the personal path:
- Prefer a DM with the bot when possible.
- A dedicated topic also works if the group is effectively private to one operator.
- Legacy `telegram_bot.allowed_chat_ids`, `allowed_user_ids`, `trigger_mode`, and
  `require_topics` are still valid for this path, but plain-text turns stay quiet
  until you activate the chat with `/bind` or `/pma on`.

If using supergroups/topics:
- Enable Topics in Telegram group settings.
- Set `telegram_bot.require_topics: true` so CAR only handles topic messages.
- Consider `telegram_bot.trigger_mode: mentions` to reduce accidental runs in busy groups.
- Prefer `collaboration_policy.telegram.destinations` for explicit topic behavior:
  - `mode: active` for collaboration topics
  - `mode: command_only` for slash/explicit-command topics
  - `mode: silent` for human-only topics

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

After the bot starts, activate the DM or topic with `/bind <repo_id|path>` or
`/pma on` before expecting plain-text turns.

For a shared supergroup with one active topic and a silent root chat:

```yaml
telegram_bot:
  enabled: true
  bot_token_env: CAR_TELEGRAM_BOT_TOKEN
  allowed_chat_ids:
    - -1001234567890
  allowed_user_ids:
    - 123456789
    - 987654321

collaboration_policy:
  telegram:
    allowed_chat_ids:
      - -1001234567890
    allowed_user_ids:
      - 123456789
      - 987654321
    require_topics: true
    destinations:
      - { chat_id: -1001234567890, thread_id: 111, mode: active, plain_text_trigger: mentions }
      - { chat_id: -1001234567890, thread_id: 222, mode: silent }
      - { chat_id: -1001234567890, mode: silent }
```

Set environment variables:

```bash
export CAR_TELEGRAM_BOT_TOKEN="<bot-token>"
```

ID discovery notes:
- If IDs are unknown, start the bot once and send a message, then read `telegram.allowlist.denied` in logs to capture `chat_id` and `user_id`.
- After allowlisting works, run `/ids` in Telegram to confirm IDs and copy the exact allowlist and `collaboration_policy.telegram.destinations` snippet for the current chat/topic.

Migration notes:
- Existing DM or dedicated-topic installs do not need to migrate if they already
  behave the way the operator wants.
- Existing shared groups should migrate to explicit
  `collaboration_policy.telegram.destinations` instead of relying on one
  chat-wide trigger setting.
- For shared groups, the safest default migration is:
  1. keep current allowlists
  2. add `require_topics: true` or a root-chat `mode: silent` destination
  3. mark only the intended topics as `mode: active` or `mode: command_only`

### Step 6: Start and Verify First Run

Run:

```bash
car doctor
car telegram health --path <hub_or_repo_root>
car telegram start --path <hub_or_repo_root>
```

In Telegram, verify this sequence:

1. `/help` responds.
2. `/ids` shows expected chat/user/thread values and a copy-paste collaboration snippet.
3. `/bind <repo_id|path>` binds the topic/chat to a repo workspace root and lets
   CAR keep a consistent durable thread for that destination.
4. `/status` shows the effective collaboration mode and plain-text trigger for the current root chat or topic.
5. `/new` starts a fresh thread.
6. Send a normal non-command message and confirm agent output.

If `trigger_mode: mentions`, also verify a message without mention does not trigger, while `@<bot_username> ...` does trigger.
If using topic destinations, also verify a `mode: silent` topic stays quiet and a `mode: command_only` topic only responds to commands.

### Step 7: Group Permission Checklist

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
   - If you configured topic destinations without `require_topics: true`, add an explicit root-chat destination such as `{ chat_id: <group>, mode: silent }` to keep the root quiet.
4. Allowlist mismatch:
   - Confirm both `allowed_chat_ids` and `allowed_user_ids` include current values from `/ids`.
   - If using `collaboration_policy.telegram.destinations`, confirm the current `thread_id` matches the snippet from `/ids`.

### Migrating an existing Telegram setup to collaboration mode

Use this when a legacy Telegram install worked for one operator but now needs to
support a shared supergroup safely:

1. Keep the existing `allowed_chat_ids` and `allowed_user_ids`; they still
   intersect and remain the admission gate.
2. Run `/ids` in the root chat and each topic you care about.
3. Add `collaboration_policy.telegram.destinations` for each active or human-only
   topic using the exact IDs from `/ids`.
4. Gate the root chat explicitly with either:
   - `telegram_bot.require_topics: true`, or
   - `{ chat_id: <group>, mode: silent }`
5. Re-run `car doctor` and check the compiled collaboration summary plus any
   root-chat warning.
6. Re-test with `/status` in each topic to confirm the effective mode and
   plain-text trigger.

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
- Verify `codex app-server` is available or configure `telegram_bot.app_server_command`.

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
- `docs/telegram/telegram-integration.md`
