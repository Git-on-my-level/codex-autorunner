# systemd Runbook (Hub + Telegram)

This runbook covers running CAR in hub mode with systemd on Linux. It includes
user and system service guidance plus templates for hub and Telegram.

## Prerequisites

- CAR installed and `car` on disk (use an absolute path from `which car`).
- Hub workspace initialized: `car init --mode hub --path ~/car-workspace`.
- If binding to a non-loopback host, set `server.allowed_hosts`. When exposing
  CAR through a public HTTPS reverse proxy, also set `server.allowed_origins`
  to the public origin for normal browser API/WebSocket requests. Keep
  `server.auth_token_env` for CLI/API automation; first browser access can be
  claimed with `.codex-autorunner/bootstrap-token` (see `docs/web/security.md`).

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
EOF
chmod 600 ~/.config/codex-autorunner/codex-autorunner.env
```

Set the auth token env in `.codex-autorunner/config.yml`:

```yaml
server:
  auth_token_env: CAR_AUTH_TOKEN
  allowed_hosts:
    - your-public-host.example
  allowed_origins:
    - https://your-public-host.example
  # base_path: /car
```

If you set `server.base_path`, also set `CAR_BASE_PATH=/car` in the env file
and update your health check URLs to include the prefix.

For first browser login on a remote host, read the token from
`~/car-workspace/.codex-autorunner/bootstrap-token` after the hub starts and
open `https://host/auth/bootstrap#token=...`.

Set Linux updater defaults in `codex-autorunner.yml` (or `.codex-autorunner/config.yml`)
so `/system/update` and Telegram `/update` use systemd:

```yaml
update:
  backend: systemd-user
  linux_service_names:
    hub: car-hub
    telegram: car-telegram
```

`update.backend` accepts `auto`, `launchd`, `systemd-user`, or `systemd-system`.
Use `systemd-system` when the hub runs as a `/etc/systemd/system` unit (see
"System service" below). On Linux `auto` resolves to `systemd-user`; the refresh
auto-detects the scope only when the user D-Bus session is unreachable, so set
`systemd-system` explicitly for system units. If you pick the wrong scope the
refresh now fails with a clear message instead of silently aborting.

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

### Self-update for a system service

To let `/system/update` (and Telegram/Discord `/update`) manage a system unit,
set the backend to `systemd-system`:

```yaml
update:
  backend: systemd-system
  linux_service_names:
    hub: car-hub
    telegram: car-telegram
    discord: car-discord
```

The refresh runs as the hub's service user, so privileged `systemctl`
(`daemon-reload`, `restart`) calls are wrapped with `sudo -n`. Grant that user
passwordless sudo for systemctl, e.g. in `/etc/sudoers.d/car-hub`:

```
car ALL=(root) NOPASSWD: /bin/systemctl daemon-reload, /bin/systemctl restart car-hub, /bin/systemctl restart car-telegram, /bin/systemctl restart car-discord
```

Override the sudo behavior with `update.systemctl_sudo` (`auto`|`true`|`false`)
in hub config, or the legacy `UPDATE_SYSTEMCTL_SUDO` env var in the unit.
Set `SYSTEMD_SCOPE` (`user`|`system`) in the unit environment when auto-detection
is insufficient; set `update.systemctl_sudo: false` when the hub runs as root.

### Staged updates and routing

Linux self-update now runs through the Python `UpdateEngine`: staged venv build,
DB snapshot, atomic `CURRENT_VENV_LINK` cutover, layered restart, and rollback.
Staged cutover requires the unit `ExecStart` to route through the `car` wrapper
or the current venv symlink. If not, the update fails with remediation unless
you explicitly opt into emergency in-place install:

```yaml
update:
  allow_in_place: true  # loud emergency-only fallback; not recommended
```

Optional escape hatches:

```yaml
update:
  restart_command: ["sudo", "-n", "systemctl", "restart", "car-hub"]
  systemctl_sudo: auto
```

Orchestration no longer lives in `scripts/safe-refresh-local-linux-hub.sh`; that
script is a thin compatibility wrapper. The hub spawns
`python -m codex_autorunner.core.update.runner`.

## Logging and health checks

- Hub logs: `journalctl --user -u car-hub -f`
- Telegram logs: `journalctl --user -u car-telegram -f`
- Health: `curl -fsS http://<host>:<port>/health` (prefix base path if set)
