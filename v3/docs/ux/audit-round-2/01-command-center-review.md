# CAR v3 command-center implementation review — round 2

## Verdict

The rebuild is a large improvement. Inbox and Incidents now explain their different jobs, human titles lead each row, filters are visibly labeled, incident detail is decision-first, raw evidence is collapsed, and live 320px/375px checks show no page-level horizontal overflow. The UI now feels like an operator console rather than a database viewer.

It is not ready for operator testing yet because the unresolved-incident completion contract is still inconsistent. One case says an operator response is required but does not identify where to respond; another is labeled “Needs response” in the queue but has no response request or next step on detail.

## Evidence reviewed

| Step | Rebuilt evidence | Result |
| --- | --- | --- |
| R1 | `round-1-built/01-inbox.png` and live `/ui` | Major hierarchy, labeling, and density improvement |
| R2 | `round-1-built/02-incidents.png` and live `/ui/incidents` | Task-first queue is clear; action-state language is not yet reliable |
| R3 | `round-1-built/03-incident-detail.png` and live release-cutover incident | Decision-first hierarchy works; completion handoff remains generic |
| R4 | `round-1-built/07-inbox-mobile.png` plus live 320px/375px checks | Cards reflow correctly with no page overflow; nav overflow is undiscoverable |
| R5 | `round-1-built/08-incident-mobile.png` plus live 320px/375px checks | Detail reflows correctly; supplied full-page capture is visually corrupted |

The capture set is under:

`/Users/dazheng/.codex/visualizations/2026/08/27/01a043d3-ef1f-7a11-bb4a-ce6a9f2051a3/v3-ux-audit/round-1-built/`

## What landed well

- Inbox leads with event title, uses relative time, separates source/triage/severity, and gives incident-backed events a direct detail link [R1].
- The filter form has persistent labels and accessible names; the live DOM exposes `Filter inbox`, named selects/inputs, and an explicit `Apply filters` button [R1].
- Incidents defaults to **Needs attention**, leads with the human summary, and demotes grouping/provider metadata [R2].
- Incident detail now answers what happened and why in the first viewport. The incident ID and grouping key are secondary, while the operator question is visually prominent [R3].
- Events, provider decisions, escalations, actions, and outcomes are merged into one durable timeline. The audit table is collapsed under **Forensic evidence** [R3].
- The shell has a skip link, named navigation, `aria-current="page"`, coherent headings, scoped table headers, visible focus styling, and readable light/dark tokens.
- Live checks at both 320px and 375px showed `scrollWidth === innerWidth` on Inbox and incident detail. Filters, records, decision panel, and sections use the full available measure rather than compressed tables [R4, R5].

## P0 — Must fix before operator testing

### P0.1 — “Needs response” is not derived from an actual response request

The Incidents queue labels the open Telegram-delivery case **Needs response** [R2]. Its detail page has no pending escalation, no decision panel, no response channel, and no next step. It only explains that CAR will not silently resend. This is a dead end and makes the queue state untrustworthy.

The current rule appears to map every open incident to “Needs response.” Open lifecycle state is not evidence that a human response exists.

**Fix prescription**

- Derive a separate operator-work state from durable facts, not from `incident.state` alone:
  - `Response required`: an unanswered escalation exists.
  - `Follow-up required`: a named operator task with a completion criterion exists.
  - `Monitoring`: the case is open but no human action is currently requested.
  - `Resolved`, `Snoozed`, `Expired`: lifecycle terminal/deferred states.
- For the Telegram-delivery case, either:
  - label it **Monitoring** and state what CAR is waiting for plus what will happen next, or
  - create/expose a real escalation with a concrete question and response handoff before labeling it **Response required**.
- Use the same derived work state on the queue, page header, decision panel, timeline, and empty counts. Preserve `open`/`escalated` as secondary provenance when useful.

**Done when**

- Every incident labeled **Response required** has an unresolved question and an actionable response handoff on detail.
- An open incident with no pending human task is never labeled **Needs response**.
- Queue state and detail state cannot contradict each other for the same incident fixture.

### P0.2 — The read-only handoff still does not tell the operator where to act

The release-cutover detail correctly says the web view is deliberately read-only, but its instruction is: “respond from the escalation card in your configured operator channel” [R3]. It does not name the channel, identify the card, link to it, or explain what to do if delivery failed. The user still cannot complete the task from the information on screen.

This misses the accepted plan’s requirement to **name the configured operator-channel handoff**. “Configured operator channel” is system vocabulary, not a destination.

**Fix prescription**

- Resolve and display the actual durable delivery target and receipt state, for example:
  - **Respond in Telegram**
  - `Escalation delivered to David · 10:20 AM`
  - card/message identifier or deep link when the transport supports one.
- If a deep link is unavailable, show a copyable incident/escalation ID and a precise instruction for locating the card.
- If delivery is pending, failed, or uncertain, replace the normal handoff with that state and its safe recovery path; never imply that a card is available.
- If no operator channel can be resolved, show **Operator channel unavailable** with the exact configuration/remediation path rather than a generic instruction.

**Done when**

- A first-time operator can say exactly where to answer and how to identify the request without knowing CAR configuration.
- Delivered, pending, failed, uncertain, and unavailable channel states each have distinct copy.
- The handoff is backed by durable delivery/receipt data; the page does not invent success.

## P1 — Fix in the final polish wave

### P1.1 — Work-state language is inconsistent across the path

The default view is **Needs attention**; one row is **escalated**, another is **Needs response**; detail then uses **escalated**, **urgent**, **Operator response required**, **unanswered**, and **pending** [R2, R3]. These are legitimate facts from different axes, but the UI does not tell the operator which label is the actual work state.

**Fix prescription**

- Make the derived work state the consistent primary label: **Response required**, **Follow-up required**, or **Monitoring**.
- Keep severity (**Urgent**, **Attention**) and provenance/lifecycle (**Escalated**, **Open**) secondary and visually quieter.
- Rename the queue default to **Needs attention** only if it contains multiple work states; show each row’s work state consistently.
- Prefer sentence case and one phrase per meaning. Do not alternate “Needs response,” “Operator response required,” and “unanswered.”

### P1.2 — Several human-facing strings repeat or produce awkward grammar

Inbox repeats the session title after the event title, for example “v2 import rehearsal completed” appears as both the row title and the final metadata item [R1, R4]. The release detail subtitle reads “Opened 10h ago in Approve release candidate deployment?.,” creating a `?.` punctuation collision [R3, R5]. “Why CAR is asking” then repeats the incident title as its first paragraph.

**Fix prescription**

- Suppress session metadata when its normalized text equals the event/incident title.
- Format the subtitle as `Opened 10h ago · Session: Approve release candidate deployment?` so user-provided punctuation is not followed by a sentence period.
- In **Why CAR is asking**, omit the incident summary when it duplicates the page title. Lead with the provider rationale, then one concise evidence sentence.
- Add copy fixtures for question-mark session titles, identical title/session values, empty session names, and long names.

### P1.3 — Mobile navigation hides destinations without signaling horizontal scroll

At 375px, the primary navigation viewport is 283px wide while its content is 345px; at 320px it is 228px versus 345px. The nav is scrollable, but the scrollbar is hidden and no clipped next item or fade communicates that **Digests** is off-screen [R4, R5]. A user can reasonably conclude that the visible destinations are the complete set.

**Fix prescription**

- Keep the accepted compact top navigation, but add a subtle trailing-edge fade/overflow cue whenever more items exist.
- Ensure the active destination is automatically scrolled into view on navigation.
- Preserve 44px touch height and keyboard scrolling; do not reduce label size to force all five items onto one line.
- Verify the first and last links remain fully visible after horizontal scroll at 320px.

### P1.4 — The supplied full-page evidence is corrupted and cannot be used for final visual sign-off

The desktop and mobile incident captures show lower timeline/forensic content duplicated after a very large blank region [R3, R5]. Live DOM inspection found only one timeline and one audit disclosure. At desktop, the live page’s main content ends at roughly 1580px and the sections occupy contiguous normal-flow rectangles; at 320px/375px, the live page has no horizontal overflow and cards fill the available width. This points to a full-page capture/stitching or scale artifact, not duplicated implementation markup.

**Fix prescription**

- Do not change page layout to compensate for these captures.
- Fix the capture path or recapture incident detail as deterministic viewport-height slices with a known device scale, then stitch outside the browser only if needed.
- Add a capture assertion that the rendered screenshot height and landmark order match live `scrollHeight` and DOM order.
- Final sign-off requires clean desktop and mobile captures without duplicate regions or rescaling.

## P2 — Worth tightening after correctness

### P2.1 — Technical detail still consumes more queue space than its value

Incidents gives **Provider runs** a full desktop column even when the value is `0`, while the session/question can be nearly as long as the incident title [R2]. Inbox keeps triage badges such as `pending` visually strong even on informational “completed/passed” rows [R1].

**Fix prescription**

- Move provider-run count into the incident metadata line and suppress zero unless diagnostically meaningful.
- Render internal triage provenance as subdued text for non-actionable events; reserve amber/red pills for facts that change operator behavior.
- Keep the title, work state, severity, and age as the stable scan columns.

### P2.2 — Timeline evidence is slightly too expanded for the default operator view

The provider-decision entry shows rationale, model/token/cost metadata, a blue probe result block with raw JSON, and a second **Structured proposal** disclosure [R3, R5]. The important operator facts are present, but the always-open machine evidence interrupts the four-item narrative.

**Fix prescription**

- Keep the decision title, rationale, policy verdict, and a compact result such as `17/17 checks passed` visible.
- Move raw probe JSON, token/cost data, and structured proposal into one **Decision evidence** disclosure.
- Keep audit evidence separate and collapsed as it is now.

### P2.3 — Deep-linked detail lacks a local return affordance

The current Incidents nav state is clear, but a user arriving on a copied incident URL has no local breadcrumb or **Back to incidents** link [R3, R5]. Browser Back may return somewhere unrelated.

**Fix prescription**

- Add a small `Incidents /` breadcrumb or **Back to incidents** link above the detail title.
- Preserve the selected incident filter in the return URL when it is known; otherwise return to **Needs attention**.

## Final acceptance check

Before declaring this command-center path polished:

- Both seeded unresolved incidents must have truthful queue work states and matching detail next steps.
- The release escalation must name its actual response destination and delivery state.
- No visible copy repeats the same title as metadata or produces punctuation collisions.
- All five top-level destinations must be discoverable at 320px without page overflow.
- Clean desktop and mobile captures must show one continuous incident page with no duplicated regions.
- Retain the improvements already verified: named filters, skip link, `aria-current`, scoped tables, focus-visible styling, human-first list rows, decision-first detail, collapsed forensic evidence, and zero page-level overflow at 320px/375px.
