# Drift Prevention Checklist

Keep long-running repos from diverging between control surfaces (web, PMA, Telegram) and filesystem state.

- **FileBox first.** Upload and fetch files through the shared FileBox (`/api/filebox` or `/hub/filebox/{repo_id}`) instead of legacy PMA/Telegram paths. The backend opportunistically migrates legacy files, and `tests/test_filebox.py` guards against regressions.
- **Pending turns.** Client turn IDs are persisted per ticket/workspace; refresh pages resume streams and clear pending state on completion so thinking UI stays aligned with backend turns.
- **Checks.** Run `make check` (includes `pytest`) before opening PRs; the FileBox tests ensure inbox/outbox listings stay in sync across sources.

## Hermes Readiness Validation

Use the Hermes readiness scorecard to enforce objective cross-surface behavior.

### Command

```bash
python scripts/hermes_readiness_scorecard.py --ci-smoke
```

Equivalent make target:

```bash
make hermes-readiness
```

Checkpoint baseline replay (pre tickets 916-919):

```bash
python scripts/hermes_readiness_scorecard.py --signals-json docs/ops/hermes-readiness-baseline-pre-916-919.json
```

### Categories and thresholds

- Web integration: `>= 7.0/10`
- Chat integration: `>= 8.0/10`
- CLI integration: `>= 8.0/10`
- Fallback robustness: `>= 7.0/10`
- Overall: `>= 8.0/10`

### Pass criteria

- The command exits `0` only when all category thresholds and overall threshold pass.
- Signals are **behavior-first**:
  - targeted pytest checks are the primary scoring weight in each category
  - static source-contract checks are lightweight anchors only
- CI-smoke behavior signals cover concrete flows:
  - web target mutation behavior
  - web active-target visibility behavior
  - destination set/show + channel-directory behavior
  - fallback partial-success reporting behavior
- `scripts/check.sh` runs this scorecard in CI-smoke mode so drift is caught during normal validation.
