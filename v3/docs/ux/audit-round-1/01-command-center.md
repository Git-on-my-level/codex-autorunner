# CAR v3 command-center UX audit — round 1

## Audit scope

This audit covers the operator path from the Inbox to the Incidents list and then into one escalated incident. The target is a calm, high-density command center with Linear-like clarity: operators should be able to find what needs them, understand why, and take a safe action without decoding the underlying event model.

The review is read-only. It uses the current-run captures below plus a live DOM inspection of `http://127.0.0.1:7193/ui` on 2026-08-30. No write interaction was exercised.

### Evidence map and step health

| Step | State | Evidence | Health |
| --- | --- | --- | --- |
| S1 | Inbox, desktop, 1440 × 900 | `baseline/01-inbox.png` | Needs structural work |
| S2 | Incidents, desktop, 1440 × 900 | `baseline/02-incidents.png` | Needs structural work |
| S3 | Escalated incident detail, desktop, 1440 × 1236 | `baseline/03-incident-detail.png` | Critical operator-flow gap |
| S4 | Inbox, narrow viewport | `baseline/07-inbox-mobile.png` | Poor reflow |
| S5 | Incident detail, narrow viewport | `baseline/08-incident-mobile.png` | Critical reflow and scanability issues |

The captures live under:

`/Users/dazheng/.codex/visualizations/2026/08/27/01a043d3-ef1f-7a11-bb4a-ce6a9f2051a3/v3-ux-audit/baseline/`

## User goal and accessibility target

The primary operator goal is: **“Show me what needs my attention, explain the situation and impact, and let me make the correct decision safely.”** A secondary goal is forensic: inspect the event, decision, outcome, and audit evidence after the fact.

The accessibility target for the first pass should be WCAG 2.2 AA behavior: semantic landmarks and headings, named controls, keyboard-complete operation, clearly visible focus, non-color status cues, 200% zoom resilience, and useful reflow at a 320 CSS-pixel viewport. Screenshots cannot establish conformance; the live DOM observations below identify specific risks to verify.

## What already works

- The shell is restrained and fast to parse. Navigation, page title, filters, and content appear in a predictable order [S1–S3].
- Native links, form controls, tables, headings, and landmarks are used. The live DOM exposes a banner, navigation, main region, one `h1`, section `h2`s, and real table semantics rather than div-based imitations.
- Severity is expressed with both text and color, so the state does not rely on color alone [S1, S3].
- The detail page retains the complete decision lineage—events, provider decision, escalation, outcome, and audit trail—which is good raw material for trust and diagnosis [S3].
- The UI is visually quiet. There are no decorative gradients, oversized heroes, or unnecessary animation competing with operational state [S1–S3].

The problem is not excess decoration. The problem is that the database shape is currently the visual hierarchy.

## Structural issues

### P0 — The interface does not close the operator loop

The Inbox’s most urgent row says “Approve release candidate deployment?” and links to an escalated incident only through the triage-state cell [S1]. The detail view then says the escalation is “unanswered” and the outcome is “Waiting for David,” but exposes no clear response control [S3]. The operator can inspect the need but cannot see how to complete it.

This is the defining command-center gap. The primary action must sit next to the request, not be inferred from audit data or completed on another unnamed surface.

Recommended first pass:

- Add an incident action bar immediately under the incident summary for unresolved escalations.
- Name actions by outcome, for example **Approve release**, **Reject**, and **Snooze**, not generic “Submit” or “Resolve.”
- Before a consequential approval, show a compact confirmation containing the exact action class/scope, the object affected, and the fact that the v2 archive remains human-owned. Keep the cancel path visually equal and keyboard reachable.
- If the web surface is intentionally read-only, say so in the action bar and provide the exact response channel: “Respond in Telegram” or “Open operator CLI,” with a deep link/copyable command where supported. Do not leave “unanswered” as the only instruction.
- After a response, replace the controls with an immutable receipt: decision, actor, timestamp, delivery state, and resulting incident state.

Acceptance criteria:

- An operator can identify the requested decision and the available response path within five seconds of opening S3.
- Every unresolved escalation has either an in-product action set or an explicit, actionable read-only handoff.
- A consequential action cannot be submitted without a summary of its scope and effect.
- Success, failure, duplicate/replayed response, and stale-incident states each have visible recovery copy; the UI never silently changes status.

### P0 — Inbox and Incidents do not communicate distinct jobs

Inbox is an event stream, while Incidents are grouped operational cases, but the UI does not explain that distinction. The same escalation appears as multiple event rows in Inbox and one incident row in Incidents [S1, S2]. The top navigation gives both labels equal weight and no counts, so the operator must learn the data model by trial.

The Inbox also mixes progress, errors, questions, and permissions in a single chronological table. That makes “Provider contract suite passed” visually compete with an urgent approval request [S1]. Triage state is positioned as another technical column rather than the main task grouping.

Recommended first pass:

- Define **Inbox** as the raw/recent attention stream and **Incidents** as the grouped work queue. Add one-sentence page subtitles until the distinction is familiar.
- Make **Needs attention** the default command-center view, grouping open and escalated incidents ahead of informational events.
- Add small, meaningful navigation counts only for actionable state, for example `Incidents 1`; do not badge zero or every tab.
- Keep progress/info events in Inbox, but default to an actionable scope or visually demote informational rows.
- Preserve a clear route from each event to its parent incident. Link the row/title or add a trailing chevron; do not require the operator to discover that only the triage-state word is clickable.

Acceptance criteria:

- A first-time operator can describe the difference between Inbox and Incidents from the UI copy alone.
- Urgent, response-required work appears before informational activity by default.
- Every event belonging to an incident has a consistent, obvious incident affordance.
- The active navigation item is expressed visually and with `aria-current="page"`.

### P0 — The incident detail follows storage chronology, not decision-making priority

S3 starts with a machine identifier and then presents five numbered database collections. The request that needs an answer is buried in section 3, below the raw events and provider decision. “Release candidate needs a final operator decision” is weaker and smaller than the incident ID, while the most important rationale is scattered among an event body, decision paragraph, JSON, escalation question, and outcome row.

Reframe the detail page as a narrative:

1. **Decision needed** — plain-language question, severity, age, current state, and action controls.
2. **Why CAR is asking** — a two-to-four sentence synthesis: what happened, what is known, what remains human-owned, and the consequence of each option.
3. **Evidence** — key checks and anomalies, with machine evidence available on demand.
4. **Timeline** — events, provider decisions, actions, and outcomes in one chronological stream.
5. **Audit log** — raw identifiers and JSON in a collapsed forensic panel.

The incident title should be the human summary. Render the ID as subdued, copyable metadata. Replace section numbering with semantic headings; numbers imply a workflow even though these are evidence categories.

Acceptance criteria:

- The first viewport of S3 answers: “What needs me?”, “Why?”, “How urgent?”, “What happens next?”, and “What can I do?”
- The primary title is human-readable; the incident ID is secondary and copyable.
- Provider rationale and key evidence are not duplicated across multiple equally prominent sections.
- Raw JSON and audit rows remain available without dominating the default reading path.

### P1 — List rows are data tables rather than task objects

On desktop, S1 devotes seven similarly weighted columns to timestamps, internal types, severity, source, session, triage state, and title. The actual task title sits at the far right [S1]. S2 similarly gives the incident summary the last column after opened time, state, session, dedupe class, and LLM runs [S2]. This is backwards for human scanning.

Use a task-first row:

- Leading status/severity marker, then title/summary as the strongest text.
- A single secondary line for session, source, relative age, and incident/event type.
- Response state and a restrained trailing affordance on the right.
- Internal fields such as dedupe class and LLM-run count in an expandable detail or secondary column, unless abnormal.
- Make the row a single, keyboard-focusable navigation target. Avoid nested competing links.

Use relative time (“4m ago”) in the scan path and expose the exact timestamp through a title/tooltip or detail view. Keep tabular numerals for aligned counters and machine time.

Acceptance criteria:

- Titles form a clean vertical scan line on both list pages.
- Metadata never has equal or greater contrast/weight than the task title.
- One click or Enter anywhere on a row opens its incident/detail.
- At least 20 typical rows fit on a 900px-high desktop without text clipping or sub-12px primary copy.

### P1 — Narrow layouts shrink tables instead of changing composition

On the narrow Inbox capture, all seven columns remain, forcing timestamps and titles into narrow vertical fragments [S4]. The row height becomes large while comprehension drops. On incident detail, the event table crushes the decision copy into a thin column and the audit table overflows far beyond the reading measure [S5]. The long full-page capture also contains a large empty interval before audit content, which should be investigated as a layout/stitching symptom rather than accepted as content spacing [S5].

Responsive design should change the information model, not merely permit wrapping:

- Below 760px, render Inbox and Incident rows as compact stacked list items: title, severity/state, then one metadata line.
- On detail, keep the decision summary and action bar full width. Convert event/outcome records to stacked items.
- Put the audit table in a horizontally scrollable, explicitly labeled forensic region, or render each record as key/value rows. Do not allow the page itself to scroll horizontally.
- Collapse the top navigation into a menu or horizontally scrollable tab strip with a visible current item. Preserve 44px touch targets.
- Keep code blocks scrollable internally and allow long identifiers to wrap or truncate with a copy action.

Acceptance criteria:

- At 320 CSS pixels and at 200% zoom, there is no page-level horizontal overflow.
- A title does not collapse below a practical reading width (target ≥160px).
- Primary actions remain visible without horizontal scrolling and have at least 44 × 44 CSS-pixel targets.
- All information available on desktop remains reachable on mobile; lower-priority metadata may be disclosed progressively.

### P1 — Filters are unlabeled and rely on placeholder/default text

The live DOM exposes three unnamed comboboxes followed by textboxes named only by placeholders (“repo” and “search title/body…”). Placeholder text is not a durable label and default option text such as “any state” does not give a robust accessible name. The filter action also creates a separate submit step for simple view changes without indicating active-filter count [S1, S4].

Recommended first pass:

- Give every field a visible label or an accessible name paired with a persistent group label.
- Rename “any vendor” to **Source**, “any severity” to **Severity**, and “any state” to **Triage state**, while preserving “All” inside the choice list.
- Use a search field with label **Search inbox** and a single clear icon/button that is keyboard and screen-reader named.
- Show active filters as removable chips and provide **Clear all** only when filters are active.
- Apply native select changes immediately if server cost is negligible; otherwise retain an explicit **Apply filters** button and announce the resulting count after navigation.

Acceptance criteria:

- Automated accessibility inspection reports a non-empty accessible name for every form control.
- Labels remain visible when fields contain values.
- Filter state is encoded in the URL, survives refresh/back, and is summarized near the results count.
- Keyboard users can reach, change, apply, and clear filters in a predictable order.

### P1 — Status semantics and operational state are too weak

“urgent,” “attention,” “escalated,” “pending,” and “llm_resolved” use similar small pills or plain linked text [S1–S3]. These terms describe different axes—severity, incident lifecycle, response state, and triage mechanism—but the interface presents them as interchangeable tags. “open + escalated” is also an implementation-shaped filter label, not an operator state [S2].

Define and consistently render separate status families:

- Severity: Urgent / Attention / Notice / Info.
- Work state: Needs response / Investigating / Snoozed / Resolved / Expired.
- System provenance: Rule-resolved / Provider-resolved / Escalated, shown as secondary metadata.
- Delivery/receipt state: Sending / Delivered / Failed / Confirmed, visible after a response.

Rename the default incidents filter to **Needs attention**. Use text, icon, and color together for high-impact states, but reserve strong color for what changes operator behavior.

Acceptance criteria:

- A status term has one meaning and one visual treatment across Inbox, Incidents, and detail.
- No lifecycle or response state is communicated by color alone.
- Status foreground/background pairs meet WCAG AA contrast; this requires measured verification in both color schemes.

## Polish issues

These should follow the structural pass rather than mask it.

- **Measure and alignment:** Desktop content is pinned to the left and leaves a large unused field [S1–S3]. Center a capped command-center measure, while allowing the list to use enough width for a readable title column. Align the header, filters, list, and detail content to the same grid.
- **Typography:** Increase the visual difference between human language and machine metadata. Use proportional type for summaries and rationale; reserve mono for IDs, classes, models, and JSON. Keep primary body at 13–14px minimum, with a clear 18–22px title scale.
- **Spacing rhythm:** Current section gaps are inconsistent: compact tables alternate with large vertical breaks on detail [S3]. Use a 4/8px spacing system, 32px between major sections, and 8–12px within a task row.
- **Surface treatment:** Add subtle bordered list rows/cards and a sunken table/list header only where it improves grouping. Avoid card-inside-card nesting. Use hover elevation or a trailing chevron to make navigation discoverable.
- **Copy:** Replace raw labels: `ts` → **Time**, `llm runs` → **Provider runs**, `dedupe class` → **Grouping key** (or hide it), `requires response` → **Response**, `title / body` → **Event**, `unanswered` → **Awaiting operator response**. Sentence-case all user-facing labels.
- **Current location:** Active navigation color is subtle [S1–S3]. Add a persistent selected treatment and semantic `aria-current` without making the entire shell louder.
- **Empty and loading states:** “No events match” and “No incidents in this view” should distinguish an empty system from filters with zero matches and offer the relevant recovery action.
- **Hover/focus parity:** The source styling has row hover but no equivalent row focus affordance. Every hover-revealed affordance must also appear on keyboard focus.
- **Density control:** A single comfortable default is enough for round one. If operators later need more, add Compact/Comfortable density as a preference, not ad hoc per-page spacing.

## Keyboard and assistive-technology risks

Confirmed from the live DOM:

- The Inbox selects lack accessible names.
- The repo and search inputs rely on placeholders rather than associated labels.
- Current navigation is represented by a CSS class, not `aria-current="page"`.
- Status chips are generic spans. This is acceptable for static text, but any future clickable pill must become a real button or link.
- Only the incident-state text and summary are links; table rows are not focusable navigation targets.

Likely risks requiring interaction testing:

- No explicit global `:focus-visible` treatment is present in the v3 inline stylesheet, so native focus may be inconsistent against the dark theme.
- Narrow tables may cause two-dimensional keyboard/zoom navigation and loss of row context.
- Updated results, incident state, and action receipts will need announced status messages (`role="status"`) if any updates occur without full navigation.
- Exact contrast, 400% zoom behavior, screen-reader table announcements, tab order, reduced-motion behavior, forced-colors behavior, and touch target sizes were not measured in this screenshot-led pass.

Verification should include keyboard-only completion of the primary response path; VoiceOver on macOS/iOS; axe or equivalent automated checks; both light and dark schemes; 320px reflow; 200% and 400% zoom; forced colors; and long/empty/error data fixtures.

## Coherent first-pass design

Keep the implementation restrained and server-renderable. The first pass does not need a new application shell or an animation system.

1. **Compact shell:** centered 1120px measure, clearer current navigation, actionable incident count, and consistent page header/subtitle.
2. **Task-first queues:** reusable list-row composition for Inbox and Incidents, with title first, one metadata line, status at the edge, whole-row navigation, and mobile stacking.
3. **Decision-first detail:** human summary title, request/action bar, explanation, key evidence, merged timeline, collapsed forensic audit.
4. **Semantic controls:** labeled filters, persistent URL state, clear focus treatment, status taxonomy, and accessible response confirmations/receipts.
5. **Responsive rules:** intentional list/card reflow, contained code/audit overflow, and touch-sized actions.
6. **Polish pass:** tokenized typography, spacing, borders, hover/focus, copy, empty states, and measured contrast.

The visual character should be quiet and neutral. Let unresolved work, not chrome, carry the strongest contrast. Linear-like polish here means predictable alignment, consistent primitives, quick keyboard travel, and careful state transitions—not simply smaller type or more gray.

## Compact proposed component inventory

| Component | Purpose |
| --- | --- |
| `AppShell` | Header/nav, centered measure, current-page state, actionable count |
| `PageHeader` | Title, one-line description, result/action summary |
| `FilterBar` | Named search/select controls, active-filter chips, clear/apply behavior |
| `CommandList` | Shared accessible list/table container with loading, empty, and error states |
| `CommandRow` | Task-first Inbox/Incident row; whole-row link; desktop/mobile layouts |
| `StatusBadge` | Typed severity, work-state, provenance, and delivery variants |
| `IncidentHero` | Human title, copyable ID, urgency/age, summary, session context |
| `DecisionPanel` | Question, rationale, explicit response path, safe confirmation, receipt |
| `EvidenceSummary` | Key checks/anomalies with progressive disclosure |
| `IncidentTimeline` | Unified chronological events, decisions, actions, and outcomes |
| `ForensicDisclosure` | Collapsed audit/JSON region with contained overflow and copy actions |
| `EmptyState` | Distinguishes empty system, zero filter results, loading, and failure |
