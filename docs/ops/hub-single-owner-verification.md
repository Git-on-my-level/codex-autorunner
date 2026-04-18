# Hub Single-Owner Verification

Use this runbook to verify that hub owns shared state, side processes stay on the
control-plane boundary, and health stays responsive while automation is active.

## Automated Coverage

Run the focused regression set first:

```bash
.venv/bin/python -m pytest \
  tests/test_hub_issue_1266.py \
  tests/test_discord_hub_handshake.py \
  tests/test_telegram_hub_handshake.py \
  tests/surfaces/web/routes/test_hub_control_plane_routes.py \
  tests/test_architecture_boundaries.py \
  -q
```

What this proves:

- `tests/test_hub_issue_1266.py` keeps `/health` and `/car/health` responsive while
  deferred startup work is still running.
- `tests/test_discord_hub_handshake.py` and `tests/test_telegram_hub_handshake.py`
  distinguish incompatible hub responses from temporary unavailability in the
  surface startup logs.
- `tests/surfaces/web/routes/test_hub_control_plane_routes.py` preserves the
  typed HTTP distinction between `hub_unavailable` (`503`) and
  `hub_incompatible` (`409`).
- `tests/test_architecture_boundaries.py` blocks Discord and Telegram from
  importing hub-owned shared-state or polling owners directly.

## Live Checks

Run these checks against a real multi-process hub root after deploying the hub
build and side-process build together.

### 1. Confirm the hub endpoint and health

```bash
cat .codex-autorunner/hub_endpoint.json
curl -fsS http://127.0.0.1:4517/health
curl -fsS http://127.0.0.1:4517/car/health
```

Expected result:

- `hub_endpoint.json` points at the running hub process.
- Both health endpoints return quickly with `{"status":"ok",...}`.
- The response includes `hub_startup_phase` (one of `constructed`, `reconciling`,
  `ready`, `started`) and `hub_deferred_startup_complete` (boolean) so you can
  observe startup progress without guessing from logs alone.

### 2. Distinguish incompatibility from hub unavailability

Check the hub log after starting Discord or Telegram:

```bash
rg -n "hub_control_plane\\.handshake_(incompatible|failed)" \
  .codex-autorunner/codex-autorunner-hub.log
```

Interpretation:

- `*.handshake_incompatible` means the side process reached the hub but rejected
  the API/schema contract. Do not wait for retries; upgrade or roll back the
  mismatched build.
- `*.handshake_failed` with `error_code":"hub_unavailable"` means the side
  process could not reach the hub yet. Fix availability first, then retry.

### 3. Check for duplicate ownership

Look for an unclean hub takeover and confirm process ownership:

```bash
rg -n "hub_started_unclean" .codex-autorunner/codex-autorunner-hub.log
car doctor processes --repo <hub_root> --json
```

Expected result:

- No fresh `hub_started_unclean` event during a normal rollout.
- `car doctor processes` shows stable `owner_pid` values and does not require
  forced cleanup to explain active CAR-owned children.

If the hub was restarted unexpectedly, resolve the old owner before trusting the
new process as the single shared-state owner.

### 4. Check health while automation is active

Use one terminal to poll health and another to create real PMA or SCM activity.

```bash
while true; do
  date -u +"%Y-%m-%dT%H:%M:%SZ"
  curl -fsS http://127.0.0.1:4517/health >/dev/null || break
  curl -fsS http://127.0.0.1:4517/car/health >/dev/null || break
  sleep 1
done
```

While that loop is running, trigger one of:

- a PMA wakeup or managed-thread resume on the live hub
- normal SCM polling work by syncing an already-bound PR
- the disposable orchestration canary on a separate scratch root if you only
  need a bounded recovery/load rehearsal:

```bash
tmp_root="$(mktemp -d)"
.venv/bin/python -m codex_autorunner.cli hub orchestration canary \
  --path "$tmp_root" \
  --json
```

Expected result:

- health polling keeps succeeding while automation continues
- no side-process logs indicate local shared-state fallback ownership

## Recovery Guidance

- If you see `handshake_incompatible`, stop the side process and align it with
  the deployed hub build before restarting.
- If you see `handshake_failed` with `hub_unavailable`, recover the hub service
  or endpoint first.
- If you see `hub_started_unclean` or stale process ownership, inspect
  `car doctor processes --repo <hub_root> --json` before using forced cleanup.
