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
