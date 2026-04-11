# Execution-History Canary

Use the orchestration canary to seed a disposable hub root with oversized historical
execution data, run the migration and compaction flow, and verify that startup,
checkpoint-based recovery, and trace inspection still behave correctly afterward.

## Command

```bash
tmp_root="$(mktemp -d)"
.venv/bin/python -m codex_autorunner.cli hub orchestration canary \
  --path "$tmp_root" \
  --json
```

The command mutates the directory passed via `--path`. Use a temporary or otherwise
disposable location rather than a live hub root.

## What It Validates

- seeds realistic completed and failed executions with oversized hot timelines
- backfills cold trace manifests and compact checkpoints
- compacts hot rows to the configured policy and vacuums SQLite afterward
- measures `/health` startup on the migrated hub root
- exercises managed-thread checkpoint recovery for plain PMA and Discord-bound threads
- inspects a heavy execution through the trace inspection API to confirm tool calls,
  intermediate output deltas, terminal state, and checkpoint metadata remain available

## Output Highlights

The JSON payload includes:

- `before.audit` and `after.audit` for hot-row and manifest/checkpoint counts
- `migration`, `compaction`, and `after.vacuum` summaries
- `startup.duration_seconds`
- `recovery.duration_seconds` plus PMA and Discord-bound checkpoint-restore results
- `trace_validation` for the preserved heavy execution
