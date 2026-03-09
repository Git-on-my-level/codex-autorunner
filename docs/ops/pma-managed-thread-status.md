# PMA Managed-Thread Status Model

CAR uses one normalized operator-facing status for PMA managed threads.

## Status Enum

| Status | Meaning | Terminal |
| --- | --- | --- |
| `idle` | Healthy and waiting for work | no |
| `running` | A managed turn is currently executing | no |
| `paused` | Execution was intentionally compacted/paused | no |
| `completed` | The latest managed turn finished successfully | yes |
| `failed` | The latest managed turn finished unsuccessfully | yes |
| `archived` | The thread is read-only and archived | yes |

## Transition Table

| Current | Signal | Next |
| --- | --- | --- |
| any | `thread_created` | `idle` |
| `paused`, `archived` | `thread_resumed` | `idle` |
| `idle`, `completed`, `failed`, `paused` | `turn_started` | `running` |
| `idle`, `completed`, `failed`, `paused` | `thread_compacted` | `paused` |
| `running` | `managed_turn_completed` | `completed` |
| `running` | `managed_turn_failed` | `failed` |
| `running` | `managed_turn_interrupted` | `failed` |
| any | `thread_archived` | `archived` |

## Persisted Context

Each managed thread persists:

- `normalized_status`
- `status_reason_code`
- `status_updated_at`
- `status_terminal`
- `status_turn_id`

API, CLI, and hub UI surfaces read these persisted fields instead of re-inferring
status from mixed lifecycle signals. Operator-facing payloads keep compatibility
aliases such as `status_reason` and `status_changed_at`, while lifecycle write
admission remains separate in `lifecycle_status`.
