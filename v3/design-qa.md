# Decision inbox visual QA

## Target and evidence

- Source visual truth: `/var/folders/3v/4wd8xf9s0z175zhynpxrt4g80000gn/T/codex-clipboard-3dee6400-d3d8-4ed2-b9a3-5326bb019db5.png`.
- Implementation: the real Hono UI, using the isolated, in-memory preview on ports
  7195 (light) and 7196 (dark). The user's workspace remains on port 7194.
- Source: 2048 × 1536 perspective mockup. Implementation captures: 1440 × 1000,
  900 × 1000, 390 × 844, and 320 × 800 CSS/pixel dimensions, normalized at 1:1.
  This is an intentional adaptation of its inbox hierarchy, not a pixel clone of
  email content, a perspective window frame, people, or attachment artwork.
- Full-view comparisons presented the source and rendered capture together, first
  with `/tmp/car-inbox-design-qa/desktop-light-initial.png`, then with
  `/tmp/car-inbox-design-qa/desktop-light-final.png`.
- Additional evidence in that directory: `desktop-light-revised.png`,
  `desktop-dark-final.png`, `tablet-light-final.png`, `mobile-light-reading.png`,
  `mobile-dark-list.png`, and `narrow-light-final.png`.
- State: authenticated, populated inbox; explicit guided selection with exact
  response choices; native and guided rows; system dark and forced-preview light.
  At 1440 px the subject, list text, and choices were legible in the full capture,
  so an additional magnified crop was not necessary. Narrow captures separately
  checked source wrapping and toolbar controls.

## Comparison history

1. Initial pass: P2 redundant inspector links under the message list, literal
   “Reading pane” label, repetitive row statuses, and bulky choice layout.
   Removed the footer, added message position and previous/next links, retained
   only exceptional inbox status labels, and aligned choices/actions on wide
   screens. Revised captures show the changes.
2. Browser inspection identified P2 legacy form gaps caused by quirks mode.
   Added the document doctype at the HTML response boundary. Final captures and
   DOM inspection confirm `CSS1Compat` and contiguous choice rows.
3. Independent review found pagination/lookahead selection loss, stale-selection
   blank readers, and missing exact native answers. Added exact selection/recovery
   and native reply rendering, including approval/denial; covered by Hono tests.
4. Testing a native draft-discard confirmation stalled the in-app browser's older
   preview tab. Replaced native confirmations with an inline, keyboard-focusable
   keep-editing/discard safeguard. Unit regressions pass; final live interaction
   verification is blocked on dismissing/closing that older browser tab.

## Required fidelity surfaces

- Typography: restrained system sans, 13–14 px list subjects, 21–24 px reading
  subject, regular body text, and small secondary source/time metadata. No display
  font or oversized dashboard headings. Long subjects wrap; previews truncate.
- Spacing/layout: sidebar / list / reader on desktop; single list or reader on
  phones. Thin dividers, restrained radii, no stacked colored panels. No horizontal
  overflow at 320, 900, or 1440 px. Independent panes preserve reading position by
  pausing automatic reload after scrolling.
- Colors: neutral white/gray and charcoal equivalents; semantic color reserved
  for urgency and problems. Removed mint accents and colored recommendation cards.
- Assets: preserved the real CAR wordmark. Deliberately omitted people/attachment
  imagery because CAR sources are agents and projects, not email contacts; no fake
  avatars, decorative SVGs, or simulated app icons.
- Copy/content: source recommendation, impact, uncertainty, exact choice text and
  consequences remain visible. Connection/revision details are secondary. Recorded,
  received, unblocked, withdrawn, and missed outcomes remain distinct.

## Verification

- Foundation dependency check and TypeScript check passed.
- 786 Bun tests and 102 supplemental portable tests passed.
- Browser-rendered light/dark, desktop/tablet/phone captures inspected; no console
  errors in the fresh dark preview. Standards mode verified in the browser.
- Hono tests cover answer/receipt semantics, native reconciliation redirects,
  lookahead/off-page selection, canonical tabs, stale links, escaping, and auth.
- Inline-guard tests cover keeping drafts, explicit discard, refresh, modifier/hash
  exemptions, non-link clicks, and persistent dirty state after blur.
- Local port 7194 was restarted without replacing its database. Authenticated HTTP
  confirmed the new mailbox and the user's existing decision record are present.

## Remaining blocker

An older disposable preview tab is holding the superseded native confirmation.
The browser can capture and navigate fresh pages, but its input commands do not
complete reliably until that older tab is closed. Complete the final browser
reply/keep-editing/discard/menu checks and restore the user's browser sign-in after
it is dismissed. Do not mistake the passing source/HTTP tests for that live check.

final result: blocked
