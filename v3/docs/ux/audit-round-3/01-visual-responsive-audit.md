# Round 3 visual and responsive audit

## Audit scope

Combined UX and accessibility audit of the server-rendered v3 operator console after the first two polish passes. This round is grounded in fresh rendered captures, not source inspection alone.

Routes inspected:

1. Inbox
2. Incidents
3. Incident detail
4. Digests
5. Policy diagnostics
6. Legacy context

Viewports inspected:

- 1440 x 900 and 1440 x 1000 desktop
- 1024 x 768 compact desktop
- 768 x 900 tablet
- 390 x 844 mobile
- 320 x 800 narrow mobile

The before and after evidence is stored in the local Product Design capture directory under `v3-ux-audit/round-3-current` and `v3-ux-audit/round-3-after`.

## User goal and accessibility target

An operator should understand what needs attention, why it needs attention, and where to respond within a few seconds. Technical evidence must remain available without forcing identifiers, protocol names, or compatibility disclaimers into the primary reading path. The UI should reflow at 320 px without horizontal page overflow and retain keyboard-visible navigation and controls.

## Strengths

- The console already had a restrained, consistent dark visual system with clear status color semantics.
- Inbox and incident rows use large linked targets rather than tiny action affordances.
- Incident decisions distinguish durable Telegram handoff from the read-only web view.
- Diagnostic evidence is present and auditable instead of being silently discarded.
- Table-to-card responsive switching is structurally sound.

## UX risks found

### P0 — Mobile task entry was displaced by controls

At 390 px, the section selector and expanded five-field filter form occupied most of the first viewport. The operator could not see a complete signal row without scrolling. The selector label also repeated the current route instead of helping navigation.

Decision: reduce the mobile header to `CAR v3` plus `Menu`; group links into Work and System; collapse filters by default and reopen them when a filter is active.

### P0 — Internal vocabulary leaked into primary rows

Visible strings such as `Attention.Question`, `release-cutover`, host names, raw action classes, duplicate session titles, receipt IDs, and grouping keys competed with the actual task.

Decision: show human labels such as Question, Error, Agent, Related work, and State. Keep durable IDs, grouping keys, message receipts, policy files, and raw payloads in technical disclosures.

### P1 — Daily work and migration diagnostics had equal navigation weight

Inbox, Incidents, Legacy context, Policy diagnostics, and Digests appeared as equivalent destinations. This made the product feel like an implementation dashboard rather than an attention router.

Decision: keep Inbox, Incidents, and Digests in primary navigation. Move Safety & policy and Legacy compatibility under System. Rename both pages around the human purpose rather than their implementation nouns.

### P1 — Sparse pages did not use space to communicate state

The desktop Inbox and Incidents pages had large unused fields while important counts were small, isolated text in the header. Digests rendered one long column despite short, comparable summaries.

Decision: add compact summary metrics, integrate counts into state tabs, combine Inbox state badges into one column, and use a two-column digest grid on wide screens.

### P1 — Incident detail repeated the same decision in several forms

The header badge, decision panel, provider rationale, source event body, context panel, and timeline repeated overlapping content. The Telegram message ID and read-only explanation appeared in the main decision callout.

Decision: present one response state, one question, and one short handoff. Add a compact incident summary strip; show one provider rationale; move receipt IDs and incident identifiers into disclosures.

### P2 — Compatibility pages over-explained their non-authority

Legacy context and policy diagnostics repeated the same safety disclaimer in the header, alert, section descriptions, cards, empty states, and forms.

Decision: state the boundary once near the top, then use shorter section names and local labels. Preserve the full forensic detail behind disclosures.

## Accessibility risks found

- The mobile menu and filters needed smaller closed states while retaining 40–44 px interactive targets.
- Duplicate form IDs would have been introduced by rendering desktop and mobile filter controls together; the implementation uses distinct ID prefixes.
- Collapsible technical evidence must retain native `details`/`summary` semantics and keyboard operation.
- Status cannot rely on color alone; every chip retains visible text.

## Implemented recommendations

1. Reduced primary navigation to Work destinations and added a System menu.
2. Replaced the large mobile route selector with a compact menu.
3. Added mobile-only collapsible filters and preserved a full desktop filter bar.
4. Added Inbox and incident summary strips with directly useful counts.
5. Removed host names, dedupe classes, duplicate titles, and raw event namespaces from the main lists.
6. Consolidated Inbox state into one dense column.
7. Simplified the incident decision and rationale path.
8. Moved durable IDs, receipt IDs, grouping keys, policy paths, and raw data into disclosures.
9. Reframed policy as a three-stage authority chain.
10. Reframed legacy memory as a compatibility-only System page with shorter labels.
11. Switched Digests to a two-column wide layout and removed duplicate generated titles.

## Evidence limits and verification gaps

- The deterministic preview exercises representative event, incident, digest, and compatibility data; it does not prove every possible long-string or localization case.
- Screenshot review verifies rendered geometry and visual hierarchy, not assistive-technology announcements.
- Native Cursor and OMP dogfood is documented separately because the deterministic preview is not itself a live agent runtime.
- A future browser-level keyboard pass should explicitly test menu dismissal, focus order, and active-filter persistence.
