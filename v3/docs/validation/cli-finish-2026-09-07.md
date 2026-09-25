# CLI finish dogfood — 2026-09-07

Scope: bounded local CAR v3 CLI validation and adapter ergonomics. No production
deployment, customer contact, shared preview mutation, global configuration change,
install, commit, or push. The existing user preview on port 7194 was not used.

## Isolated native run

- Daemon: `http://127.0.0.1:7197`
- Config: `/tmp/car-finish-qa.abwxEJ/cli/config.toml`
- Per-agent connection/spool roots: `/tmp/car-finish-qa.abwxEJ/grok/` and
  `/tmp/car-finish-qa.abwxEJ/omp/`
- Cursor/Grok execution: `exec-exercise-unusual-water-crazy-solid-various`
- OMP/GLM execution: `exec-above-lecture-input-pitch-toast-forward`
- Post-fix Cursor/Grok acceptance: `exec-coral-vendor-usual-stomach-world-rule`
- Both executions reached terminal `completed`; `agentctl recent --label
  cli-finish --state nonterminal` returned zero rows. Tokens and connection-file
  contents were not printed.

Grok exercised help/schema/doctor, malformed and envelope-shaped files, invalid
flags and IDs, preparation context, and offline spooling. It created two synthetic
requests that remain isolated in `needs_you` for human/UI inspection:

- `req_2727af5be16ebeebefd012730296c9b625be` — context revision 2, `needs_you`.
- `req_0ae27ba91d388d8354d236c1d2d643d69b79` — unknown-flag probe, `needs_you`.

OMP exercised the same validation and round trip. Its synthetic requests were
cancelled during its own cleanup:

- `req_b78fd0e13dfefecd81bb505122717978baec`
- `req_d68ba548f23ad755348dd1517b8eb6af10d1`

### Native findings

1. Unknown flags were silently ignored and could create a live request. Both
   agents reproduced this before the fix.
2. Omitting `--file` fell through to stdin, producing an opaque EOF/JSON error in
   non-interactive runs. Envelope-shaped files exposed a raw validation dump.
3. An offline spool made while overriding `CAR_URL` belonged to a different
   origin audience. Restoring the original profile correctly did not replay it,
   but `flush` reported `remaining: 0` without explaining `other_audiences: 1`.
4. Preparation guidance named `packet.json` even when the source needs to write a
   replacement context file. OMP also observed that `wait`'s 0–3600-second bound
   was not documented in help.

## Bounded fixes

- CLI option parsing now rejects unknown/short/duplicate/missing values and stray
  positional arguments before network
  setup, supports `--flag=value`, and reports stable error codes.
- Packet reads require `--file PATH` or explicit `--file -`; missing files,
  malformed JSON, and request-envelope-shaped files produce actionable errors
  before a raise is sent. Raise key/input validation precedes credential setup.
- `flush` returns a `next_action` when pending work belongs to another
  origin/credential audience; it preserves audience isolation and does not add a
  cross-identity replay path.
- Preparation guidance uses `context.json`; shared guide text tells agents to use
  literal argv and check process exit status before parsing or retrying.

## Verification

- `bun test test/attention/cli-startup.test.ts test/attention/payload.test.ts`
  — 7 tests passed, 0 failed (42 assertions).
- `bun test` — **804 passed**, 0 failed across 59 files.
- `bun run test:portable` — **109 passed**, 0 failed (plus 164-file
  TypeScript transpile boundary check).
- `bun run check` — passed.
- `bun run check:foundation` — passed.
- Live isolated probes after the fix showed `unknown_option`,
  `missing_option_value`, `input_file_missing`, and a clear cross-audience
  `flush.next_action`; no new request was created by those invalid probes.

The two Grok `needs_you` rows were synthetic evidence only; the post-fix section
below records the bounded human answer, receipt, resolution, and cleanup.

## Post-fix acceptance

After the human answered the bounded preparation probe in the isolated UI, the
post-fix Grok run rechecked the adapter against the live daemon:

- Five invalid raises (unknown long flag, short flag, stray positional, missing
  `--file`, and missing input file) all exited 1 with `unknown_option`,
  `unexpected_argument`, `missing_option_value`, or `input_file_missing`.
  The pending spool/request count did not change and no new request was created.
- `req_2727af5be16ebeebefd012730296c9b625be` was fetched as `answered`, then
  received as `received` with `guidance.can_apply_answer: true`.
- Exact answer text was: “Accept the minimal packet for preparation. Record that
  this isolated probe completed enrichment; do not change production behavior or
  perform external work.”
- The durable local receipt was verified as
  `car.client-receipt.v1` / `historical_answer_snapshot`, with request ID
  `req_2727af5be16ebeebefd012730296c9b625be` and answer ID
  `reply_8d68952d32be0cdedf4b10a1d6a2b6ce1cb0`; no credentials or full snapshot
  were printed.
- The exact request was resolved with that answer ID and no external work was
  claimed. The leftover synthetic `req_0ae27ba91d388d8354d236c1d2d643d69b79`
  was cancelled at its current revision with a synthetic cleanup reason.

The coordinator independently verified the receipt metadata, terminal database
rows and Handled mailbox, then the isolated daemon on 7197 was stopped cleanly.
Artifacts remain available for inspection. The existing user preview on 7194
remains running with preserved data. The coordinator also added explicit timeout
`0..3600` documentation and input schemas that do not falsely require defaulted
packet fields; the startup assertions and final integrated checks pass.
