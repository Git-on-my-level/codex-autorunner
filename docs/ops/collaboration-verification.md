# Collaboration Verification Runbook

This runbook is the repo-local verification checklist for the shared Telegram and
Discord collaboration rollout. It is designed to be runnable by an agent without
human interaction.

## Purpose

Verify that:

- the shared collaboration policy evaluator behaves as expected
- Telegram still preserves the easy personal DM path and enforces topic-level
  collaboration controls
- Discord still preserves the personal dedicated-channel path and enforces
  active, silent, denied, command-only, and mention-only collaboration behavior
- doctor output and cross-surface parity checks remain aligned with runtime
  behavior

## Fast verification

Run the focused collaboration suite:

```bash
make test-managed-thread-cutover
```

Or run the collaboration-specific subset:

```bash
./.venv/bin/pytest \
  tests/integrations/chat/test_collaboration_policy.py \
  tests/test_telegram_trigger_mode.py \
  tests/test_telegram_bot_integration.py \
  tests/test_doctor_checks.py \
  tests/integrations/discord/test_message_turns.py \
  tests/integrations/discord/test_service_routing.py \
  tests/integrations/discord/test_doctor_checks.py \
  tests/integrations/discord/test_config.py \
  tests/integrations/discord/test_allowlist.py \
  tests/test_cross_surface_parity.py
```

This is the default verification command for collaboration tickets.
It also runs the orchestration-ingress guardrails that catch transport-owned
ordinary-turn shortcuts in the Telegram and Discord entrypoints.

## What this covers

- Shared evaluator:
  - admission without filters is denied
  - actor/container filters intersect
  - `command_only` disables plain text but keeps commands
  - `silent` disables both commands and plain text
  - mentions-only plain text requires explicit invocation
  - Telegram `require_topics` denies root-chat traffic directly at the policy layer
- Telegram:
  - personal DM path without collaboration policy
  - personal DM path with mentions trigger
  - collaborative topic with explicit destinations
  - silent topic
  - denied participant
  - mentions-only topic behavior
  - doctor migration/privacy/root-chat guidance
- Discord:
  - personal bound channel path without collaboration policy
  - mentions-only active channel
  - silent destination
  - denied participant
  - command-only destination
  - quiet unbound allowlisted channel behavior
  - status/ids operator UX
  - doctor migration/default-mode guidance
- Cross-surface parity:
  - Discord still routes plain-text triggering through the shared collaboration
    bridge and turn-policy primitive

## Full repo verification

Before shipping broad changes, run the full repo test suite:

```bash
./.venv/bin/pytest
```

If you changed Python files in the collaboration area, also run:

```bash
./.venv/bin/ruff check \
  src/codex_autorunner/integrations/chat \
  src/codex_autorunner/integrations/telegram \
  src/codex_autorunner/integrations/discord \
  tests/integrations/chat \
  tests/integrations/discord \
  tests/test_telegram_bot_integration.py \
  tests/test_doctor_checks.py
```

## Expected outcome

- The focused collaboration suite passes without network access or live Telegram
  or Discord credentials.
- The parity check passes.
- No manual Telegram or Discord environment is required for confidence in the
  collaboration rollout.

## Orchestration verification

Run the orchestration verification suite to verify the integrated build works
across runtime threads, bindings, PMA shims, and flow targets:

```bash
./.venv/bin/pytest \
  tests/browser/test_orchestration.py \
  tests/test_pma_routes.py \
  tests/test_pma_cli.py \
  tests/integrations/discord/test_service_routing.py \
  tests/test_telegram_pma_routing.py \
  tests/routes/test_flows_route_characterization.py \
  tests/routes/test_flow_status_history_routes_extracted.py \
  -v
```

### What this covers

- **Runtime-backed thread targets**: Surface orchestration ingress routes messages
  to thread targets correctly for web, Discord, and Telegram surfaces.
- **Authoritative bindings**: Discord and Telegram bindings are resolved and used
  for routing without bypassing the orchestration layer.
- **PMA shims**: PMA routes use the orchestration service for thread status,
  tail, and listing operations.
- **Flow-target controls**: `ticket_flow` remains a distinct flow target and
  does not collapse into a normal chat thread.
- **Observability**: Thread and flow targets share discovery and observability
  surfaces without losing their distinct identities.
- **Capability filtering**: ZeroClaw and Hermes capability differences, plus
  capability-aware filtering, continue to work inside the integrated build.

### Acceptance criteria

1. The surface orchestration ingress correctly routes messages to thread targets
   when no paused flow is detected.
2. The surface orchestration ingress correctly routes messages to flow targets
   (e.g., `ticket_flow`) when a paused flow is detected.
3. Thread and flow targets remain operationally distinct - operations on one do
   not affect the other.
4. Discord message handling uses the orchestration ingress for routing.
5. Telegram message handling uses the orchestration ingress for routing.
6. PMA thread status, tail, and list routes use the orchestration service.
7. Flow routes use the orchestration service for status and resume operations.
8. No PMA-only bypasses or transport-local binding authority are used for
   routing decisions; ordinary chat routing must resolve through shared
   orchestration-managed thread targets.

### Hermes and ZeroClaw capability verification

Verify that richer adapter capabilities remain visible and are not flattened to
the weakest common interface:

```bash
./.venv/bin/pytest \
  tests/routes/test_agents_routes.py::test_list_agents_includes_hermes_when_available \
  tests/routes/test_agents_routes.py::test_models_endpoint_returns_capability_error_for_hermes \
  tests/test_telegram_hermes_parity.py \
  tests/test_pma_routes.py::test_pma_managed_thread_status_and_tail_use_orchestration_service \
  tests/test_pma_cli.py::test_pma_cli_thread_query_commands_use_orchestration_routes \
  -v
```

For Hermes specifically, this should confirm:

1. Discovery surfaces list Hermes when runtime preflight is ready.
2. Unsupported Hermes actions such as model listing fail with explicit
   capability errors.
3. Telegram and PMA routing preserve Hermes thread bindings instead of falling
   back to Codex/OpenCode assumptions.
