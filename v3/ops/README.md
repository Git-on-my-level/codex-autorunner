# Running `card serve` as a supervised service

Two templates in this directory keep CAR v3's daemon (`card serve`) running under
`KeepAlive`/`Restart=always`. The daemon is crash-only (SQLite is the sole source of
truth — see `../DESIGN.md` non-negotiable #1), so an aggressive restart policy is safe
by design: kill it any way you like, the supervisor brings it back, and it resumes from
table state with nothing lost.

Both templates expect `bun install` to have already been run inside `v3/` (see
`../README.md`).

## macOS — launchd

1. Copy the template out of the repo (don't edit it in place, so it stays generic for
   future re-installs):
   ```bash
   cp ops/com.car.card.plist ~/Library/LaunchAgents/com.car.card.plist
   ```
2. Fill in the placeholders in your copy:
   - `<<BUN_PATH>>` → output of `which bun` (e.g. `/Users/you/.bun/bin/bun`)
   - `<<CAR_V3_DIR>>` → absolute path to this `v3/` checkout
   - `<<HOME_DIR>>` → your home directory
   - `<<CAR_TELEGRAM_TOKEN>>` → your bot token (see `../README.md` "Telegram bot
     setup"); omit the whole `CAR_TELEGRAM_TOKEN` dict entry if you're not running
     Telegram yet
   - `<<ANTHROPIC_API_KEY>>` → your Anthropic API key
3. Create the log directory (launchd does not create parent dirs for you):
   ```bash
   mkdir -p ~/.car/log
   ```
4. Bootstrap and start it:
   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.car.card.plist
   launchctl kickstart -k gui/$(id -u)/com.car.card
   ```
5. Check it's up:
   ```bash
   launchctl print gui/$(id -u)/com.car.card | head -30
   curl -s http://127.0.0.1:7171/healthz
   ```

**Logs**: `~/.car/log/card.out.log` and `~/.car/log/card.err.log` (paths set by the
plist's `StandardOutPath`/`StandardErrorPath`). Tail with `tail -f ~/.car/log/card.err.log`.

**Stop / uninstall**:
```bash
launchctl bootout gui/$(id -u)/com.car.card
rm ~/Library/LaunchAgents/com.car.card.plist
```

**After editing the plist**: `launchctl bootout` then `bootstrap` again (launchd does
not hot-reload plist changes).

## Linux — systemd (user unit)

A user unit needs no root and stays scoped to your account.

1. Copy the template:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp ops/card.service ~/.config/systemd/user/card.service
   ```
2. Fill in the placeholders in your copy (same set as the launchd template above):
   `<<BUN_PATH>>` (`which bun`), `<<CAR_V3_DIR>>`, `<<HOME_DIR>>`,
   `<<CAR_TELEGRAM_TOKEN>>`, `<<ANTHROPIC_API_KEY>>`.
3. Reload systemd's view of unit files, then enable + start:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now card.service
   ```
4. So the service keeps running after you log out (user units stop by default on
   logout unless lingering is enabled):
   ```bash
   loginctl enable-linger "$USER"
   ```
5. Check it's up:
   ```bash
   systemctl --user status card.service
   curl -s http://127.0.0.1:7171/healthz
   ```

**Logs**: captured by journald by default (the `StandardOutput`/`StandardError` lines
in the unit are commented out — uncomment them to also mirror to
`~/.car/log/card.{out,err}.log`, and `mkdir -p ~/.car/log` first if you do). Read
journald logs with:
```bash
journalctl --user -u card.service -f          # follow
journalctl --user -u card.service -n 200      # last 200 lines
```

**Stop / uninstall**:
```bash
systemctl --user disable --now card.service
rm ~/.config/systemd/user/card.service
systemctl --user daemon-reload
```

**After editing the unit**: `systemctl --user daemon-reload` then
`systemctl --user restart card.service`.

## Common to both

- The daemon binds `127.0.0.1:7171` by default (`~/.car/config.toml` → `[http]`); it
  is not exposed off-box unless you deliberately change `http.host` and add bearer
  tokens under `[http.ingest_tokens]` (see `../README.md`).
- `card status` and `card doctor` (run manually, from `v3/`, while the service is up)
  are the fastest way to sanity-check a supervised install:
  ```bash
  bun run src/cli.ts status
  bun run src/cli.ts doctor
  ```
- If the service won't start, check the log paths above first; a bad/missing
  `CAR_TELEGRAM_TOKEN` will not crash the daemon (Telegram is optional — see
  `telegram.enabled` in config), but a malformed `~/.car/config.toml` will.
