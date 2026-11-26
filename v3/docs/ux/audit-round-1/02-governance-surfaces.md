# Round 1 UX audit: governance and learning surfaces

## Audit scope

This audit covers the CAR v3 **Memory**, **Policy**, and **Digest archive** pages as shown in the live read-only preview on 2026-08-30 and in the supplied baseline screenshots:

- `baseline/04-memory.png`
- `baseline/05-policy.png`
- `baseline/06-digests.png`

The target is a calm, precise, keyboard-friendly governance experience with the information density and finish associated with Linear. The user should be able to answer three questions without understanding CAR internals:

1. What has CAR learned, and what needs my review?
2. What can influence a decision, and what can actually authorize an effect?
3. What happened since I last checked, and what still needs me?

Accessibility target: WCAG 2.2 AA for contrast, focus, semantics, labels, target size, reflow, and state communication. Screenshots and DOM inspection support likely-risk findings only; keyboard, screen-reader, zoom, motion, and contrast measurements still require implementation testing.

## Non-negotiable product model

The current UI must not imply that provider-owned memory or policy advice can execute work.

- **Memory is context.** Provider memory can retrieve scoped context and consume human facts, instructions, decisions, and feedback. It is not authoritative router state and cannot grant execution.
- **Provider policy is advice.** A provider can evaluate a proposed effect, but its verdict is only one input.
- **Core grants are authority.** Human grants and their constraints are core-owned and auditable.
- **Core safety is enforcement.** Panic, dangerous-content rails, grant checks, rate limits, budgets, circuit breakers, dedupe, and effect-claim validation remain non-bypassable.
- **Legacy `memory` and `policy.toml` views are compatibility projections.** They are not consulted for v3 routing, provider selection, grant creation, safety authorization, or effect execution.

This distinction is the foundation of the redesign. Visual polish cannot compensate for an incorrect authority model.

## Overall verdict

The three pages expose useful raw state, use real HTML controls, and fail closed when data is absent. However, they currently read like internal database/admin pages rather than a trustworthy operator workspace. Memory has the richest content but the weakest consequences model. Policy presents a legacy file as though it were the active v3 control plane. Digests are legible raw markdown but do not behave like an attention summary.

| Step | Surface | Health | Main reason |
| --- | --- | --- | --- |
| 1 | Memory | At risk | “Approve,” “granted,” and “autonomy” blur learning context with execution authority; destructive actions are immediate and unexplained. |
| 2 | Policy | Critical | The page centers `policy.toml` and says its absence controls daemon behavior, while v3 policy advice and core safety are separate systems. |
| 3 | Digests | At risk | Useful content is trapped in raw markdown cards; delivery truth, urgency, provenance, and next actions are hard to scan. |

## Strengths worth preserving

- The global navigation is simple and consistent across all three pages.
- Memory separates active rules, pending review, notes, and charter rather than flattening unlike objects into one table.
- Counts beside section names make pending work discoverable.
- Confidence, evidence, scope, author, and status are exposed rather than hidden.
- The Policy empty state explicitly communicates a safe fallback instead of silently pretending configuration exists.
- Digest records retain rendered content and delivery timestamps, which is a sound basis for an auditable archive.
- Text labels accompany colored states, so meaning is not exclusively color-coded.
- The pages render without external assets, a useful operational constraint that a polished design can retain.

## Structural issues

### P0 — The information architecture misrepresents authority

**Evidence:** Memory uses columns named `autonomy` and values such as `granted`, then explains that promotion to granted happens in Telegram. Policy presents `/tmp/car-test/policy.toml` as the policy surface. The v3 architecture instead defines provider memory as context, provider policy as advice, and core grants plus safety as execution authority.

**Risk:** An operator can reasonably conclude that approving a memory proposal, promoting a rule, or editing `policy.toml` authorizes autonomous execution. That is a trust and safety failure even if the backend remains correct.

**Recommendation:** Replace the flat Memory/Policy model with a **Governance** group:

- **Learning** — provider-owned context proposals, accepted context, human notes, feedback, and provider exports/links.
- **Grants** — core-owned execution permissions, constraints, expiry, usage, revocation, and audit.
- **Policy advice** — selected provider, capability route, status, version, evaluation history, and advisory verdicts.
- **Safety** — authoritative core mode, panic/breaker state, budgets, rails, rate limits, and claim-time denials.
- **Digests** — summaries and delivery truth.

If the first implementation must retain current routes, add prominent, persistent authority labels and a migration banner. Do not silently relabel compatibility data as a v3 grant or safety state.

**Acceptance criteria:**

- No provider-memory control uses “grant,” “granted,” or “autonomy” without an adjacent statement that it does not authorize execution.
- No policy-advice page implies it can bypass or replace core grants or safety.
- Every effect-related detail identifies the four stages separately: proposal, provider advice, core grant, core safety/execution.
- Legacy projections are labeled “Compatibility projection” and state what does and does not consume them.
- Grants and Safety remain separate destinations and separate data models.

### P0 — Risky actions have no consequences model

**Evidence:** Memory offers one-click `approve`, `reject`, `demote`, and `archive` buttons inside dense rows. The labels do not say whether the action changes provider context, execution authority, or history. There is no preview, confirmation, undo, audit link, or success state visible in the baseline.

**Risk:** Similar-looking controls have materially different meanings, and “approve” sounds more authoritative than it is. Accidental archive/reject actions are easy, especially when tabbing through repeated unlabeled controls.

**Recommendation:**

- Rename provider-memory review actions to **Accept as context** and **Dismiss proposal**.
- Use **Reduce influence** rather than “demote,” with a before/after preview.
- Treat archive and dismiss as recoverable: use a confirmation popover for consequential changes and a toast with **Undo**.
- Show the exact affected provider, scope, future retrieval behavior, and non-effect on grants/safety before confirmation.
- Keep a row-level overflow menu for secondary actions; reserve the primary button for the next review decision.
- Link every completed mutation to its audit entry.

**Acceptance criteria:**

- Each mutation states the object, scope, consequence, and authority boundary before it is submitted.
- Destructive/reducing actions require confirmation or provide a reliable undo window.
- Repeated action controls have accessible names containing row context, for example “Archive rule: Escalate when semantic progress is stale.”
- A successful action is announced in an `aria-live` region, visibly updates the row, and provides an audit/undo path.
- Refreshing after submission cannot repeat the mutation.

### P0 — The Policy empty state is architecturally misleading

**Evidence:** The entire page contains a filesystem path and “No policy.toml found at this path. The daemon runs escalate-only until one exists.” It does not show selected policy provider, core safety state, active grants, or why execution is currently blocked.

**Risk:** A missing legacy file appears to be the cause of v3 behavior, and provider policy advice is visually conflated with the safety kernel.

**Recommendation:** Make the top of the page an authority summary rather than a file viewer:

- **Current effect mode:** Active, escalate-only, panic, budget exhausted, or degraded, derived from authoritative core state.
- **Why:** ordered list of blocking/limiting reasons with links to Safety, Grants, and provider health.
- **Policy advisor:** selected provider/profile/version/health and a clear “advisory only” label.
- **Safety kernel:** compact read-only summary with a dedicated Safety link.
- **Legacy compatibility file:** collapsed diagnostics section below the fold.

When no provider policy capability is configured, say “No policy advisor configured. Effects still require a core grant and must pass core safety.” When the compatibility file is missing, describe only the compatibility projection’s behavior unless it truly affects the current v3 composition.

**Acceptance criteria:**

- The first viewport answers “Can CAR execute an effect right now?” and “Why?” using authoritative state.
- Provider policy and core safety use different headings, status tokens, and explanatory copy.
- A missing or invalid compatibility file cannot be mistaken for the v3 policy advisor or safety kernel.
- Parse errors show the failing location, preserve the last known good/closed state, and offer a copyable remediation path.
- Status freshness and source are visible.

### P1 — Memory lacks provenance and explainability

**Evidence:** Rules show a content summary, numeric confidence, `+/-` evidence counts, author, and scope, but not where the learning came from, when it changed, what confirmed or contradicted it, which provider owns it, or where it was used.

**Risk:** Confidence appears mathematically precise without being interpretable. Reviewers cannot assess a proposal without leaving the page or guessing what evidence means.

**Recommendation:** Make every memory item open a detail drawer containing:

- human-readable statement and structured scope;
- owner provider, author, creation/update time, and source interaction links;
- confidence translated into a labeled level plus the underlying score;
- evidence timeline with confirming and overriding examples;
- retrieval/use history, outcomes, and last-used time;
- current influence on provider context;
- explicit “Cannot authorize execution” boundary;
- audit history and archival reason.

Use progressive disclosure: the list row should show statement, scope, provider, confidence level, evidence balance, status, and last changed; the drawer carries forensic detail.

**Acceptance criteria:**

- A reviewer can explain why a proposal exists and what accepting it changes without inspecting raw JSON or the database.
- Confidence is never displayed as a bare decimal alone.
- Evidence counts link to evidence details and distinguish human feedback from inferred outcomes.
- Provider ownership and last-updated time are visible in every row.
- An item’s last retrieval/use can be traced to an incident or decision when available.

### P1 — The review queue is buried and not designed as a queue

**Evidence:** Pending review appears below active rules in a uniform table. One item has equal visual weight to historical/active content. There is no age, priority, bulk triage, or “why now.”

**Risk:** Learning review is an attention task, but the page makes it secondary. Stale or risky proposals can remain unnoticed.

**Recommendation:** Make **Review inbox** the default Learning tab when work is pending. Each proposal card/row should include statement, scope, provider, reason proposed, source/evidence, age, confidence, and risk tag. Support a keyboard review flow: open, accept as context, dismiss, next. Add filters for provider, scope, age, and proposal type; avoid bulk acceptance of unrelated context.

**Acceptance criteria:**

- Pending count is visible in navigation and page title.
- Proposals are ordered by risk/recency with the sort rule visible.
- The reviewer can inspect evidence and complete the next proposal without pointer precision.
- Empty state explains how proposals arrive and confirms that no review is required.
- Loading, stale-cache, and failed-refresh states preserve existing rows and communicate freshness.

### P1 — “Notes” mixes authorship, scope, and instruction semantics

**Evidence:** A single inline form uses placeholder-only fields: `new note…`, `vendor (optional scope)`, and `repo (optional scope)`. The existing note sounds like an operational instruction, but the page does not state how or whether providers use it.

**Risk:** Users may believe a note is a command, a grant, a global preference, or inert annotation. Free-form scope strings are error-prone.

**Recommendation:** Use a labeled **Add context** composer with:

- type: fact, preference, instruction, or feedback;
- scope builder with Global / Provider / Repository / Session and validated selectors;
- selected provider destinations;
- expiry/review date when appropriate;
- a live “Who can see this / What this can do” summary;
- explicit copy: “This becomes provider context. It does not grant permission to execute.”

Keep advanced metadata behind disclosure. Show confirmation before saving global or long-lived instructions.

**Acceptance criteria:**

- All inputs have persistent labels and programmatic descriptions.
- Invalid or conflicting scopes are rejected inline with recovery guidance.
- The final review sentence restates type, scope, destination, retention, and authority boundary.
- Newly added context appears with author, timestamp, provider, scope, and audit link.

### P1 — Digests are rendered as documents, not attention summaries

**Evidence:** Each archive item is a date/status heading followed by a monospaced markdown block. “Unsent” is a neutral chip without explanation. High-value lines such as a pending migration decision have the same weight as “702 tests passed.” Delivery time lacks timezone, destination, and receipt state.

**Risk:** The user must read every line to find the one item that needs action. “Unsent” could mean not scheduled, queued, failed, suppressed, or uncertain, which are operationally different.

**Recommendation:** Render structured digest cards:

- header: period, generated time, destination, delivery state, receipt time, and freshness;
- **Needs you** section first, with count and deep links;
- **Changes since last digest** grouped by incidents, providers, grants/safety, learning, delivery, and spend;
- compact outcome metrics with semantic labels;
- collapsed **Raw markdown** for copy/debug parity;
- delivery timeline that distinguishes generated, queued, delivered, uncertain, rejected, failed, suppressed, and expired.

Archive controls should support status/date filters and full-text search. Keep a comfortable reading measure (roughly 65–80 characters) rather than stretching cards across the entire canvas.

**Acceptance criteria:**

- A user can identify all required decisions in under five seconds without reading raw markdown.
- Every actionable item links to the owning incident, grant, learning proposal, or migration decision.
- “Unsent” is replaced by a precise delivery state with explanation and next step.
- Times include timezone and relative age; destination and receipt identity are available.
- Raw markdown remains available but is not the primary reading experience.
- Empty state distinguishes “no digest generated yet” from filtered-no-results and load failure.

### P1 — Empty, error, loading, and stale states are incomplete

**Evidence:** Memory has terse “No active rules,” “Nothing pending,” and “No notes” messages. Charter displays “(no charter file found)” inside a large read-only textarea. Policy has one missing-file sentence. Digests says “No digests recorded yet.” No loading, stale, permission, partial-data, or retry state is visible.

**Risk:** Operational absence, misconfiguration, and network/read-model failure look too similar. The user cannot tell whether the system is safe, incomplete, or merely empty.

**Recommendation:** Standardize state panels with status icon, plain-language title, consequence, source/freshness, and one next step. For governance surfaces, always state the safe behavior. Examples:

- “No learning proposals — nothing needs review. Provider context is unchanged.”
- “Charter not configured — providers receive no charter text. Core grants and safety are unchanged.”
- “Digest generation has not run yet” versus “Could not load digest archive; showing data from 8 minutes ago.”

Do not render a missing charter as an editable-looking empty textarea.

**Acceptance criteria:**

- Empty, first-run, loading, stale, partial, permission-denied, validation-error, and request-failure states have distinct presentations.
- Every state says whether effects, grants, safety, or provider context are affected.
- Failed refresh preserves last known data and offers retry; it does not blank the page.
- Error details are available through progressive disclosure and are copyable.

## Visual and interaction polish

### P1 — Establish a real workspace shell

The current full-width top strip, tiny nav, narrow content pinned to the upper-left, and large unused canvas make the product feel unfinished. Adopt a stable app shell:

- 220–240 px sidebar with grouped navigation, pending badges, keyboard hints, and clear active state;
- 48–56 px page header with title, one-sentence purpose, status/freshness, and contextual actions;
- 720–960 px primary reading column, with an optional 360–420 px detail drawer;
- consistent 4/8 px spacing rhythm, 32 px rows, 36 px controls, and 40–44 px touch targets for destructive or primary actions;
- subtle 1 px separators and layered surfaces rather than many heavy table rules;
- sticky review/action header for long queues.

Linear-level polish comes primarily from consistent geometry, state transitions, and restrained hierarchy—not decoration.

### P1 — Replace undifferentiated tables with task-appropriate layouts

- Use a virtualized/data table for active context when comparison and sorting matter.
- Use review rows/cards for pending proposals because they require decisions and evidence.
- Use description lists and status cards for policy/safety summaries.
- Use readable document cards for digests.
- Keep dense metadata aligned, but give the statement or decision enough width to remain primary.

### P2 — Tighten typography, copy, and state tokens

- Increase the effective base text size and line height; current small gray text and uppercase 12 px headers are hard to scan in dark mode.
- Use sentence case everywhere: “Authored by,” “Evidence,” “Accept as context.”
- Replace implementation labels (`none`, `suggest`, raw decimal confidence) with user-facing labels and retain raw values in details/tooltips.
- Use a semantic token set: neutral, info, positive, warning, critical, disabled. Never rely on hue alone.
- Use monospace only for paths, IDs, raw configuration, and raw markdown—not primary digest content.
- Standardize tense and time: “Delivered Aug 29 at 12:30 PM EDT,” “Generated 4 minutes ago.”

Suggested copy changes:

| Current | Recommended |
| --- | --- |
| `Pending review` | `Learning review` |
| `approve` | `Accept as context` |
| `reject` | `Dismiss proposal` |
| `demote` | `Reduce influence` |
| `archive` | `Archive context…` |
| `autonomy` | `Provider influence` (compatibility only) |
| `evidence +/-` | `Evidence` with “8 confirm · 0 contradict” |
| `authored by` | `Source` or `Proposed by` |
| `unsent` | Exact receipt state, such as `Queued`, `Suppressed`, or `Delivery uncertain` |
| `No active rules.` | `No accepted learning context in this view.` |

## Accessibility risks

### Visible or DOM-supported risks

- Placeholder-only note fields have no persistent labels, so their purpose disappears after typing and may be unclear to assistive technology.
- Repeated controls such as “archive,” “approve,” and “reject” lack object-specific accessible names.
- No explicit `:focus-visible` treatment is defined; keyboard focus may be difficult to locate.
- Small muted text and thin borders in dark mode appear at risk of insufficient contrast; measure all text, border, and focus tokens against WCAG AA.
- Several controls appear below the recommended 44 × 44 CSS pixel target for important actions.
- Digest headings jump from `h1` to `h3`, weakening semantic outline.
- Rendered digest markdown is exposed as a generic preformatted block instead of semantic headings and lists.
- The Charter textarea has no associated label and looks editable despite being read-only.
- Active navigation is primarily a color change and needs a non-color indicator plus `aria-current="page"`.
- Action results and validation errors have no evident live-region announcement.
- Wide memory tables are likely to overflow or become unusable at 200% zoom and narrow widths.

### Required verification after implementation

- Complete every review and archive flow using keyboard only, including escape/return behavior in drawers and confirmations.
- Verify logical focus order, visible focus, focus restoration, and no focus loss after a row disappears.
- Test VoiceOver names, roles, states, table navigation, error association, and live announcements.
- Measure normal, muted, chip, border, focus, hover, disabled, and destructive-state contrast in light and dark themes.
- Test 200% and 400% zoom, 320 CSS px reflow, long repository names, long translated copy, and large text settings.
- Verify reduced-motion behavior for drawers, toasts, and row transitions.

## Recommended delivery order

1. **Authority truth (P0):** relabel compatibility projections; separate Learning, Policy advice, Grants, and Safety; correct Policy empty-state claims.
2. **Safe governance (P0):** consequence previews, recoverable destructive actions, audit links, object-specific accessible names.
3. **Task structure (P1):** learning review inbox, authoritative policy status summary, structured digest cards and receipt states.
4. **Resilience (P1):** complete empty/error/loading/stale states and provenance drawers.
5. **System polish (P1/P2):** workspace shell, component tokens, typography, density, responsive behavior, and motion.
6. **Accessibility verification:** keyboard, VoiceOver, contrast, zoom, reflow, and automated checks before handoff.

## Proposed page architecture

```text
CAR
├── Attention
│   ├── Inbox
│   └── Incidents
├── Governance
│   ├── Learning
│   │   ├── Review inbox [count]
│   │   ├── Accepted context
│   │   ├── Human context
│   │   └── History
│   ├── Grants                 (core authority)
│   ├── Policy advice          (provider input)
│   └── Safety                 (core enforcement)
├── Operations
│   ├── Providers
│   └── Digests
└── Global status / command menu
```

### Learning page

```text
Page header
  Title + purpose + provider/source freshness
  Non-authority notice
  Add context

Tabs: Review inbox | Accepted context | Human context | History

Filter/sort bar
Learning list or review queue
  Statement
  Scope + provider + source
  Confidence label + evidence balance + age
  Primary review action / overflow

Context detail drawer
  What it says
  Why it was proposed
  Evidence timeline
  Retrieval/use history
  Scope and provider ownership
  Authority boundary
  Audit history
```

### Policy advice page

```text
Authoritative effect-mode banner
  Can CAR execute effects now?
  Blocking/limiting reasons
  Last evaluated / freshness

Policy advisor card              Safety kernel card
  Provider/profile/version         Core mode and breaker
  Health and capability route      Budgets/rates/rails summary
  Advisory-only explanation        Link to Safety

Recent evaluations table
  Proposed effect → advice → grant → safety → outcome

Compatibility diagnostics (collapsed)
  Legacy policy.toml path, parse state, raw file
```

### Digest archive page

```text
Page header + search/filter
Digest status summary

Digest card
  Period + generation/delivery state + destination
  Needs you [count, deep links]
  Changes since last digest
  Outcomes / providers / learning / spend
  Delivery timeline
  Raw markdown (collapsed)
```

## Component inventory

| Component | Purpose | Key requirements |
| --- | --- | --- |
| `AppShell` | Stable navigation and content geometry | Grouped nav, badges, responsive collapse, skip link, keyboard command entry |
| `PageHeader` | Page purpose and current status | Title, description, freshness, status, one primary action |
| `AuthorityBoundaryNotice` | Prevent context/advice from reading as authority | Fixed vocabulary, links to Grants and Safety, cannot be dismissed permanently |
| `StatusBanner` | Answer “can it act?” | Authoritative source, reason list, timestamp, non-color state |
| `StatusChip` | Compact semantic state | Icon + text, AA contrast, consistent tokens, tooltip only for supplemental detail |
| `FreshnessIndicator` | Communicate live/stale/partial data | Timestamp, source, stale and refresh-failure variants |
| `LearningReviewRow` | Triage one proposal | Statement first, scope/provider/source, evidence, age/risk, primary action |
| `ContextTable` | Compare accepted context at scale | Sort/filter, sticky header, responsive columns, virtualization when needed |
| `ContextDetailDrawer` | Progressive forensic detail | Focus trap/restore, deep link, evidence/use/audit timelines, authority notice |
| `ScopeBuilder` | Create validated context scope | Named scope levels, searchable selectors, conflict validation, preview sentence |
| `ContextComposer` | Add a fact/preference/instruction/feedback | Persistent labels, destination, retention, review, consequence summary |
| `ConsequenceDialog` | Confirm consequential changes | Before/after, exact scope/provider, effect on context vs grants/safety |
| `UndoToast` | Recover from archive/dismiss/reduce | Live announcement, keyboard reachable, reliable undo window |
| `EmptyStatePanel` | Explain safe absence | State-specific title, consequence, source/freshness, one next step |
| `ErrorStatePanel` | Distinguish failure from empty | Preserve stale data, retry, copyable details, affected subsystem |
| `PolicyAdvisorCard` | Show provider policy capability | Provider/profile/version/health, advisory-only label, recent verdict link |
| `SafetySummaryCard` | Summarize core enforcement | Breaker/panic/budget/rails, authoritative label, link to full Safety page |
| `DecisionProvenanceRail` | Explain an effect outcome | Proposal → advice → grant → safety → execution, with status at each stage |
| `DigestCard` | Render a digest for scanning | Needs-you first, semantic sections, readable measure, raw markdown disclosure |
| `DeliveryTimeline` | Show digest receipt truth | Generated/queued/delivered/uncertain/rejected/failed/suppressed/expired |
| `AuditLink` | Make mutations explainable | Actor, timestamp, verb, object, immutable detail view |

## Evidence limits

- The preview was inspected read-only; risky mutations were not submitted.
- The supplied state shows a missing policy file and charter, but not valid-file, parse-error, network-error, permission-error, loading, stale-cache, or large-data states.
- Screenshots do not prove actual contrast ratios, keyboard behavior, focus order, screen-reader output, live announcements, motion behavior, zoom resilience, or touch target dimensions.
- The digest archive contains only two examples, so search, pagination, grouping, and high-volume behavior were not observable.
- The audit uses the checked-in v3 architecture to identify authority-model mismatches; implementation must bind the redesigned states to authoritative core/provider projections rather than recreating the distinction in page-local UI state.
