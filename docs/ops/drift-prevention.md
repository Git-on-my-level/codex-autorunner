# Drift Prevention Checklist

Keep long-running repos from diverging between control surfaces (web, PMA, Telegram) and filesystem state.

- **Artifact delivery first.** Send files through `car artifacts send <file>` when the active target or one unique chat binding is available. FileBox outbox paths are compatibility ingress for the active hub/repo scope, not a global delivery contract.
- **FileBox compatibility.** Upload and fetch files through the shared FileBox (`/api/filebox` or `/hub/filebox/{repo_id}`) only. `tests/test_filebox.py` guards the `.codex-autorunner/filebox/` compatibility contract.
- **Pending turns.** Client turn IDs are persisted per ticket/workspace; refresh pages resume streams and clear pending state on completion so thinking UI stays aligned with backend turns.
- **Checks.** Run `make check` (includes `pytest`) before opening PRs; FileBox and artifact-delivery tests ensure inbox/outbox compatibility and journal behavior stay stable across surfaces.

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
