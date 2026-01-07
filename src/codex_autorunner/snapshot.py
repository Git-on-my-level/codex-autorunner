from .core import snapshot as core_snapshot

SeedContext = core_snapshot.SeedContext
SnapshotError = core_snapshot.SnapshotError
SnapshotResult = core_snapshot.SnapshotResult
build_snapshot_prompt = core_snapshot.build_snapshot_prompt
collect_seed_context = core_snapshot.collect_seed_context
load_snapshot = core_snapshot.load_snapshot
load_snapshot_state = core_snapshot.load_snapshot_state
redact_text = core_snapshot.redact_text
summarize_changes = core_snapshot.summarize_changes

_run_codex = core_snapshot._run_codex


def generate_snapshot(engine):
    core_snapshot._run_codex = _run_codex
    return core_snapshot.generate_snapshot(engine)


__all__ = [
    "SeedContext",
    "SnapshotError",
    "SnapshotResult",
    "_run_codex",
    "build_snapshot_prompt",
    "collect_seed_context",
    "generate_snapshot",
    "load_snapshot",
    "load_snapshot_state",
    "redact_text",
    "summarize_changes",
]
