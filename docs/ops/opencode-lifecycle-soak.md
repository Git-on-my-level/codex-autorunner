# OpenCode Lifecycle Soak

Use `scripts/opencode_lifecycle_soak.py` to run a real OpenCode lifecycle soak against a temporary CAR hub/repo layout and capture JSON evidence for:

- registry reuse
- one-shot temporary sessions
- backend non-reused temporary sessions
- idle pruning
- supervisor shutdown
- `car doctor processes` lifecycle diagnostics

## Command

Run the soak from the repo root with the project venv:

```bash
.venv/bin/python scripts/opencode_lifecycle_soak.py --cycles 2 --output /tmp/opencode-lifecycle-soak.json
```

The script exits `0` on signoff and `1` when the captured evidence suggests follow-up work is still needed.

## What it does

The soak creates a temporary CAR hub and repo, writes a repo override with an aggressive OpenCode retention policy, and then exercises three lifecycle phases:

1. `registry_reuse_phase`
2. `one_shot_phase`
3. `backend_phase`

Each phase records:

- managed-process registry state
- `car doctor processes --json` lifecycle summaries
- live handle modes
- pid/pgid/RSS snapshots from `ps`

## Signoff Criteria

The soak signs off only when all of the following are true:

- a CAR-managed registry-reused server is terminated and deregistered
- one-shot temporary sessions do not leave managed registry state behind
- backend non-reused temporary sessions clear backend session state and do not leave managed registry state behind
- final lifecycle diagnostics show no managed OpenCode servers and no live CAR handles

## March 13, 2026 Signoff Run

Command:

```bash
.venv/bin/python scripts/opencode_lifecycle_soak.py --cycles 2 --output /tmp/opencode-lifecycle-soak.json
```

Observed results from the signoff run:

- `registry_reuse_phase.second_close_removed_pid` was `true`, and the registry was empty after reused-server teardown.
- `registry_reuse_phase.doctor_during_reuse` reported one managed server with `last_attach_mode: registry_reuse`, proving the observability path distinguishes reused CAR-managed servers from locally spawned handles.
- `one_shot_phase` reused one spawned-local OpenCode server (`pid 96685`, about `31104 KB` RSS during the run), then `prune_idle()` removed the idle handle and the registry was empty after `close_all()`.
- `backend_phase` reused one spawned-local OpenCode server (`pid 96833`, about `31136 KB` RSS during the run), and every cycle ended with `backend_session_id_after: null` and `backend_temporary_session_id_after: null`.
- `backend_phase.pruned_handles` was `1`, and the registry was empty after `close_all()`.
- `final_doctor` reported `active: 0`, `spawned_local: 0`, `registry_reuse: 0`, `stale: 0`, with no managed servers and no live handles.
- Remaining OpenCode processes in the final doctor snapshot were reported only as unmanaged host processes, which is the expected steady state for this shared development machine.

## Known Caveats

- This signoff run establishes a fixed-build baseline. It does not rerun a pre-fix build in the same environment.
- The OpenCode dispose endpoint currently answers with HTML for the session-dispose route, which CAR already tolerates. The soak therefore treats registry cleanup, backend session-state cleanup, pid exit, and doctor output as the authoritative cleanup signals.
