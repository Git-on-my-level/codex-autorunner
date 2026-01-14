# systemd Runbook (Hub + Telegram)

This runbook covers running CAR in hub mode with systemd on Linux. It includes
user and system service guidance plus templates for hub and Telegram.

## Prerequisites

- CAR installed and `car` on disk (use an absolute path from `which car`).
- Hub workspace initialized: `car init --mode hub --path ~/car-workspace`.
- If binding to a non-loopback host, set `server.auth_token_env` and
  `server.allowed_hosts` (see `docs/web/security.md`).

## Environment file

Create an env file and keep it private:

```
mkdir -p ~/.config/codex-autorunner
cat > ~/.config/codex-autorunner/codex-autorunner.env <<'EOF'
CAR_BASE_PATH=/
CAR_AUTH_TOKEN=replace_me
OPENAI_API_KEY=replace_me
CAR_TELEGRAM_BOT_TOKEN=replace_me
CAR_TELEGRAM_CHAT_ID=replace_me
# Optional: custom app-server command for Telegram.
# CAR_TELEGRAM_APP_SERVER_COMMAND=/home/you/.local/bin/codex app-server
EOF
chmod 600 ~/.config/codex-autorunner/codex-autorunner.env
```

Set the auth token env in `.codex-autorunner/config.yml`:

```yaml
server:
  auth_token_env: CAR_AUTH_TOKEN
  # base_path: /car
```

If you set `server.base_path`, also set `CAR_BASE_PATH=/car` in the env file
and update your health check URLs to include the prefix.

## User service (recommended)

1) Copy `docs/ops/systemd-hub.service` to `~/.config/systemd/user/car-hub.service`.
2) Edit paths, host, and port in the copied unit file.
3) `systemctl --user daemon-reload`
4) `systemctl --user enable --now car-hub`
5) If the box is headless, run `loginctl enable-linger $USER`.
6) `systemctl --user status car-hub`
7) `journalctl --user -u car-hub -f`
8) Health check: `curl -fsS http://127.0.0.1:4173/health`

## Telegram service

1) Copy `docs/ops/systemd-telegram.service` to
   `~/.config/systemd/user/car-telegram.service`.
2) Ensure the env file includes `CAR_TELEGRAM_BOT_TOKEN` and
   `CAR_TELEGRAM_CHAT_ID`.
3) `systemctl --user daemon-reload`
4) `systemctl --user enable --now car-telegram`
5) `journalctl --user -u car-telegram -f`

## System service (root-managed)

If you need a system service:

- Copy the hub unit to `/etc/systemd/system/car-hub.service`.
- Set `User=` and `Group=` in the unit (ex: `User=car`).
- Update `WorkingDirectory` and `EnvironmentFile` (ex: `/etc/codex-autorunner/codex-autorunner.env`).
- `sudo systemctl daemon-reload`
- `sudo systemctl enable --now car-hub`

The same pattern applies to `car-telegram.service`.

## Logging and health checks

- Hub logs: `journalctl --user -u car-hub -f`
- Telegram logs: `journalctl --user -u car-telegram -f`
- Health: `curl -fsS http://<host>:<port>/health` (prefix base path if set)
