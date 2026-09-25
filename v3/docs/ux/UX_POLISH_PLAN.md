# CAR v3 operator experience

Status: implemented and verified in the v3 preview

This document turns the first design audit into the working UI contract for the v3 rewrite. The target is a quiet, precise operator console: high information density without miniature type, decisive hierarchy without decorative chrome, and authority boundaries that remain obvious under pressure. “Linear-like” means consistent geometry, restrained color, fast scanning, keyboard clarity, and carefully worded states. It does not mean copying Linear’s brand.

Source audits:

- [Command center](audit-round-1/01-command-center.md)
- [Governance surfaces](audit-round-1/02-governance-surfaces.md)
- [Visual system and accessibility](audit-round-1/03-visual-system-accessibility.md)

## Product model shown by the UI

The web surface must preserve the v3 authority model in both labels and visual hierarchy:

1. The attention router owns durable event, incident, decision, grant, effect, delivery, and audit lifecycle.
2. Providers propose decisions and effects. Provider policy is advisory.
3. Provider learning is scoped context. Accepting context can influence later proposals but cannot authorize execution.
4. Core grants are authority. Core safety remains non-bypassable at effect claim and execution time.
5. Legacy `memory` and `policy.toml` data are compatibility projections. They are never presented as the v3 control plane.
6. The initial web console is read-only for incident decisions. Unresolved escalations must name the configured operator-channel handoff instead of presenting fake or unsafe web actions.

## Information architecture

The initial console keeps a compact top navigation because five destinations do not justify a permanent sidebar:

- **Inbox** is the recent attention stream across providers.
- **Incidents** is the grouped operational work queue and defaults to work that needs attention.
- **Legacy context** exposes the v2-compatible memory projection honestly until an authoritative v3 provider-learning read model exists.
- **Policy diagnostics** exposes legacy policy diagnostics without implying that provider-advice state is present.
- **Digests** is a readable archive with explicit delivery receipt state.

Future Grants, Safety, and Providers pages should be added only when their authoritative read models exist. The current UI must not invent those states from legacy files.

## Screen contracts

### Inbox

- Lead with the event title, then relative time, type, session, and source.
- Keep severity and triage as separate status families.
- Label every filter persistently and encode the state in the URL.
- Link attached events directly to the owning incident.
- On narrow screens, replace the table with stacked records; never compress every column.

### Incidents

- Default to **Needs attention** rather than implementation-shaped states.
- Lead with the human summary and route the whole row to detail.
- Keep grouping key and provider-run count secondary.
- The first incident-detail viewport must answer: what needs me, why, urgency, next step, and current state.
- Merge events, provider decisions, escalations, actions, and outcomes into a durable timeline. Put raw audit evidence in progressive disclosure.

### Legacy context

- Show review work before accepted legacy context when proposals are pending.
- Label the page and every mutation as compatibility-only; it does not feed the v3 router or providers.
- Use **Accept in legacy store**, **Dismiss proposal**, **Reduce legacy setting**, and **Archive context**.
- Place a persistent compatibility notice above the first mutation.
- Every repeated action needs an object-specific accessible name.
- Show confidence as a label plus score, evidence balance, provider/source, scope, and age.
- The add-context composer must state that it writes only to the legacy projection and cannot grant permission to execute.

### Policy advice

- Lead with the core effect-authority boundary, not `policy.toml`.
- Label `policy.toml` as a legacy compatibility projection and read-only diagnostic.
- Keep provider advice, core grants, and core safety visually and semantically separate.
- Missing or invalid compatibility data must state the safe consequence without claiming it controls v3 execution.

### Digests

- Render a readable document with a constrained measure, not a page-wide monospaced block.
- Distinguish delivered from no-receipt state with words and semantic color.
- Preserve raw markdown in a collapsed disclosure for audit and copy parity.
- Future structured digest fields should put “Needs you” first and link each item to its owning durable object.

## Visual system

- Reuse the established CAR palette and typography intent: warm neutral light surfaces, deep blue-black dark surfaces, teal accent, crisp one-pixel boundaries, and almost no shadow.
- Center the shell at a 1280px maximum with 32px desktop, 24px tablet, and 16px mobile gutters.
- Use 14px minimum body text and 12px minimum essential metadata. Monospace is reserved for IDs, paths, configuration, and raw evidence.
- Status families are consistent: neutral for inactive/archived, blue for informational/progress, amber for attention/pending, red for urgent/error/escalated, and green for approved/sent/complete.
- Controls use primary, secondary, ghost, and destructive roles with a shared focus-visible treatment.
- Color is never the sole carrier of state.

## Responsive and accessibility acceptance

- No page-level horizontal overflow at 320px or 375px.
- Tables that support scanning switch to stacked records below 640px. Forensic tables scroll inside a named container.
- Important mobile controls are at least 44 CSS pixels high.
- The document has a skip link, named navigation, one `h1`, a coherent heading outline, `aria-current`, visible field labels, table captions/scopes, and context-specific action names.
- Keyboard focus remains clearly visible in both color schemes and forced colors.
- The primary operator path remains usable at 200% zoom; all content remains reachable at 400% zoom.
- Verification includes desktop and mobile screenshots, keyboard traversal, semantic DOM inspection, targeted web tests, full TypeScript checking, and a fresh independent review.

## Delivery loop

1. Capture the baseline flows.
2. Run independent command-center, governance, and visual/accessibility audits.
3. Implement the shared shell and page contracts as one system.
4. Capture the rebuilt flows and run a fresh independent review against this document.
5. Fix remaining usability and visual defects, rerun tests, and leave a deterministic local preview ready for operator testing.
