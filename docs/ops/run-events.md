# Canonical run events

CAR writes a canonical, append-only run event stream for each run in:

- `.codex-autorunner/runs/run-<id>.events.canonical.jsonl`

Each line is a JSON object with the following schema:

- `seq` (int): monotonically increasing sequence per run.
- `id` (string): unique event id.
- `run_id` (string): run id.
- `event_type` (string): one of the canonical event types.
- `timestamp` (string): ISO-8601 UTC timestamp.
- `data` (object): event-specific payload.
- `step_id` (string, optional): flow step id when relevant.

The canonical event types reuse `FlowEventType` for all run/flow events:

- Flow lifecycle: `flow_started`, `flow_stopped`, `flow_resumed`, `flow_completed`, `flow_failed`
- Step lifecycle: `step_started`, `step_progress`, `step_completed`, `step_failed`
- Agent/streaming: `agent_stream_delta`, `agent_message_complete`, `agent_failed`
- Tooling: `tool_call`, `tool_result`, `approval_requested`
- App server: `app_server_event`
- Usage: `token_usage`
- Run lifecycle: `run_started`, `run_finished`, `run_state_changed`, `run_no_progress`,
  `run_timeout`, `run_cancelled`
- Artifacts: `plan_updated`, `diff_updated`

Example payloads (non-exhaustive):

- `agent_stream_delta`: `{ "delta": "...", "delta_type": "assistant_stream" }`
- `tool_call`: `{ "tool_name": "...", "tool_input": { ... } }`
- `token_usage`: `{ "usage": { ... } }`
- `run_finished`: `{ "exit_code": 0 }`
- `run_state_changed`: `{ "from_status": "running", "to_status": "idle" }`

Legacy run events are still written to `.codex-autorunner/runs/run-<id>.events.jsonl`
for backward compatibility. New consumers should prefer the canonical stream.
