# Running in a VM / Cloud Agent Environment

Notes for running codex-autorunner inside containerized or cloud-provisioned VMs
(e.g. Cursor Cloud agents, Codespaces, CI runners).

## Services overview

| Service | How to run | Notes |
|---------|-----------|-------|
| Web Hub (FastAPI/Uvicorn) | `make serve-dev` (port 4173) | Set `CAR_DEV_INCLUDE_ROOT_REPO=1` to include the repo itself in the hub |
| Python tests | `make test` or `.venv/bin/python -m pytest -m "not integration"` | Uses pytest with xdist for parallel runs |
| Linting | `make check` (full suite) or individually: `black --check`, `ruff check`, `mypy`, `pnpm lint` | See `scripts/check.sh` for the full pre-commit check sequence |
| TS build | `pnpm run build` or `make build` | Compiles `static_src/*.ts` → `static/*.js`; always rebuild after TS changes |

## Startup caveats

- **`python3.12-venv` system package**: Required but not pre-installed on Ubuntu 24.04 cloud VMs. Install via `sudo apt-get install -y python3.12-venv` before `make setup`.
- **Hub init**: Before running the dev server for the first time, run `CAR_DEV_INCLUDE_ROOT_REPO=1 .venv/bin/car init --mode hub` to bootstrap `.codex-autorunner/`.
- **Dev server binding**: Use `--host 0.0.0.0` (not the default `127.0.0.1`) to make the UI accessible outside the VM.
- **Process termination tests**: A few tests in `tests/test_opencode_supervisor_process_management.py` and `tests/test_process_termination.py` may fail in containerized environments due to PID namespace / signal handling constraints. These are environment-specific, not code bugs.
- **No external services required**: SQLite is embedded (stdlib); no Postgres/Redis/Docker needed for core dev workflows.

## Optional Docker Profile Probe

Use this only when validating a real image for `destination.profile: full-dev`. It is integration-marked and skipped by default:

```bash
CAR_TEST_DOCKER_FULL_DEV=1 \
CAR_TEST_DOCKER_FULL_DEV_IMAGE=ghcr.io/your-org/your-image:tag \
.venv/bin/python -m pytest -q tests/integrations/docker/test_full_dev_profile_probe.py -m integration
```

What it checks:
- Docker daemon reachable
- Container image can be started
- All `full-dev` required binaries are present (`codex`, `opencode`, `python3`, `git`, `rg`, `bash`, `node`, `pnpm`)
