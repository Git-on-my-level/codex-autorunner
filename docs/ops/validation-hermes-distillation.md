# Hermes Distillation Validation

Date: 2026-02-27

This checkpoint records end-to-end validation for multi-target PMA delivery and destination-backed execution.

## Commands Run

1. Static/lint checks

```bash
.venv/bin/python -m ruff check .
```

2. Full test suite

```bash
.venv/bin/python -m pytest -q
```

3. Optional docker-gated integration test

```bash
CAR_TEST_DOCKER=1 .venv/bin/python -m pytest -q tests/integrations/docker/test_runtime_integration.py
```

4. Multi-target PMA manual smoke (telegram + discord + local)

```bash
.venv/bin/python - <<'PY'
# temp hub root
# configure targets: telegram + discord + local
# invoke deliver_pma_output_to_active_sink(...)
# inspect TelegramStateStore / DiscordStateStore / local deliveries.jsonl
PY
```

5. Docker destination manual smoke (worktree)

```bash
scripts/smoke_destination_docker.sh \
  --hub-root /private/var/folders/.../car-smoke-docker-hub2-2d0aa9h9 \
  --repo-id wt-demo \
  --image busybox:latest \
  --execute
```

## Results

## Full suite + lint

- `ruff check`: passed.
- `pytest -q`: passed (`2209 passed, 4 skipped`).

## Optional docker integration test

- `tests/integrations/docker/test_runtime_integration.py` failed in this environment.
- Failure reason:
  - docker CLI is installed (`docker --version` works),
  - but daemon is not reachable:
  - `Cannot connect to the Docker daemon at unix:///Users/dazheng/.orbstack/run/docker.sock`.

## Multi-target PMA smoke

Observed via real delivery path (`deliver_pma_output_to_active_sink(...)`) using a temporary hub root:

- Telegram outbox: 1 record (`chat_id=123`, `thread_id=456`)
- Discord outbox: 1 record (`channel_id=987654321012345678`)
- Local JSONL updated at:
  - `/var/folders/.../car-smoke-multitarget-.../.codex-autorunner/pma/deliveries.jsonl`
- Delivery call returned `delivered=true`.

This confirms fanout behavior and durable local mirror writes.

## Docker destination smoke

Smoke script now resolves CAR CLI robustly (`car` or `.venv/bin/python -m codex_autorunner.cli`) and executes destination set/show + flow bootstrap/start/status.

For a temporary hub/worktree setup:

- Destination set/show succeeded for worktree repo `wt-demo`.
- Flow runs were created and run artifacts were written under:
  - `/private/var/folders/.../wt-demo/.codex-autorunner/flows/<run_id>/...`
- Worker logs prove docker execution path was invoked:
  - stack includes `integrations/agents/destination_wrapping.py` -> `runtime.ensure_container_running(spec)`.
- Backend session failed because daemon connectivity is unavailable in this environment:
  - `DockerRuntimeError: Cannot connect to the Docker daemon ...`

So docker path wiring is exercised, but successful daemon-backed execution could not be validated on this host.

## Follow-up Tickets

- `.codex-autorunner/tickets/TICKET-900-docker-daemon-readiness-validation.md`
  - capture daemon-readiness diagnostics and make docker-smoke validation reproducible when daemon is unavailable.

## Notes

- During this checkpoint, `scripts/smoke_destination_docker.sh` was hardened to:
  - avoid relying on `car` being on PATH,
  - fall back to `.venv/bin/python -m codex_autorunner.cli`,
  - canonicalize hub root path (`pwd -P`) to reduce symlink path pitfalls in temp environments.
