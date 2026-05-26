# Runtime Identity Backfill

Runtime identity is stored as a canonical envelope with these stages:
`requested`, `resolved`, `launch`, `effective`, and `projected`.
Historical rows may not have durable evidence for every stage, so the
orchestration migration backfills only facts that can be reconstructed from
stored CAR state.

## Source Precedence

For `orch_thread_executions` rows:

- `resolved` comes from `turn_request_json`.
- `launch` comes from `turn_request_json` plus persisted launch evidence such
  as `backend_turn_id`, `started_at`, `model_id`, `reasoning_level`, and stored
  model payloads. Queued or pending rows without launch evidence keep `launch`
  unknown.
- `effective` is preserved when already present in `runtime_identity_json`.
  The migration does not infer provider-effective runtime from current defaults.
- `projected` remains a read-model fact. It is not invented during SQLite
  migration.

For `orch_automation_child_execution_edges` rows:

- `requested` comes from `requested_runtime_json`.
- `resolved` and `launch` may be copied from the linked child execution
  envelope when `child_id` points at an `orch_thread_executions.execution_id`.
- `effective` comes from `actual_runtime_json` or an already persisted child
  effective stage.

The backfill never uses browser picker memory, frontend local state, current
agent defaults, or hard-coded OpenCode/Codex fallback models as historical
truth.

## Partial And Contradictory Rows

Rows with insufficient evidence keep missing stages as `null` and set envelope
metadata:

- `partial: true`
- `missing_stages: [...]`
- `partial_reason`

When stage values disagree, such as requested model `zai-coding-plan/glm-5.1`
and effective model `glm-5v-turbo`, the migration records `contradictions` in
metadata. Runtime-chain diagnostics also report those differences as drift.

## Inspection

Use runtime-chain diagnostics for one row:

```bash
car doctor runtime-chain --execution-id <execution_id> --json
car doctor runtime-chain --automation-child-edge-id <edge_id> --json
```

Use the broader execution-history diagnostic to list active runtime-chain
invariants:

```bash
car doctor --json
```

The diagnostic code `RUNTIME_CHAIN_PARTIAL_HISTORICAL_BACKFILL` means the row was
migrated with explicit unknown stages because no durable historical evidence
exists. Treat this as an audit fact, not a repair instruction.

## Developer Guardrails

Future migrations should preserve existing envelope stages and fill only missing
stages from durable sources. If evidence is absent, leave the stage unknown and
record why in metadata. Do not add compatibility fallbacks that turn UI input
state or provider defaults into historical runtime facts.
