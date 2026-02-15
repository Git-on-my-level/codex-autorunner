# Stress Loop Result

- Date (UTC): 2026-02-15T15:34:33Z
- Repo: /Users/dazheng/car-workspace/worktrees/codex-autorunner--process-opencode-leak-remediation
- Iterations: 20
- Baseline `car doctor processes` opencode count: 32
- Baseline `car doctor processes` codex app-server count: 37
- Baseline `pgrep -fl opencode | wc -l`: 30

## Exact commands executed

```bash
for i in {1..20}; do
  ./car flow ticket_flow start --repo "/Users/dazheng/car-workspace/worktrees/codex-autorunner--process-opencode-leak-remediation" --force-new
  ./car flow ticket_flow stop --repo "/Users/dazheng/car-workspace/worktrees/codex-autorunner--process-opencode-leak-remediation" --run-id "<run_id_from_start_output>"
  ./car doctor processes --repo "/Users/dazheng/car-workspace/worktrees/codex-autorunner--process-opencode-leak-remediation"
  pgrep -fl opencode | wc -l
done
```

## Per-iteration counts

| Iteration | Run ID | doctor opencode | doctor codex app-server | os opencode (`pgrep -fl`) | stop status |
|---:|---|---:|---:|---:|---|
| 1 | `4c75bb2a-4b0f-4d37-bded-f25766d8cb4f` | 32 | 37 | 30 | ok |
| 2 | `f2ac5fa1-bb31-426a-9679-a747a33d8e37` | 33 | 37 | 31 | ok |
| 3 | `579eaa5c-8647-4092-b52a-9a43a25fb6d7` | 32 | 37 | 30 | ok |
| 4 | `6d47dda1-cce2-44f4-96f9-b2eb21aac855` | 32 | 37 | 30 | ok |
| 5 | `1d5b0d99-0128-42c3-90d9-959b99a42124` | 32 | 37 | 30 | ok |
| 6 | `5bd2a205-70eb-443f-9441-86197daabe5d` | 32 | 37 | 30 | ok |
| 7 | `63db33f2-b323-4e2c-9599-6bb77555b0d2` | 32 | 37 | 30 | ok |
| 8 | `47c98ead-83f7-4b6a-bacd-0f1b468d47fe` | 32 | 37 | 30 | ok |
| 9 | `fcdaa2b5-22c7-4b69-b4e4-5afc92ddf3bf` | 32 | 37 | 30 | ok |
| 10 | `c8e09162-9bd9-497c-9646-4a78d33bc63d` | 32 | 37 | 30 | ok |
| 11 | `cbe85ff0-0b9b-43c5-a8b2-421bd46ab853` | 32 | 37 | 30 | ok |
| 12 | `4459ceab-5732-47a3-8da5-4561d4232d82` | 32 | 37 | 30 | ok |
| 13 | `322c1825-199f-4c13-b525-d161da67e700` | 32 | 37 | 30 | ok |
| 14 | `b4a1c637-468b-49de-afef-072cf5b146f5` | 32 | 37 | 30 | ok |
| 15 | `72a28adc-e4e3-4250-b7e8-08513ec8a50c` | 32 | 37 | 30 | ok |
| 16 | `e4a3eeab-aa45-4245-a43b-23ab436d5fcf` | 32 | 37 | 30 | ok |
| 17 | `ddf6ebd0-f755-443c-adb3-97e9a2ab4281` | 32 | 37 | 30 | ok |
| 18 | `bedf6dc6-018a-4f6e-a2fb-333e7fd2c6b8` | 32 | 37 | 30 | ok |
| 19 | `a7edc901-b9c7-4a9b-8142-3395aaf17477` | 32 | 37 | 30 | ok |
| 20 | `0560f293-e987-474b-b936-5b419ceac834` | 32 | 37 | 30 | ok |

## Acceptance Check

- Process count monotonically increases: false
- Final doctor opencode returns to baseline (<= 32): true (final=32)
- Final OS opencode returns to baseline (<= 30): true (final=30)
- Final status: **PASS**
