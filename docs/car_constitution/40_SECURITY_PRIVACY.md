# Security & Privacy Posture

CAR defaults to **YOLO** (full permissions) under an assumed isolated-workspace model. This doc encodes safe *structural* practices without adding runtime friction.

## Threat model (pragmatic)
- Primary risk is accidental blast radius (wrong repo, wrong workspace, wrong secrets).
- Secondary risk is credential leakage via logs/artifacts.
- Runtime permissions are permissive by default; mitigation is environment scoping + auditability.

## Structural mitigations
- Prefer isolated workspaces (worktrees/containers) and cheap reset/recreate.
- Scope credentials to the minimum needed for the task.
- Keep secrets out of logs and durable artifacts by default.
- Centralize where secrets are loaded so audit is possible.

## Optional safety postures (opt-in)
Examples of knobs a deployment may enable:
- no-network mode
- dry-run mode
- command allow/deny lists
- “no git push” guardrails
- write restrictions outside workspace root

## Privacy rules
- Treat run artifacts as potentially shareable; redact secrets at source.
- Avoid copying full sensitive documents into logs; store references and hashes where possible.
