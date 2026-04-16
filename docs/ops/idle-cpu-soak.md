# Idle CPU Soak Benchmark

Run a reproducible idle-CPU soak against a CAR hub to measure steady-state
resource consumption and sign off against committed budgets.

## Quick start

```bash
# From repo root with the project venv active:
.venv/bin/python scripts/idle_cpu_benchmark.py \
  --profile hub_only \
  --output .codex-autorunner/diagnostics/idle-cpu/
```

*(The harness script does not exist yet; this runbook describes the contract
the future harness must satisfy.)*

## Profile definitions

All profiles live in the committed file **`scripts/idle_cpu_profiles.yml`**.
Each profile specifies:

| Field | Description |
|---|---|
| `services` | Service commands the harness must launch |
| `warmup_seconds` | Seconds to wait before sampling begins |
| `duration_seconds` | Total measurement window |
| `sample_interval_seconds` | Cadence between CPU/memory snapshots |
| `health_probe` | HTTP health check target during the soak |
| `expected_owned_process_categories` | `ProcessCategory` values the harness attributes to CAR |

### Available profiles

| Profile | Topology | Budget type | Target |
|---|---|---|---|
| `hub_only` | Hub API/UI only | **Hard gate** | <5% aggregate CAR CPU |
| `hub_plus_discord` | Hub + Discord bot idle | Comparison | <8% aggregate CAR CPU |
| `hub_plus_telegram` | Hub + Telegram bot idle | Comparison | <8% aggregate CAR CPU |
| `hub_with_idle_runtime` | Hub + idle OpenCode server | Comparison | <10% aggregate CAR CPU |

### Hard gate vs comparison profiles

- **Hard gate** (`hub_only`): the benchmark **must pass** for the campaign to
  succeed.  Failure blocks downstream optimization work.
- **Comparison**: the profile is measured and recorded but does not block the
  campaign.  Data feeds regression tracking across releases.

Only `hub_only` is a hard gate in the first campaign.  Other profiles may be
promoted to hard gates in later campaigns as baselines stabilize.

## Host requirements

- **Quiet local or dedicated machine.** No competing CPU-heavy processes during
  the soak.  Close IDEs, builds, and other CAR instances before running.
- **macOS or Linux.** The harness uses `ps` / `pidstat` (Linux) or `ps` alone
  (macOS) to sample per-process CPU.
- **Python 3.10+** with the project venv active.
- **Network access** to the hub health endpoint (loopback by default).

## Artifact directory

All artifacts are written to:

```
.codex-autorunner/diagnostics/idle-cpu/
```

Each run produces one JSON file named:

```
{profile}_{timestamp}.json
```

Example: `hub_only_2026-04-17T12-00-00Z.json`.

## JSON artifact contract

Every artifact JSON file must conform to this structure (see also the
`artifact_contract` section of `scripts/idle_cpu_profiles.yml`):

```json
{
  "version": 1,
  "profile": "hub_only",
  "started_at": "2026-04-17T12:00:00Z",
  "finished_at": "2026-04-17T12:05:00Z",
  "environment": {
    "hostname": "macbook.local",
    "platform": "darwin",
    "python_version": "3.12.3",
    "car_version": "0.1.0",
    "car_git_ref": "abc1234",
    "cpu_count": 10,
    "total_memory_gb": 32.0
  },
  "aggregate_metrics": {
    "car_owned_cpu_mean_percent": 2.1,
    "car_owned_cpu_max_percent": 4.8,
    "car_owned_cpu_p95_percent": 3.9,
    "car_owned_cpu_samples": 60,
    "car_owned_memory_mean_mb": 85.3,
    "car_owned_memory_max_mb": 92.1
  },
  "per_process_metrics": [
    {
      "alias": "hub",
      "command": "car hub serve",
      "category": "car_service",
      "cpu_mean_percent": 2.1,
      "cpu_max_percent": 4.8,
      "memory_mean_mb": 85.3,
      "memory_max_mb": 92.1
    }
  ],
  "health_probe_summary": {
    "total_probes": 60,
    "successful_probes": 60,
    "failed_probes": 0,
    "last_probe_status": 200
  },
  "signoff": {
    "passed": true,
    "budget_type": "hard",
    "budget_threshold_percent": 5.0,
    "actual_aggregate_cpu_percent": 2.1,
    "message": "hub_only hard budget passed: 2.1% < 5.0%"
  }
}
```

### Required fields

| Section | Fields |
|---|---|
| Top-level | `version`, `profile`, `started_at`, `finished_at` |
| `environment` | `hostname`, `platform`, `python_version`, `car_version`, `cpu_count`, `total_memory_gb` |
| `aggregate_metrics` | `car_owned_cpu_mean_percent`, `car_owned_cpu_max_percent`, `car_owned_cpu_p95_percent`, `car_owned_cpu_samples`, `car_owned_memory_mean_mb`, `car_owned_memory_max_mb` |
| `per_process_metrics` | Array of objects with `alias`, `command`, `category`, `cpu_mean_percent`, `cpu_max_percent`, `memory_mean_mb`, `memory_max_mb` |
| `health_probe_summary` | `total_probes`, `successful_probes`, `failed_probes`, `last_probe_status` |
| `signoff` | `passed`, `budget_type`, `budget_threshold_percent`, `actual_aggregate_cpu_percent`, `message` |

## Separation from existing diagnostics

- The idle soak contract is **separate** from the live `process-monitor.json`
  time-series that the hub writes during normal operation.
- The idle soak contract is **separate** from `check.sh` validation.  It is a
  campaign-specific measurement, not a CI gate.
- The harness must not depend on `process-monitor.json` internals.

## First campaign success metric

The first optimization campaign succeeds when:

1. A `hub_only` soak produces a passing `signoff` with
   `car_owned_cpu_mean_percent < 5.0` on a quiet local or dedicated host.
2. The artifact is checked into `.codex-autorunner/diagnostics/idle-cpu/` for
   regression tracking.
3. Comparison profiles (`hub_plus_discord`, `hub_plus_telegram`,
   `hub_with_idle_runtime`) are measured and recorded but do not need to pass
   a hard gate.

This metric is the **only** success criterion for the first campaign and is now
committed in this document and the profile definitions rather than living only
in chat history.
