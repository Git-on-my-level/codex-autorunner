# Validation and Testing Infrastructure

This document describes the lane-based validation system, hermetic test guarantees, and flake-prevention mechanisms.

## Validation Lanes

Validation is split into lanes so that small, scoped changes don't trigger the full check suite. The same classifier is used locally (pre-commit) and in CI.

### Lanes

| Lane | Scope | What it runs |
|------|-------|-------------|
| `core` | Backend/logic | Python lint, typecheck, core pytest, dead-code check |
| `web-ui` | Frontend/UI | Core checks + ESLint, `pnpm run build`, JS tests, asset manifest check |
| `chat-apps` | Chat integrations | Core checks + deterministic chat-surface lab suite (`make test-chat-surface-lab`) |
| `aggregate` | Full validation | All lane checks combined |

### Lane Detection

Changed files are classified by path prefixes and globs defined in `src/codex_autorunner/core/validation_lanes.py`.

- Single-lane diffs route to that lane.
- Multi-lane diffs, shared-risk files (e.g. `pyproject.toml`, `Makefile`, `scripts/`), or unknown paths route to `aggregate`.

### Local Usage

```bash
# Auto-detect lane from staged files (used by pre-commit hook)
./scripts/check.sh

# Force a specific lane
./scripts/check.sh --lane core
./scripts/check.sh --lane web-ui
./scripts/check.sh --lane chat-apps

# Run the chat-surface lab directly
make test-chat-surface-lab

# Force full validation (all lanes)
./scripts/check.sh --full
```

The chat-surface lab command is deterministic and does not require live
Telegram/Discord credentials. It writes budget artifacts to:

```bash
.codex-autorunner/diagnostics/chat-latency-budgets/
```

### CI Routing

CI (`.github/workflows/ci.yml`) uses the same classifier via `scripts/select_ci_validation_jobs.py`:

1. The `route` job computes changed files and emits `lane`, `run_core`, `run_web_ui`, `run_chat_apps`, `run_aggregate` outputs.
2. Exactly one lane job runs; others are skipped.
3. The `check` job asserts the routing contract: exactly one lane selected, it succeeded, and all others were skipped.

## Hermetic Test Guarantees

All tests use isolated temp directories via pytest fixtures (`tmp_path`, `tmp_path_factory`) rather than writing to shared `/tmp` paths.

### Anti-Regression Guard

`scripts/check_test_tmp_usage.py` scans test files for non-hermetic `/tmp` patterns:

- Direct writable `open("/tmp/...")` calls
- `tempfile.TemporaryDirectory(dir="/tmp")` calls
- Root-path kwargs pointing to `/tmp` (e.g. `workspace_root="/tmp"`)
- `Path("/tmp/...").write_text()` and similar path write methods

The guard runs automatically as part of `scripts/check.sh` and CI. Known read-only or resolver-only exceptions are allowlisted in `scripts/test_tmp_usage_allowlist.json`.

### Manual Check

```bash
# Report all /tmp usage (does not fail)
python scripts/check_test_tmp_usage.py --report-only

# Enforce (fails on new unallowlisted violations)
python scripts/check_test_tmp_usage.py
```

## Timing-Flake Prevention

Lifecycle and reliability-critical tests use deterministic synchronization primitives from `tests/support/waits.py` instead of fixed `sleep()` calls:

| Helper | Use Case |
|--------|----------|
| `wait_for_thread_event` | Wait for a `threading.Event` with bounded timeout |
| `wait_for_predicate` | Poll a sync predicate until true |
| `wait_for_async_event` | Wait for an `asyncio.Event` with bounded timeout |
| `wait_for_async_predicate` | Poll an async predicate until true |

All helpers raise `AssertionError` with a descriptive message on timeout. Tests that previously relied on `sleep(N)` for synchronization have been migrated to these primitives.

### Affected Test Modules

The following modules were migrated from timing-based sync to deterministic waits:

- `tests/core/test_hub_lifecycle.py`
- `tests/test_hub_supervisor.py`
- `tests/core/test_pma_lane_worker.py`
- `tests/core/test_pma_queue_cross_process.py`
- `tests/test_lifecycle_events.py`

## Key Files

| File | Purpose |
|------|---------|
| `src/codex_autorunner/core/validation_lanes.py` | Shared lane classifier (lane rules, path classification) |
| `scripts/select_validation_lane.py` | Local lane detection CLI (stdin/file args) |
| `scripts/select_ci_validation_jobs.py` | CI lane routing CLI (emits GitHub Actions outputs) |
| `scripts/check.sh` | Lane-aware validation runner (local pre-commit) |
| `scripts/chat_surface_latency_budgets.py` | Deterministic chat-surface budget suite writer |
| `scripts/check_test_tmp_usage.py` | Hermetic /tmp usage guard |
| `scripts/test_tmp_usage_allowlist.json` | Allowlist for known /tmp exceptions |
| `tests/support/waits.py` | Deterministic wait helpers for tests |
| `.github/workflows/ci.yml` | CI pipeline with lane-based job routing |

## Related Documentation

- [CONTRIBUTING.md](../../CONTRIBUTING.md) — Developer setup and contribution guide
- [running-in-a-vm.md](running-in-a-vm.md) — VM/cloud agent environment notes
- [state-cleanup.md](state-cleanup.md) — CAR state retention and cleanup
- [chat-surface-lab-runbook.md](chat-surface-lab-runbook.md) — Operator and agent runbook for lab execution and scenario authoring
