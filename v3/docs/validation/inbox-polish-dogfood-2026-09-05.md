# Inbox ergonomics and native dogfood follow-up

Scope: local synthetic queue rollout, following the [previous native dogfood](native-team-dogfood-2026-09-05.md). No production deployment, customer contact, traffic change, or external scheduling was authorized. The existing user preview database on port 7194 was not seeded or reset.

## Implemented

- Customize a suggested answer without submitting or overwriting an existing draft. The exact answer and conditions remain visible. Controls are hidden without JavaScript; native forms still work.
- Persist the letter/number shortcut preference without persisting reply drafts. Escape still works when letter/number shortcuts are disabled.
- Attribute send/withdraw/review confirmations to the completed request, including paginated inbox fallback. Clear notification query markers after rendering so reload does not repeat old confirmations.
- Put the actual human reply and delivery state first in Watching/Handled; original recommendation/context remains available in a disclosure.
- Keep option actions inside full-width rows, with larger mobile touch targets.
- Run the agent CLI from another working directory without importing the web JSX stack.
- Add scoped `card request doctor` (local spool probe plus authenticated capability read, no request creation or receipt).
- Label persisted receipts as historical snapshots, validate identity/content/timestamp, and retain legacy bare-view compatibility. Fresh server state remains authoritative.
- Preserve malformed spool evidence via atomic rename into quarantine instead of retrying indefinitely. Ordinary I/O failures remain pending; symlink targets are never read for quarantine.
- Expose the packet-only CLI file schema separately from the API envelope; share concise writing and authority-gap guidance through CLI schema and capabilities.

## Browser verification

In-app browser screenshots inspected at 1280-pixel desktop and 390 × 844 mobile widths in light and dark themes. Checked custom-answer prefill, draft preservation when another option is customized, shortcut preference after navigation, exact previous-item confirmation, confirmation removal after reload, and reply-first Watching. Mobile document width matched the viewport (390 pixels); no horizontal overflow observed.

Human replies in the native exercise were entered through the actual web UI, including number-key selection and Ctrl+Enter submission. Every reply advanced within Needs you; the last reply left the empty inbox, not Watching.

## Native exercise

Evidence directory: `/tmp/car-polish-dogfood.8CyueQ` (temporary, not a permanent artifact store). Server: isolated port 7197. Agents invoked via agentctl with bounded lifetimes and per-run full access: Cursor `cursor-grok-4.6-high` with sandbox disabled, OMP `glm-5.3` with approval mode yolo. No persistent agent security settings changed.

- Grok investigation: minimal packet → one context enrichment → grounded question → exact answer received → owned note written → resolved. Request `req_b9f606c061720ccb55ddd1a902b68a52ba91`.
- OMP compatibility: identical packet/key submitted twice returned the same request and revision. Custom no-removal-date condition preserved verbatim in the received answer and output note. Request `req_0416a538b7c5a7e4712cc6f9657211ba88c4`.
- Grok release: cancelled broad question `req_fa60e614eb7c870eafbe2b75382653c428e6`, replaced it with narrower rehearsal question `req_e2f8c5794e3eb3534de8c187d1d25fa4f46d`.
- OMP applicator: request `req_524fa8fddf3e67be1788125e3c11dd17749a` rejected a premature resolve with `receipt_required`, then received the answer, applied the three local files, verified them, and resolved. Primary independently parsed the resulting TOML: exactly queue-v2, 5% traffic, compression off, status-v1 retained. Direct inspection confirmed the changelog preserves the no-removal-date condition and the rehearsal note is explicitly unsent, undated, and independent of maintenance.

## New findings

1. OMP initially supplied an API request envelope to `raise --file`, which accepts a packet body. Fixed discoverability by publishing both schemas and explicitly naming the CLI file shape. Existing API compatibility is unchanged.
2. Grok used an internal option ID as recommendation prose. Added shared writing guidance; the UI deliberately does not silently rewrite agent evidence or decision content. Agent-generated verbosity remains a quality limitation, not something the router can safely infer away.
3. Grok release constructed a multiword executable string in zsh and ignored nonzero exits in its wait loop. That produced repeated shell errors instead of polling CAR. CAR retained the answer as unreceived and did not falsely resolve the request. A bounded Grok recovery reused the original request, made four successful CLI calls, wrote the exact unsent-note conditions, and resolved only after the note existed. The original execution reached its deadline without stored final text; its transport-level `completed` status was not treated as task success.
4. The initial OMP applicator attempt reached its deadline without creating a request while prerequisites were delayed. A continuation reused the same client identity and waited for the actual recorded notes; there was no fabricated permission-to-wait decision.

## Final verification

- TypeScript check and foundation dependency boundary check passed.
- Bun: **796 tests passed**, 0 failed.
- Portable Node suite: **109 tests passed**, 0 failed.
- Daemon smoke: **17/17 checks passed**.
- Final canonical SQLite states: **4 resolved, 1 cancelled**, no outstanding dogfood requests. Both native execution label groups had zero nonterminal runs at cleanup.
- Original user preview on `http://127.0.0.1:7194/ui` restarted with the same config/database and verified HTTP 200. Before/after counts matched: 5 answered, 2 needs-you, 1 resolved. No user preview decisions were answered or reset during this exercise.
- Independent review findings were fixed, including paginated confirmation attribution, Escape preference semantics, malformed receipt timestamps, and non-destructive quarantine. A null-JSON spool regression was also added during final review.

Remaining quality issue: agent-authored copy can still be verbose or repeat internal
scope language despite better guidance. The OMP apply title was overly long. A
future authoring-quality pass could provide explicit concise-title feedback before
publication; it should not silently rewrite already published decision content or
hide exact effects and uncertainty.

Native execution evidence:

- Investigation: `exec-battle-orbit-light-random-stable-warfare`.
- Compatibility: `exec-yard-merit-arena-object-mouse-split`.
- Release original/recovery: `exec-guitar-hammer-seek-pill-prize-clown`, `exec-leg-alcohol-moon-meadow-spell-abandon`.
- Apply original/continuation: `exec-month-circle-heavy-dose-abandon-twin`, `exec-frame-farm-such-useless-crack-celery`.
