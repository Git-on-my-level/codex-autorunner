# CAR v3 visual system and accessibility review — round 2

## Verdict

The rebuild is a substantial structural improvement over the round-one baseline. The information architecture now uses product language, desktop geometry is intentional, mobile list views reflow into records, incident detail has a strong operator-first hierarchy, governance boundaries are explicit, and the semantic foundation includes labels, captions, scoped headers, `aria-current`, a skip link, contextual action names, and visible focus.

It is not ready for a final polish sign-off yet. One rendering defect is suppressing several core design rules in the live UI: quote characters inside the server-rendered `<style>` are emitted as `&quot;`. This invalidates both font-family tokens and quoted selectors/content declarations. The result is Times throughout the product, missing current-navigation styling, and missing expanded row-link hit areas. Light-mode primary buttons also fail text contrast. These are P0 because they affect every route or a core accessibility requirement.

The apparent half-width pages and repeated lower sections in the supplied screenshots are a separate capture-pipeline defect, not a live layout defect. They must be fixed before the next visual comparison so the team does not tune correct geometry against corrupted evidence.

## Scope and evidence

Reviewed on 2026-08-30:

- Every route in the rebuilt read-only preview at `http://127.0.0.1:7194/ui`.
- Both incident-detail fixtures, including the release-decision incident represented in the supplied captures.
- All nine screenshots in `v3-ux-audit/round-1-built/`.
- The round-one baseline captures.
- `v3/docs/ux/UX_POLISH_PLAN.md` and the round-one visual/accessibility audit.
- Computed styles, semantic DOM, focus order, element geometry, scroll geometry, and live screenshots at 1440, 375, and 320 CSS pixels.
- Calculated light/dark contrast for the declared tokens.

### Route health

1. **Inbox desktop — much improved, minor fixes remain.** Search and filters are clear, title-first rows scan well, and the table uses the container correctly in the live page.
2. **Incidents desktop — much improved.** “Needs attention” and the human summary now lead. Current tab styling is weakened by the shared CSS escaping defect.
3. **Incident detail desktop — strong hierarchy.** The first viewport answers what needs attention and why. The supplied half-width/duplicate capture is not representative of the live page.
4. **Learning desktop — strong product model, minor polish fixes.** “Context, not authority” is explicit and action names are contextual.
5. **Policy diagnostics desktop — healthy.** Authority and legacy compatibility are clearly separated; the diagnostic empty state is useful.
6. **Digests desktop — healthy.** The archive is readable, receipt state is explicit, and raw evidence is progressively disclosed.
7. **Inbox at 375/320 — functionally healthy, navigation and target polish remain.** Records replace the desktop table and there is no page-level horizontal overflow.
8. **Incident detail at 375/320 — functionally healthy.** It is long but coherent; the forensic table scrolls inside its own region. The repeated content in the supplied capture is not in the DOM.
9. **Learning at 375/320 — mostly healthy.** Review and accepted-context cards reflow well. The Human context table remains too compressed and should also become a record.

## Confirmed improvements from baseline

- The live 1440 layout is centered at 1280px with 80px outer gutters. The baseline’s small left-bound column is gone.
- Page headers now establish eyebrow, title, explanation, and meta count consistently.
- Inbox and Incidents lead with human titles rather than timestamps or implementation states.
- Incident detail has an operator-response panel, a clear explanation/context split, a durable timeline, and collapsed forensic evidence.
- Learning replaces legacy “Memory” framing and accurately states that context cannot grant execution authority.
- Policy advice is explicitly advisory and `policy.toml` is presented as a compatibility diagnostic.
- Digests are rendered as readable articles with delivery state and raw-markdown disclosure.
- Inbox, Incidents, and accepted Learning context switch from tables to mobile records below 640px.
- All audited pages have one `h1`, coherent heading order, unique titles, `lang="en"`, labeled primary navigation, a skip link, and a responsive viewport declaration.
- Form fields have persistent labels; data tables have captions and `scope="col"`; active primary navigation and incident-state filters use `aria-current="page"`.
- Repeated Learning actions include the target context in their accessible name.
- A 2px focus-visible outline with 2px offset is present, and the observed keyboard order is logical.
- Status is always written as text, so color is not the only carrier of meaning.
- Reduced-motion and forced-colors hooks exist in source, although the quoted forced-colors selector is currently lost in rendered CSS.

## P0 — fix before another polish capture

### P0.1 — the inline stylesheet is HTML-escaping CSS quotes

**Evidence**

The source declares:

```css
--font-ui: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
--font-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
```

The live CSSOM instead begins the token as:

```css
--font-ui: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, &quot; --font-mono: &quot;
```

The live computed body font is `16px Times`; the intended `14px/1.5` system UI font does not resolve. The same escaping drops or corrupts rules containing quoted attribute selectors and `content:""`, including:

- `nav.primary a[aria-current="page"]` visual styling;
- the normal current-page underline pseudo-element;
- `.row-title::after`, which is intended to expand row click/focus geometry;
- the forced-colors current-page underline.

This is why the rebuilt screenshots still feel editorial/browser-default instead of precise and product-like despite the correct design tokens in source.

**Exact fix**

Render the trusted, static CSS string as raw style text instead of an escaped JSX text child. With Hono JSX, use the framework-supported raw HTML property on `<style>` (for example, `dangerouslySetInnerHTML={{ __html: CSS }}`) rather than `<style>{CSS}</style>`. The CSS is a source constant and must never contain runtime/user content.

Add a rendered-response regression test that parses the final HTML and asserts:

- the style text contains literal `"Segoe UI"`, `[aria-current="page"]`, and `content:""`;
- it contains no `&quot;` inside `<style>`;
- a browser-level computed-style test reports a sans-serif body and monospace code;
- the active nav pseudo-element has non-`none` content;
- the row-link pseudo-element exists.

Do not “fix” this by removing all quotes ad hoc; raw trusted style output fixes every affected selector/declaration and prevents recurrence.

### P0.2 — light-mode primary-button text contrast fails

The primary button uses `#07110f` text over light accent `#0f7666`, which calculates to approximately **3.47:1**. The label is 13px and therefore requires 4.5:1. Dark mode is not affected because its accent is much lighter.

**Exact fix**

Create an explicit on-accent token:

```css
:root { --on-accent: #fff; }
@media (prefers-color-scheme: dark) {
  :root { --on-accent: #07110f; }
}
.button.primary { color: var(--on-accent); }
```

White on the current light accent is approximately 5.52:1. Verify normal, hover, focus, disabled, and forced-colors states with rendered contrast tests.

### P0.3 — the supplied screenshot pipeline scales content to half size and mis-stitches long pages

This is an evidence defect, not an implementation defect.

**Proof**

- In every supplied 1440px capture, the app bar border and content stop near x=720 and the intended 22px title rasterizes near 11px.
- In the supplied 375px captures, full-width panels stop near x=180. The release and Learning captures repeat later content after a large blank gap.
- The live 1440 browser reports `innerWidth=1440`, `devicePixelRatio=1`, header width 1440, and main bounds x=80 through x=1360 (1280px). There is no transform or zoom.
- The live 375 browser reports `innerWidth=375`, `devicePixelRatio=1`, main width 375, and document scroll width 375.
- A clean browser-native 375px full-page capture fills all 375 pixels and contains one continuous copy of the release incident.
- The release incident’s live document height is 2257px with exactly one timeline, four timeline items, and one Audit log disclosure. The supplied PNG is 2256px tall but contains a half-scale stitched duplicate.
- Learning’s live height is 1951px with one each of Learning review, Accepted context, Human context, and Provider charter. The supplied PNG is 1950px tall but repeats its lower sections.

The evidence is consistent with a 0.5 raster scale/DPR normalization being applied to tiles while the output canvas and stitch offsets remain in CSS pixels.

**Exact fix**

- In the capture harness, set the viewport explicitly and use DPR/device scale factor 1 for audit fixtures.
- Capture with one browser-native `fullPage` operation when possible. If tiling is unavoidable, convert tile sizes and offsets through DPR exactly once before compositing.
- Do not resize the rendered bitmap while retaining the original CSS-pixel canvas dimensions.
- Save a metrics sidecar for each capture: `innerWidth`, `innerHeight`, `devicePixelRatio`, `scrollWidth`, `scrollHeight`, and final PNG dimensions.
- Fail capture when `pngWidth !== innerWidth * DPR` or when the full-page height does not match `scrollHeight * DPR` within rounding tolerance.
- Add a long-page fixture with unique top/middle/bottom markers and fail if a marker is duplicated or omitted.
- Re-capture all desktop and mobile routes after correcting the harness. Do not change the 1280px shell or mobile content widths based on the corrupted screenshots.

## P1 — required final-polish fixes

### P1.1 — mobile primary navigation hides destinations and current location

At 320px the nav viewport is about 228px while its content is about 345px. Horizontal overflow is contained, so there is no page-level overflow, and keyboard focus scrolls hidden links into view. Pointer/touch users receive no edge fade, scrollbar, menu affordance, or guarantee that the current item is initially visible. At 375px, Digests is fully hidden in the supplied mobile captures.

**Exact fix**

At `<=375px`, use a two-row app header: brand/status on the first row and a full-width 44px navigation row below. Give the nav a visible trailing-edge fade and keep its scrollbar visually available or add an explicit “Sections” disclosure. On page load, the current destination must be visible without user discovery; prefer the disclosure approach for a zero-client-JS surface, or use a tiny progressive script that calls `scrollIntoView({ block: "nearest", inline: "center" })` on `[aria-current="page"]`.

Acceptance: every destination is discoverable by touch, the current destination is visible at 320px without first tabbing, and the control remains usable at 200%/400% zoom.

### P1.2 — Human context still uses a compressed table on mobile

Accepted context correctly becomes records below 640px, but Human context remains a three-column table. At 320/375px repository scopes and content wrap into narrow columns, recreating a smaller version of the baseline table problem.

**Exact fix**

Mark the Human context table as the desktop representation and add a mobile `.record-list` representation. Each record should present:

1. context text;
2. added time and source;
3. provider/repository scope as wrapping metadata.

Only one representation may be exposed at a time. Add a DOM/responsive test matching the accepted-context pattern.

### P1.3 — compact row links do not currently meet the intended mobile target behavior

Because the escaped `content:""` is dropped, mobile Inbox/Incident title links are only 19–37px high. The visual record suggests a card-sized destination, but only the title text is interactive. Even after P0.1 restores the pseudo-element, the current `inset:-12px -900px` technique is hard to reason about and does not reliably describe the record boundary.

**Exact fix**

Use a dedicated stretch-link pattern scoped to a positioned row/record:

```css
.record, tbody tr { position: relative; }
.record-link::after, .table-row-link::after {
  content: "";
  position: absolute;
  inset: 0;
}
.record:focus-within,
tbody tr:focus-within td { background: var(--raised); }
```

Keep badges non-interactive and above the overlay only when needed. Do not use a ±900px magic inset. Verify click targets at the first and last pixel of a record, visible row focus, and no overlap with nested actions. The resulting mobile record destination should be at least 44px high.

### P1.4 — form/control boundaries do not meet non-text contrast

Declared strong borders are approximately 1.68:1 against the light surface and 1.70:1 against the dark surface. Inputs rely on these boundaries to show their shape, so this is below the 3:1 non-text contrast target. Panel dividers may remain subtle; interactive boundaries should not share the same token.

**Exact fix**

Add a dedicated `--control-border` token around `#8a8c85` in light mode and `#667084` in dark mode (approximately 3.4:1 and 3.7:1 against their current surfaces). Apply it to inputs, selects, textareas, secondary/destructive buttons, and disclosure controls. Keep `--border` for nonessential panel separation. Recheck disabled controls separately; do not communicate disabled state by opacity alone when the state must remain perceivable.

### P1.5 — essential status text is below the plan’s size floor

Badges are 11px while the accepted plan requires at least 12px for essential metadata. Because incident state, severity, delivery, and influence are operationally important, they should not use the smallest decorative-label size.

**Exact fix**

Set badge text to 12px/16px and retain the compact 22–24px total height. Recheck table row height after the system font fix. Light warning text on its soft background is approximately 4.50:1—technically at the threshold but too close for rounding and state variants—so darken light `--warning` slightly to provide at least 4.7:1 headroom.

### P1.6 — disclosure targets land just under the mobile product target

Live Audit log and Raw markdown summaries measure about 43px high at 375px. They pass WCAG’s 24px minimum but miss the project’s 44px mobile target.

**Exact fix**

Set `summary { min-height:44px; display:flex; align-items:center; }` inside the mobile media query. Verify the native disclosure marker, focus outline, and open-state divider remain visible in light/dark/forced-colors modes.

## P2 — worthwhile finishing touches

### P2.1 — light and dark browser chrome use one dark theme color

The document always emits `<meta name="theme-color" content="#0a0c12">`, even in light mode.

Use two media-qualified theme-color tags matching `--bg`, one for light and one for dark. This prevents a dark browser/title-bar strip around an otherwise warm light UI.

### P2.2 — current state should use more than a hairline

Once P0.1 restores the intended rules, primary navigation uses text color plus a 2px underline. This is precise, but a slightly stronger weight or quiet surface tint would improve current-location scanning without copying Linear’s navigation. Keep `aria-current`, underline in forced colors, and no animated indicator.

For incident-state tabs, the active surface differs from its container by only about 1.1:1, but the text also changes from muted to strong and the state is programmatic. This is acceptable; do not solve it with a loud brand fill. A 1px strong border on the active tab is enough if user testing finds it too quiet.

### P2.3 — validate type metrics after the real font is restored

The intended weight values (520, 590, 610, 680, 720) will render differently across Inter, San Francisco, Segoe UI, and fallback sans fonts. After P0.1, check for clipped controls, changed nav width, filter wrapping, table density, and title wrapping on macOS and one non-Apple browser. Use optical judgment rather than further shrinking type.

### P2.4 — keep long pages, but add location help only when data grows

The release incident is genuinely about 2257px tall at 375px. Its order is coherent and each section appears once. Do not compress it merely to make a shorter screenshot. If production timelines become materially longer, add a compact in-page section index or sticky “Back to operator question” affordance; current fixture length does not yet justify extra chrome.

## Status-token review

The state mapping is coherent and should be retained:

- neutral: snoozed, expired, archived, skipped;
- blue: info, notice, progress/resolved states;
- amber: attention, pending, open, unanswered, queued;
- red: urgent, error, escalated, rejected, failed;
- green: approved, sent, granted, complete, resolved.

Calculated text/background ratios for current soft badges are generally healthy:

| Pair | Light | Dark |
| --- | ---: | ---: |
| positive | 5.05:1 | 8.10:1 |
| warning | 4.50:1 | 7.84:1 |
| critical | 4.83:1 | 6.55:1 |
| info | 4.98:1 | 6.92:1 |

Continue using words in every badge. Do not turn state badges into links merely by changing them to accent color; link affordance and state semantics must remain separate.

## Semantic and keyboard review

### Confirmed healthy

- Skip link is first in focus order and becomes visible with a 2px focus outline.
- Brand and primary navigation follow; form/action order then follows visual order.
- Learning action names include their target context.
- Native controls, forms, links, buttons, tables, `details`/`summary`, headings, articles, lists, and time elements are used appropriately.
- Mobile desktop-table representations are `display:none`, so screen readers do not encounter duplicate Inbox/Incident/accepted-context content.
- The forensic table remains in a named horizontal scroll region rather than being flattened into unreadable cards.
- No page-level horizontal overflow was measured at 320 or 375px on any route.

### Recheck after fixes

- Current navigation styling and forced-colors underline after raw CSS rendering.
- Full-row focus/target geometry after replacing the magic-inset stretch link.
- Light/dark button contrast and interactive-boundary contrast.
- Current destination visibility in mobile navigation.
- VoiceOver table navigation for the forensic log and desktop data tables.
- 200% and 400% zoom with the restored system font.
- Mutation success/error announcements in an isolated fixture; this review kept the preview read-only.

## Final acceptance gates

1. Computed body font is 14px system sans, code is system monospace, and no CSS token contains `&quot;`.
2. Active primary navigation has a visible, programmatic current state in normal and forced colors.
3. Light primary-button labels meet at least 4.5:1; input/control boundaries meet 3:1; normal and muted text continue to meet 4.5:1.
4. Every essential status badge uses at least 12px text and has contrast headroom in light/dark themes.
5. Inbox, Incidents, accepted context, and Human context all use records below 640px; forensic evidence scrolls only inside its named container.
6. No route has page-level horizontal overflow at 320 or 375px.
7. All important mobile controls and disclosures are at least 44px high; stretched record destinations match the visible record boundary.
8. The current primary destination is visible/discoverable at 320px without keyboard focus or an unexplained horizontal gesture.
9. Keyboard traversal preserves a visible focus indicator and logical order across every route.
10. Clean DPR1 captures at 1440×900, 375×1000, and 320×800 match live geometry, contain no duplicated section, and include capture metrics.
11. Browser-level semantic tests cover labels, captions/scopes, `aria-current`, contextual action names, heading order, and inactive responsive representation visibility.
12. Visual regression is rerun only after P0.1–P0.3; corrupted round-one-built screenshots are not used as geometry references.

## Evidence limits

This review inspected the current dark-mode live preview and calculated light-mode contrast from declared tokens; it did not visually force the browser into light or forced-colors mode. The preview was kept read-only, so confirmation, validation-error, and success-announcement behavior was not exercised. Screenshot evidence alone cannot establish WCAG conformance; the acceptance gates still require manual VoiceOver, keyboard, zoom/reflow, light/dark, and forced-colors verification after the fixes.
