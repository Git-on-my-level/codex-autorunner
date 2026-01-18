# Telegram Debugging Guide

Use this guide when Telegram replies are missing, delayed, or out of order.

## Quick Triage

- Confirm the bot process is running:
  - `launchctl list | rg -n "codex.*telegram|autorunner.*telegram"`
  - `launchctl print gui/$(id -u)/com.codex.autorunner.telegram`
- Confirm the active log file:
  - The launchd plist `StandardOutPath` is the authoritative log path.
  - Telegram JSON events go to `.codex-autorunner/codex-autorunner-hub.log` (launchd stdout can be empty).

## Follow a Turn End-to-End

1) Locate the Telegram update:
   - Search for `telegram.update.received` with the chat/thread ids.
2) Verify a turn starts:
   - Look for `telegram.turn.starting`.
   - On startup, check for `telegram.commands.updated` to confirm slash-command registration.
3) Verify the app-server request:
   - `app_server.request` with `method:"turn/start"` and the thread id.
4) Verify the turn completes:
   - `app_server.turn.completed` and `telegram.turn.completed`.
5) Verify delivery:
   - `telegram.outbox.enqueued` then `telegram.outbox.delivered`.

If you see `app_server.turn.completed` but no `telegram.turn.completed`, the bot likely dropped the completion event or crashed mid-turn.

## Outbox Checks

- Inspect the outbox:
  - `.codex-autorunner/telegram_state.json` -> `outbox` entries.
- If outbox is empty but no delivery was logged, the response was never enqueued (upstream failure).

## Common Failure Patterns

- **Stuck at "Working..."**
  - `telegram.turn.starting` exists but no `telegram.turn.completed`.
  - Check for app-server disconnects or bot restarts.
- **App-server disconnect**
  - Look for `CodexAppServerDisconnected` or `app_server.*` errors.
  - Ensure `codex app-server` is healthy and reachable.
- **Silent drop**
  - `telegram.turn.completed` exists, but no outbox events.
  - Indicates delivery path skipped or crashed before enqueue.

## Useful Commands

```bash
# Find a thread id in hub log
rg -n "019b" .codex-autorunner/codex-autorunner-hub.log -S

# Show recent Telegram events
rg -n "telegram\\.(update|turn|outbox)" .codex-autorunner/codex-autorunner-hub.log -S | tail -n 200

# Show app-server completion events
rg -n "app_server\\.turn\\.completed" .codex-autorunner/codex-autorunner-hub.log -S | tail -n 50
```

## When to Restart

- If the bot log is stale or missing recent events, restart Telegram:
  - `/update telegram` or
  - `launchctl kickstart -k gui/$(id -u)/com.codex.autorunner.telegram`
