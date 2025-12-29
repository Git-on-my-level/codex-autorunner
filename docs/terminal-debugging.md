# Terminal Debugging Guide

This guide explains how to debug terminal replay/scrollback issues in CAR.

## Quick enable

1) Add `?terminal_debug=1` to the CAR UI URL and reload.
   - This enables client console logs and adds `terminal_debug=1` to the
     terminal WebSocket so the server logs attach details.
2) Optional: set `localStorage.setItem("codex_terminal_debug", "1")` to keep
   client logs on without the URL param (server logs still need the param).

## Where logs live

- Repo server log: `.codex-autorunner/codex-server.log`
- Runner log: `.codex-autorunner/codex-autorunner.log`
- Hub log (launchd): `~/car-workspace/.codex-autorunner/codex-autorunner-hub.log`

## Useful endpoints

- Session registry (hub):
  - `GET /car/repos/<repo>/api/sessions`
- Example (local hub):
  - `http://127.0.0.1:4517/car/repos/codex-autorunner/api/sessions`

## Client debug signals

With `terminal_debug=1`, the browser console logs:

- `connect` — mode, whether replay is expected, saved session id
- `hello` — session id assigned by the server
- `replay_end` — replay chunk count/bytes and whether an alt-screen prelude
  was applied
- `first_live_reset` — whether we reset on first live data after empty replay
- `alt-buffer state` — whether xterm is in the alternate buffer and the current
  alt scrollback size

## Server attach debug

When `terminal_debug=1` is present on the terminal WebSocket request, the
server logs one line on attach with:

- session id
- alt-screen active state (as tracked from PTY output)
- replay buffer size (bytes) and chunk count

## Common failure modes

- No scrollback on iOS while attached:
  - Check `alt-buffer state` logs. If `active=true` and `scrollback=0`, the
    client is in alt-screen without captured scrollback. This usually points
    to replay/render timing or missing alt-screen tracking.
  - If `active=false` but the UI is a TUI, the server may have misdetected
    alt-screen state or the session started before alt-screen tracking was
    added. Restart the terminal session to refresh tracking.

- Missing output between prompts:
  - If attach prelude forced alt-screen when the session was actually in the
    normal buffer, output can be rendered in the alternate buffer and later
    disappear. Confirm the server attach debug line shows `alt_screen=false`
    for such sessions.

## Notes

- Attach behavior depends on server-side alt-screen tracking, which is driven
  by escape sequences emitted by the PTY (`?1049h` enter, `?1049l` exit, etc.).
- Existing sessions created before alt-screen tracking may report `false` until
  new output is emitted; restarting the session gives the cleanest signal.
