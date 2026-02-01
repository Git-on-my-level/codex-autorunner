# Drift Prevention Checklist

Keep long-running repos from diverging between control surfaces (web, PMA, Telegram) and filesystem state.

- **FileBox first.** Upload and fetch files through the shared FileBox (`/api/filebox` or `/hub/filebox/{repo_id}`) instead of legacy PMA/Telegram paths. The backend opportunistically migrates legacy files, and `tests/test_filebox.py` guards against regressions.
- **Pending turns.** Client turn IDs are persisted per ticket/workspace; refresh pages resume streams and clear pending state on completion so thinking UI stays aligned with backend turns.
- **Checks.** Run `make check` (includes `pytest`) before opening PRs; the FileBox tests ensure inbox/outbox listings stay in sync across sources.
