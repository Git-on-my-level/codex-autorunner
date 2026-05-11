# Running in a VM / Cloud Agent Environment

Notes for running codex-autorunner inside containerized or cloud-provisioned VMs
(e.g. Cursor Cloud agents, Codespaces, CI runners).

## Services overview

| Service | How to run | Notes |
|---------|-----------|-------|
| Web Hub (dev: FastAPI reload + Vite HMR) | `make serve` — hub on **4173**, Web UI on **5173** (`WEB_DEV_PORT`) | Open the printed **Vite** URL; `CAR_DEV_INCLUDE_ROOT_REPO=1` is set by the script |
| Python tests | `make test` or `.venv/bin/python -m pytest -m "not integration"` | Serial by default; use `-n auto` for xdist parallelism |
| Lane-aware checks | `./scripts/check.sh` (auto-detect) or `./scripts/check.sh --lane <lane>` | Lanes: `core`, `web-ui`, `web-core-contract`, `chat-apps`, `aggregate` (full) |
| Full validation | `./scripts/check.sh --full` or `make check-full` | Runs all lanes plus extended checks |
| Linting | `black --check src tests`, `ruff check src tests`, `make typecheck-strict` | Individual linters for targeted runs |
| Web Hub build | `pnpm run build` or `make build` | Builds the default Svelte UI in `src/codex_autorunner/web_frontend/` → `src/codex_autorunner/web_static/`; always rebuild after Web UI changes |

## Startup caveats

- **`python3.12-venv` system package**: Required but not pre-installed on Ubuntu 24.04 cloud VMs. Install via `sudo apt-get install -y python3.12-venv` before `make setup`.
- **Hub init**: Before running the dev server for the first time, run `CAR_DEV_INCLUDE_ROOT_REPO=1 .venv/bin/car init --mode hub` to bootstrap `.codex-autorunner/`.
- **Dev server binding**: Use `make serve HOST=0.0.0.0 WEB_DEV_PORT=5173` (defaults shown) so both the hub and Vite listen outside localhost. Health check: `curl http://localhost:4173/health`. For production-like static PMA only (no HMR), use `make serve-hub`.
- **Process termination tests**: A few tests in `tests/test_opencode_supervisor_process_management.py` and `tests/test_process_termination.py` may fail in containerized environments due to PID namespace / signal handling constraints. These are environment-specific, not code bugs.
- **Text delta coalescer**: `tests/unit/test_text_delta_coalescer.py::test_multibyte_unicode_newline` may occasionally error in containerized VMs. This is environment-specific.
- **Test suite is large**: ~6300+ tests. Run via `make test` (serial, `-m "not integration"`) or in parallel with `-n auto` (used by `scripts/check.sh`).
- **Lane-based validation**: `scripts/check.sh` auto-detects the appropriate lane from staged files. Backend-only changes run the `core` lane (no frontend build); UI changes run `web-ui`; scoped core + Web surface contract changes run `web-core-contract`; chat integration changes run `chat-apps`. Broad multi-lane or shared-risk diffs fall back to `aggregate` (full checks). Force full checks with `--full`.
- **Tests are hermetic**: Tests use isolated temp directories (via fixtures and `tmp_path`). A guard script (`scripts/check_test_tmp_usage.py`) runs as part of the standard check flow and blocks new non-hermetic `/tmp` writable patterns in tests. Known read-only exceptions are allowlisted in `scripts/test_tmp_usage_allowlist.json`.
- **No external services required**: SQLite is embedded (stdlib); no Postgres/Redis/Docker needed for core dev workflows.
- **Pre-commit subset**: The pre-commit hook (`scripts/check.sh`) runs `pytest -m "not integration and not slow"` with lane-aware scoping. Use the same marker set when iterating locally to match pre-commit behavior.
- **Individual lint commands**: `black --check src tests`, `ruff check src tests`, `pnpm lint`, `make typecheck-strict`. The full suite is `make check`.

## Optional Docker Profile Probe

Use this only when validating a real image for `destination.profile: full-dev`. It is integration-marked and skipped by default:

```bash
CAR_TEST_DOCKER_FULL_DEV=1 \
CAR_TEST_DOCKER_FULL_DEV_IMAGE=ghcr.io/your-org/your-image:tag \
.venv/bin/python -m pytest -q tests/adapters/docker/test_full_dev_profile_probe.py -m integration
```

What it checks:
- Docker daemon reachable
- Container image can be started
- All `full-dev` required binaries are present (`codex`, `opencode`, `python3`, `git`, `rg`, `bash`, `node`, `pnpm`)
