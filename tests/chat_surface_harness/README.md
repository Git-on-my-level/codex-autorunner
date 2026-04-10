# Chat Surface Harness

Reusable integration fixtures for cross-surface PMA regressions.

Use this package when a bug needs to be exercised through the real Discord or
Telegram services instead of unit-level stubs.

Available helpers:

- `tests.chat_surface_harness.hermes`
  - Registers a real `HermesHarness` backed by `tests/fixtures/fake_acp_server.py`
  - Uses the same official `session/*` transport path that Hermes exercises in CAR
- `tests.chat_surface_harness.discord`
  - Builds a `DiscordBotService` with fake gateway/rest transports and PMA state
- `tests.chat_surface_harness.telegram`
  - Builds a `TelegramBotService` with a fake bot and PMA topic state

Recommended focused runs:

```bash
pytest tests/agents/hermes/test_hermes_supervisor_official_prompt_hang.py -q
pytest tests/integrations/chat/test_hermes_official_completion.py -q
```

The harness is intentionally in `tests/chat_surface_harness/` so future agents
can discover it without mining large support files.
