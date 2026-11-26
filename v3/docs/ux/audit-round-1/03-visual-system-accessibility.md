# CAR v3 visual system and accessibility audit — round 1

## Verdict

CAR v3 already has the right product posture for an offline operational tool: restrained, information-dense, fast, and free of decorative noise. Its semantic page skeleton is also better than the screenshots initially suggest. The current presentation is not yet polished, however. It reads as styled browser output rather than an intentional product system because typography falls back to Times, nearly all content sits in one undifferentiated plane, controls and navigation are undersized, interaction states are incomplete, and dense tables simply compress on narrow screens.

“Like Linear” should mean precision, rhythm, clarity, and calm density—not copying Linear’s brand, color, sidebar, or motion. CAR should feel more operational and slightly more conservative: crisp system type, shallow surfaces, strong state semantics, predictable keyboard behavior, and very little visual ornament.

## Audit scope and evidence

This is a combined visual-system, responsive UX, and accessibility-risk audit of the current server-rendered CAR v3 UI at `http://127.0.0.1:7193/ui` on 2026-08-30.

Evidence reviewed:

1. `01-inbox.png` — desktop Inbox, generally usable structure but very small controls and weak hierarchy.
2. `02-incidents.png` — desktop Incidents, healthy information order but visually underdeveloped empty space and tabs.
3. `03-incident-detail.png` — desktop incident detail, complete data but difficult to scan and compare.
4. `04-memory.png` — desktop Memory, useful grouping but action-heavy tables and form labels need work.
5. `05-policy.png` — desktop Policy empty state, technically clear but visually stranded.
6. `06-digests.png` — desktop Digest archive, readable content blocks but weak archive/navigation affordance.
7. `07-inbox-mobile.png` — narrow Inbox, unhealthy reflow: columns become narrow vertical text strips and targets remain desktop-sized.
8. `08-incident-mobile.png` — narrow incident detail, unhealthy reflow: dense tables fragment and long machine data dominates. The apparent second Audit trail at the bottom is not present in the live DOM and is treated as a full-page capture/stitching artifact, not a confirmed product defect.

The accepted screenshot set is stored at:

```text
/Users/dazheng/.codex/visualizations/2026/08/27/01a043d3-ef1f-7a11-bb4a-ce6a9f2051a3/v3-ux-audit/baseline/
```

The live DOM inspection confirmed `header`, `nav`, and `main` landmarks, one `h1` per page, native links/buttons/inputs/selects, `lang="en"`, a responsive viewport declaration, and system light/dark color selection. It also confirmed no responsive media queries, no explicit form labels on Inbox or Memory, no `aria-current` on navigation, no table captions or `scope` attributes, no skip link, and no authored focus-visible treatment.

## Strengths to preserve

- The UI is content-first. It does not hide operational truth behind charts or decorative cards.
- Information architecture is small and legible: Inbox, Incidents, Memory, Policy, and Digests.
- Pages use real headings, tables, forms, links, buttons, and landmarks rather than generic clickable containers.
- A single shared stylesheet already centralizes the important visual decisions, making a coherent system inexpensive to implement.
- Severity and autonomy are presented as text as well as color. This is the correct base for non-color state communication.
- Dark and light modes follow `prefers-color-scheme` without requiring client JavaScript.
- Monospace is used selectively for identifiers and machine-shaped content.
- The incident page exposes the full causal record—events, decisions, escalations, outcomes, and audit trail—which is the right trust model for CAR.

## Highest-impact findings

### P0 — mobile tables do not reflow

Screenshots 07 and 08 show desktop tables squeezed into a narrow viewport. Dates, labels, event titles, and audit data wrap one or two words per line. This makes the primary workflow slower and risks failure of WCAG 1.4.10 Reflow at 320 CSS px and 400% zoom.

The fix is not smaller type. Use a responsive data-view strategy:

- At `>= 900px`, keep dense semantic tables.
- From `640–899px`, put non-critical wide tables in a labeled horizontal scroll region, keep row headers/primary identity visible when practical, and show a subtle edge fade plus “Scroll for more” hint until the region is used.
- Below `640px`, render Inbox, Incidents, Memory rules/review, and Outcomes as stacked records. Each record should lead with title/summary, then severity and state, then a two-column definition list for time, source, session, and other metadata.
- Preserve the underlying data order and all actions. Do not make a card itself clickable when it also contains links or buttons.
- Keep Audit trail as a scrollable table at small sizes rather than turning arbitrary JSON into a card. Put long detail in a disclosure (`details`/`summary`) or a dedicated full-width block.

If two server-rendered representations are used, only one may be exposed at each breakpoint (`display: none` for the inactive version) so screen readers do not encounter duplicate content.

### P0 — type, controls, and navigation are undersized

The current 13px nav, 12px uppercase headers, 11px chips, 3–4px control padding, and browser-default Times body combine into a visually tiny interface. Desktop density is appropriate; the size and rhythm are not. Mobile targets are well below a reliable touch size.

Adopt a system UI stack and an explicit type scale:

- Body: `14px/20px`, weight 400.
- Dense table cell: `13px/18px`; never below 12px for essential text.
- Supporting metadata: `12px/16px`, with sufficient contrast.
- Page title: `20px/28px`, weight 650.
- Section title: `14px/20px`, weight 650.
- Label/table header: `11px/16px`, weight 650, `0.04em` tracking; use uppercase sparingly.
- Code: `12.5px/18px`, using the existing system monospace stack.

Controls should be at least 32px high on pointer-first desktop and 44px at narrow/touch layouts. Chips may remain 20–22px high because they are non-interactive; interactive pills must meet the control target rule.

### P0 — accessible names and state context are incomplete

Inbox selects and inputs depend on option text or placeholder text rather than persistent labels. The Memory note form has three placeholder-only inputs. Repeated buttons such as “archive” do not identify which rule they affect. Current navigation is indicated by class/color but not `aria-current="page"`. Tables lack captions and `scope="col"`.

Required semantic corrections:

- Give every form control a `<label>`, visible where space permits and visually hidden only for obvious compact filters.
- Use `type="search"` for Inbox search and a named fieldset/legend or form label such as “Filter inbox”.
- Give row actions contextual accessible names, for example `Archive rule: Keep release cutovers human-approved` while retaining the short visible label.
- Add `aria-current="page"` to the active primary navigation link.
- Add a skip link to `#main-content` and make the brand a home link with an unambiguous name.
- Add concise table captions (they may be visually hidden) and `scope="col"` to column headers. Use `scope="row"` where a cell is the row’s stable identity.
- Mark timestamps up with `<time datetime="…">` and keep a consistent human-readable display.
- Keep the incident heading concise: “Incident” as the `h1`, with the identifier in a copyable code element and state in a separate status badge.
- Announce post-action success with `role="status"` and failures with `role="alert"`; move focus to a confirmation/error summary when navigation alone does not make the result clear.

### P1 — hierarchy is too flat

Almost every region sits directly on the page background, divided by faint horizontal rules. The 1100px left-aligned main column leaves a large empty right side on 1440px desktop screenshots, while dense content remains compressed inside that column.

Use hierarchy without card sprawl:

- Full-width app header, then a centered content container with `max-width: 1280px` and responsive gutters.
- A page header row containing title, one-line context, and page-level actions.
- Tables sit on one raised surface with one boundary; do not wrap every row or section in an independent card.
- Incident detail uses a 12-column grid at wide sizes: eight columns for the event/decision record and four for status, identity, timing, and operator outcome. Collapse to one column below 960px.
- Memory and Inbox should use the full content width. Policy’s empty state should use a bounded explanatory panel rather than two lines stranded in the upper left.
- Section spacing should distinguish groups more strongly than row spacing does.

### P1 — operational actions lack a consistent hierarchy

All buttons currently share the same neutral treatment, and dangerous actions only become dangerous on hover. “approve”, “reject”, “archive”, “demote”, and “Add note” therefore look equally important and equally safe.

Define four button roles:

- Primary: the recommended page-level action, used at most once per local action group.
- Secondary: common safe actions.
- Ghost: low-emphasis row utilities.
- Destructive: irreversible or authority-reducing actions, visibly differentiated before hover.

Reject, archive, and demote should not rely on red alone; add precise action text, an icon only when the icon library already provides one, and confirmation when recovery is difficult. Approve/reject pairs must remain visually balanced enough not to bias an operator accidentally.

### P1 — interaction states are incomplete

The stylesheet defines link color, nav hover/active color, row hover fill, button hover border, and danger hover. It does not define focus-visible, pressed/current button states, disabled states, form focus, validation, or a consistent active row.

Every interactive component needs the state matrix:

- Rest, hover, active/pressed, focus-visible, disabled, and busy where applicable.
- A 2px focus ring with 2px offset that remains visible against base and raised surfaces in both color schemes.
- `:focus-visible` rather than removing browser focus; keyboard focus must never depend on hover.
- Inputs use border plus subtle ring on focus and border/icon/text for invalid state.
- Active navigation uses shape/weight plus color and `aria-current`, not color alone.
- Hover row highlighting is supplementary. Keyboard focus within a row should produce the same row-level cue via `:focus-within`.
- Motion should be limited to color, opacity, and small disclosure transitions of roughly 120–160ms, and removed under `prefers-reduced-motion: reduce`.

### P1 — state color semantics are fragmented

The current chips use severity classes, but pending/escalated/resolved/sent/unsent states are inconsistently plain text or links. Some warm hues have unclear meaning, and thin colored outlines carry too much of the message.

Create one status vocabulary across all pages:

- Neutral: info, pending, inactive, archived.
- Blue: notice, in progress, rules resolved.
- Amber: attention, needs review, unsent.
- Red: urgent, error, escalated, rejected.
- Green: healthy, approved, sent, granted, complete.

Every badge includes readable text; optional icons must be redundant. Background tint, foreground, and border should all change together. Never encode “clickable” by making status text blue; links need an independent affordance such as underline on hover/focus and an accessible destination name.

### P2 — copy and machine data need presentation rules

Snake-case values such as `llm_resolved`, raw event types, long IDs, repository scopes, paths, and JSON are necessary but should not dominate the main scan path.

- Present human labels (“LLM resolved”) and retain the raw value in accessible supporting text, a tooltip only as a supplement, or a copy affordance.
- Truncate stable identifiers visually in list views, but expose the full value through copy and accessible name.
- Wrap prose normally; use monospace only for code, IDs, paths, and payloads.
- Put raw JSON/detail under a disclosure with an accurate summary; do not make the audit record itself disappear.
- Add helpful empty-state copy with the observed path, current fallback behavior, and next safe diagnostic step. Do not imply an action the UI cannot perform.

## Restrained design direction

### Visual character

Aim for “quiet control room”: dark mode that is neutral rather than blue-black, low-chroma status tints, crisp one-pixel boundaries, almost no shadows, and a compact but not miniature density. A user should be able to scan incidents for an hour without the interface feeling either toy-like or terminal-like.

Avoid gradients, glass, oversized radii, floating cards, decorative illustrations, animated charts, and excessive iconography. They would add visual novelty without improving operator certainty.

### App shell and navigation

Keep the simple top navigation; five destinations do not justify importing a complex sidebar. Refine it into a 48px app bar:

- CAR brand/home at the left.
- Primary nav next, with 36px minimum-height items and a quiet filled current state.
- A right-side utility area reserved for connection/runtime health and theme only when those features exist; do not add empty chrome.
- On viewports below 640px, use a 48px horizontally scrollable nav row with visible overflow affordance or an accessible `details` menu. Do not shrink all five links to fit.

### Layout grid

```css
--content-max: 80rem;        /* 1280px */
--gutter: 1rem;              /* 16px mobile */
--gutter-md: 1.5rem;         /* 24px tablet */
--gutter-lg: 2rem;           /* 32px desktop */
--grid-columns: 12;
--grid-gap: 1rem;
```

- `<640px`: single-column records, 16px gutter.
- `640–899px`: two-column filter groups where useful, 24px gutter, wide-table scroll regions.
- `900–1199px`: full tables, single-column detail, 24px gutter.
- `>=1200px`: 1280px centered content, 32px gutter, optional 8/4 detail split.

Breakpoints should follow where content fails, not device brand names. Test at 320, 375, 768, 1024, 1280, and 1440 CSS px.

## Proposed foundation tokens

The exact palette must be contrast-tested in rendered components. These tokens are a restrained starting point, not a compliance claim.

```css
:root {
  color-scheme: light dark;

  --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;

  --text-strong: #17191f;
  --text: #2f333b;
  --text-muted: #626975;
  --canvas: #f7f8fa;
  --surface: #ffffff;
  --surface-raised: #ffffff;
  --surface-hover: #f1f3f6;
  --border-subtle: #e4e7ec;
  --border-strong: #cbd0d8;
  --accent: #315acb;
  --focus: #315acb;

  --status-neutral-fg: #555d69;
  --status-neutral-bg: #eef0f3;
  --status-info-fg: #2854b8;
  --status-info-bg: #edf2ff;
  --status-warning-fg: #7a4b00;
  --status-warning-bg: #fff3d6;
  --status-danger-fg: #a32a2a;
  --status-danger-bg: #ffeded;
  --status-success-fg: #17663a;
  --status-success-bg: #e8f7ee;

  --space-0: 0;
  --space-1: 0.25rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-5: 1.25rem;
  --space-6: 1.5rem;
  --space-8: 2rem;
  --space-10: 2.5rem;
  --space-12: 3rem;

  --radius-sm: 0.25rem;
  --radius-md: 0.375rem;
  --radius-lg: 0.5rem;
  --shadow-popover: 0 12px 32px rgb(16 20 28 / 14%);
  --ring: 0 0 0 2px var(--focus);
}

@media (prefers-color-scheme: dark) {
  :root {
    --text-strong: #f1f3f5;
    --text: #d7dae0;
    --text-muted: #9da3ad;
    --canvas: #0e1013;
    --surface: #14171c;
    --surface-raised: #191c22;
    --surface-hover: #1e2229;
    --border-subtle: #292d35;
    --border-strong: #3a404b;
    --accent: #8da8ff;
    --focus: #a9bcff;

    --status-neutral-fg: #b3b8c1;
    --status-neutral-bg: #252931;
    --status-info-fg: #a9bcff;
    --status-info-bg: #202942;
    --status-warning-fg: #f2c464;
    --status-warning-bg: #372d1c;
    --status-danger-fg: #ff9b98;
    --status-danger-bg: #3c2427;
    --status-success-fg: #7ed9a5;
    --status-success-bg: #1d3428;
  }
}
```

Use borders for most separation. Reserve `--shadow-popover` for menus/dialogs only; normal tables and panels should not float.

## Reusable component inventory

Build these as server-rendered template partials/classes with consistent semantics and state modifiers:

1. **App shell** — skip link, brand/home, labeled primary navigation, `main` container.
2. **Page header** — eyebrow/context, `h1`, status, supporting text, action slot.
3. **Button** — primary, secondary, ghost, destructive; small/default sizes; complete state matrix.
4. **Field** — persistent label, hint/error slot, input/select/textarea, compact filter variant.
5. **Filter bar** — labeled form, responsive grid/wrap, submit and “Clear filters” affordance, active-filter summary.
6. **Status badge** — common state mapping, text plus optional redundant icon, non-interactive by default.
7. **Data table** — caption, scoped headers, row focus-within, numeric alignment, empty row, overflow wrapper.
8. **Responsive record list** — mobile representation of dense entities using `article`/heading plus `dl` metadata.
9. **Section header** — `h2`, count badge, description/action slot; no decorative underline as the only grouping cue.
10. **Empty state** — title, one-sentence explanation, path/context, safe next step.
11. **Code block/payload disclosure** — copy affordance, scroll/wrap rules, `details` for secondary raw data.
12. **Notice** — neutral/info/warning/danger/success message with `role` chosen by urgency.
13. **Action group** — related safe/destructive actions with contextual accessible names and confirmations.
14. **Pager** — previous/next links, current range, disabled semantics; ready before datasets grow.

Do not introduce a client-side component framework solely for this work. Template partials plus a small, well-structured CSS layer fit the offline SSR architecture.

## Screen-specific application

### Inbox

- Give search visual priority, place filters in a responsive filter bar, and add a clear/reset action only when filters are active.
- Make title the primary row cell; severity/state/source are secondary. The current timestamp-first order is optimized for storage, not scanning.
- On mobile, use record cards ordered title → severity/state → source/session → timestamp.
- Make the incident destination explicit: “View incident: Approve release candidate deployment?” rather than linking only the state word.

### Incidents list

- Replace the unstructured state-link row (`open`, `escalated`, etc.) with a labeled segmented filter or compact tab list with counts and current state.
- Keep summary as the dominant column; opened time, state, session, dedupe class, and run count are supporting data.
- Give the single-row state a real badge and the summary link a strong row affordance.

### Incident detail

- Separate incident identity, status, timing/session metadata, and summary into a page header/context panel.
- Keep the five-step causal sequence, but improve section anchors and spacing. Provide an in-page “On this incident” nav only if records become long enough to justify it.
- Use prose styles for decisions/escalation questions and disclosure styles for model metadata and raw payloads.
- Keep “Waiting for David” visually tied to pending operator outcome; do not bury it in a small table.

### Memory

- Distinguish durable rules, pending review, operator notes, and charter with section descriptions and counts.
- Keep approve/reject adjacent; keep archive/demote secondary and contextual.
- Label note fields and collapse optional vendor/repo scope under a clearly named “Scope” group on mobile.
- The charter read-only state should look intentionally read-only, not like a disabled editable textarea.

### Policy

- Use a compact empty-state/diagnostic panel with the attempted path, consequence (“escalate-only until one exists”), and safe next step.
- Use semantic warning treatment because this changes runtime behavior, but avoid an alarming red error if the fallback is safe and intentional.

### Digests

- Treat each digest as an archive entry with date, delivery status, and content summary.
- Make sent/unsent a status badge with exact delivery timestamp as metadata.
- Use `<article>` and a heading hierarchy (`h2` entries beneath the page `h1`); the current jump from `h1` to `h3` should be corrected.

## WCAG-relevant risk register

| Priority | Risk | Evidence | Relevant WCAG 2.2 criteria | Required verification |
| --- | --- | --- | --- | --- |
| P0 | Wide tables collapse into unreadable narrow columns | Screenshots 07–08; no responsive CSS in live stylesheet | 1.4.10 Reflow, 1.4.4 Resize Text | 320px viewport and 400% browser zoom without two-axis page scrolling |
| P0 | Compact targets are too small for reliable touch/pointer use | Screenshots 07–08; 3–4px button padding and 13px nav | 2.5.8 Target Size (Minimum) | Measure rendered hit areas; target at least 24×24 CSS px, with 44px product target on mobile |
| P0 | Inputs/selects lack persistent accessible labels | Live Inbox and Memory DOM | 3.3.2 Labels or Instructions, 4.1.2 Name Role Value | Accessibility-tree and screen-reader name checks |
| P1 | Keyboard focus treatment is not authored and may be inconsistent/low contrast | No `:focus-visible` rules | 2.4.7 Focus Visible, 2.4.11 Focus Not Obscured, 1.4.11 Non-text Contrast | Tab through every route in light/dark mode and at zoom |
| P1 | Current navigation uses class/color but not programmatic state | Live nav has `.active`, no `aria-current` | 1.3.1 Info and Relationships, 4.1.2 Name Role Value | Accessibility-tree check for current page |
| P1 | Tables lack captions and scoped headers | Live DOM across Inbox, Incidents, Incident, Memory | 1.3.1 Info and Relationships | Screen-reader table navigation |
| P1 | Status/background/border combinations are not yet contrast-tested | Current tokens and thin chip borders | 1.4.3 Contrast (Minimum), 1.4.11 Non-text Contrast, 1.4.1 Use of Color | Automated contrast test plus manual state review in both schemes |
| P1 | Post-action result/error announcements are unknown | POST forms on Memory; screenshots cannot show transitions | 3.3.1 Error Identification, 3.3.3 Error Suggestion, 4.1.3 Status Messages | Exercise safe fixture actions in an isolated test environment |
| P1 | Repeated row actions have ambiguous accessible names | Multiple “archive” buttons in Memory | 2.4.6 Headings and Labels, 4.1.2 Name Role Value | Accessibility-tree name uniqueness check |
| P2 | Heading level skips in Digests | `h1` followed by `h3` | 1.3.1 Info and Relationships, 2.4.6 Headings and Labels | DOM heading-outline check |
| P2 | Long identifiers and raw payloads may create horizontal overflow at zoom | Screenshots 03 and 08 | 1.4.10 Reflow | 200%/400% zoom and long-content fixtures |

## Measurable acceptance criteria

### Visual and responsive

- At 1440px, core list/table surfaces use the available content container without leaving more than the intentional centered outer gutters.
- At 320px, 375px, 768px, 1024px, 1280px, and 1440px, no page-level horizontal scroll appears. A labeled table scroll region is allowed where explicitly specified.
- At 400% zoom on a 1280px viewport, users can read and operate the page without two-dimensional page scrolling.
- Body text is at least 14px/20px; essential table text is at least 12px/16px; no essential UI text is 11px.
- Interactive controls render at least 32px high on desktop and 44px high in the mobile layout. Every target meets WCAG 2.2’s 24×24 CSS px minimum or documented spacing exception.
- Desktop content uses the 1280px maximum container and documented 16/24/32px responsive gutters.
- All spacing resolves to the shared spacing scale; no one-off values are introduced without a comment explaining the exception.
- Normal panels/tables use no shadow; menus/dialogs are the only elements permitted to use the popover shadow.

### Interaction

- Every link, button, input, select, textarea, disclosure, and nav item has visible rest, hover, focus-visible, active/current, and disabled behavior where applicable.
- Focus indicators are at least 2 CSS px, visually unobscured, and reach at least 3:1 contrast against adjacent colors in both themes.
- A keyboard-only pass can reach every interactive element in logical order, identify the current location, operate disclosures/forms, and return from any confirmation UI without a trap.
- Row hover styling has an equivalent `:focus-within` treatment.
- Destructive actions are visually distinct before hover and include clear recovery/confirmation behavior proportional to risk.
- `prefers-reduced-motion: reduce` removes nonessential transitions; no essential state change depends on motion.

### Semantics and accessibility

- Every route has a unique document title, one descriptive `h1`, valid heading order, `header`/labeled `nav`/`main`, and a working skip link.
- Active primary navigation exposes `aria-current="page"`.
- Every form control has an accessible name that does not rely on placeholder text; validation errors are associated with the relevant field.
- Every data table has an accessible name/caption and scoped column headers; stable row identities use row headers where appropriate.
- Repeated row-action names include their target entity in the accessible name.
- State is never communicated by color alone; badges expose meaningful text and meet contrast in light and dark themes.
- Normal text meets 4.5:1 contrast, large text 3:1, and meaningful non-text boundaries/focus states 3:1.
- Status updates use the correct live-region behavior and do not steal focus for routine success.
- Automated HTML/accessibility checks report zero serious/critical issues, followed by manual keyboard, zoom/reflow, light/dark, VoiceOver, and forced-colors checks.

### Regression coverage

- Screenshot tests cover every audited route at 1440×900 and at least one 375px-wide mobile viewport, with stable fixture data.
- DOM tests assert the current-nav state, form labels, heading outline, table caption/header scopes, and contextual row-action names.
- Responsive tests assert that the desktop table and mobile record representations are never simultaneously exposed.
- Theme tests cover default light, default dark, hover, focus, active/current, disabled, warning, danger, and success states.

## Recommended implementation order

1. **Foundation (P0):** system type, container/grid, spacing scale, control sizing, focus-visible, semantic labels/current-page/table headers.
2. **Responsive data views (P0):** Inbox and Incident first, then Incidents and Memory; verify 320px and 400% zoom before visual polish.
3. **Shared components (P1):** buttons, fields, filter bar, status badges, data tables, notices, empty states, disclosures.
4. **Page hierarchy (P1):** page headers, incident context split, Memory action grouping, Policy diagnostic empty state, Digest archive entries.
5. **Polish and proof (P1/P2):** status palette, copy normalization, keyboard/VoiceOver pass, light/dark contrast, screenshot regression suite.

## Evidence limits

This audit can identify visible and DOM-level risks; it cannot claim WCAG conformance. The supplied screenshots do not prove keyboard order, screen-reader announcements, browser zoom behavior, forced-colors rendering, post-action errors/success, reduced-motion behavior, or contrast after any redesign. The live preview was inspected read-only, so mutating Memory actions were not exercised. Those items remain explicit verification gates rather than assumed defects or strengths.
