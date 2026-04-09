# Drift Prevention Checklist

Keep long-running repos from diverging between control surfaces (web, PMA, Telegram) and filesystem state.

- **FileBox first.** Upload and fetch files through the shared FileBox (`/api/filebox` or `/hub/filebox/{repo_id}`) only. `tests/test_filebox.py` guards the canonical `.codex-autorunner/filebox/` contract.
- **Pending turns.** Client turn IDs are persisted per ticket/workspace; refresh pages resume streams and clear pending state on completion so thinking UI stays aligned with backend turns.
- **Checks.** Run `make check` (includes `pytest`) before opening PRs; the FileBox tests ensure inbox/outbox behavior stays stable across surfaces.

## Managed-Thread Cutover Smoke

Run this focused suite to verify the canonical managed-thread path:

```bash
make test-managed-thread-cutover
```

This covers runtime-thread event contract, hub supervisor wiring, PMA
lifecycle, Telegram/Discord routing, orchestration ingress guardrails, and
unified error sanitization. Use this for quick regression checks after changes
to the shared managed-thread path.

## Cross-Surface Chat Contract Checks

Use this when you want explicit chat-platform contract/shape coverage beyond default `make check`.

```bash
make test-chat-platform-contract
```

Combined extended validation:

```bash
make check-extended
```
