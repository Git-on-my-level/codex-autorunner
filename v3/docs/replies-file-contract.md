# File reply-back fallback contract

Every reply-back path in CAR (`ActionBus.deliver` in `../src/ports.ts`) either reaches
an agent through a live adapter (`claude-hook-http`, `claude-resume`,
`codex-exec-resume`, `agentctl-run`, `multica-api`) or falls through to this file
contract — the universal fallback, generalized from v2's `tickets/replies.py` inbox
(DESIGN.md §8). It's also what's used for a `response_channel: {"kind": "file"}` (or
`null`) session from the start, and for genuinely-live interactive terminal sessions
where no vendor offers safe injection.

This is a documented, stable **contract**, not an implementation detail: anything —
a shell loop, a Claude Code hook, an editor plugin — can be "an agent that consumes
CAR replies" by following the layout below.

## Layout

```
~/.car/replies/
  <car_session_id>/
    reply-0001.md
    reply-0002.md
    ...
    reply_history/
      reply-0001.md   # moved here once consumed
```

- `<car_session_id>` is CAR's own session id (not the vendor's native id — see
  DESIGN.md §2 "Session identity"; look it up via `card status` or the web UI /
  incident detail page if you only know the vendor-native id).
- Each pending reply is a small markdown file, `reply-<seq>.md`, zero-padded
  4-digit sequence, monotonically increasing per session, starting at `0001`.
- File **content**: plain text (no frontmatter) —
  - a text reply's body verbatim, or
  - the literal string `APPROVED` or `DENIED` for a permission decision answered via
    this fallback, each followed by a trailing newline.
- `reply_history/` holds replies an agent has already consumed. It does not exist
  until the first reply is archived.

## How an agent polls

There's no push mechanism — an agent (or a hook, or a human) checks for new files
under its own session's directory and reads them in sequence order:

```bash
SESSION_DIR="$HOME/.car/replies/<car_session_id>"
for f in "$SESSION_DIR"/reply-*.md; do
  [ -e "$f" ] || continue   # glob didn't match anything
  echo "--- $(basename "$f") ---"
  cat "$f"
done
```

A shipped example is Claude Code's `UserPromptSubmit` hook snippet (DESIGN.md §8):
because a live interactive Claude Code session has no safe injection point, CAR
stages the reply as a file **and says so honestly in-thread** ("reply staged; paste it
yourself, or it's picked up automatically next time you submit a prompt"). The
`UserPromptSubmit` hook slurps any staged reply into context automatically so you
don't have to paste it by hand — see [`claude-code-hooks.md`](./claude-code-hooks.md)
for the surrounding hook wiring; the snippet itself is:

```bash
#!/usr/bin/env bash
# .claude/hooks/slurp-car-replies.sh — wire as a UserPromptSubmit command hook.
# Reads any pending CAR replies for this session and prints them as additional
# context, then archives them so they aren't re-injected on the next prompt.
set -euo pipefail
CAR_SESSION_ID="${CAR_SESSION_ID:-}"   # set this however your setup maps
                                        # Claude's session_id -> car_session_id
[ -n "$CAR_SESSION_ID" ] || exit 0
DIR="$HOME/.car/replies/$CAR_SESSION_ID"
[ -d "$DIR" ] || exit 0
shopt -s nullglob
files=("$DIR"/reply-*.md)
[ ${#files[@]} -gt 0 ] || exit 0
mkdir -p "$DIR/reply_history"
echo "## Reply from CAR (staged, now delivered)"
for f in "${files[@]}"; do
  cat "$f"
  mv "$f" "$DIR/reply_history/$(basename "$f")"
done
```

(This mirrors `additionalContext` in the hook response shape documented in
[`claude-code-hooks.md`](./claude-code-hooks.md) — emit the reply text as
`additionalContext` rather than stdout if you're wiring this as an `http` hook instead
of a shell command hook.)

## Archiving

Once an agent (or the snippet above) has consumed a reply, move it to
`reply_history/` under the same session directory, preserving its filename. This is
purely a courtesy for debugging/audit trails on the agent side — CAR itself doesn't
read `reply_history/` back; the durable record of what CAR sent is the `audit` table
(verb `reply.staged`) plus the `outbox` row, not the file's presence or absence.

## What CAR guarantees

- CAR creates `~/.car/replies/<car_session_id>/` on first write (`mkdir -p`
  semantics) — you don't need to pre-create it.
- Every write is audited (`audit` table, actor `adapter:file`, verb `reply.staged`,
  object `session`/`<car_session_id>`, detail includes the file path) — so "did CAR
  actually try to deliver this" is always answerable even if no agent ever reads the
  file.
- Sequence numbers are per-session and monotonic (derived from a count over prior
  `reply.staged` audit rows for that session), but a file being consumed does not
  reset or reuse numbers — don't assume gap-free-forever if you build tooling on top
  of the numeric suffix; treat it as "increasing," not "1..N with no gaps."
- CAR never deletes a reply file itself. If nothing ever reads
  `~/.car/replies/`, files just accumulate — clean up `reply_history/` yourself if you
  care about disk usage over a long-running session.
