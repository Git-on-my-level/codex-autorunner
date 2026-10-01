# Round 2 implementation review: governance surfaces

## Scope and evidence

This review covers the rebuilt **Learning**, **Policy advice**, and **Digest archive** surfaces at the live read-only preview on port 7194. It compares the implementation against:

- [the round-one governance audit](../audit-round-1/02-governance-surfaces.md);
- [the accepted UX polish plan](../UX_POLISH_PLAN.md);
- [`v3/DESIGN.md`](../../../DESIGN.md);
- [ADR 0001](../../../docs/architecture/0001-attention-router-capability-providers.md);
- the supplied desktop and mobile captures in `v3-ux-audit/round-1-built/`.

The live pages were inspected at the default desktop viewport, 375 px, and 320 px. DOM semantics, computed layout width, action names, target heights, active navigation, and focus styling were inspected read-only. No mutation was submitted.

## Verdict

The rebuild is a large visual and semantic improvement, but it is not ready for final polish yet. The shell, hierarchy, field labels, object-specific action names, mobile record layout, readable digests, and explicit “context, not authority” language are all substantially better. Two truth problems remain release-blocking:

1. Learning now claims the legacy `memories` projection is retrieved by and influences configured providers, while the current daemon explicitly keeps it outside the v3 router/provider path.
2. Digest rows with no `sent_at` are all labeled **Not delivered**, even though the canonical outbox may be pending, sending, failed, deferred, suppressed, or **uncertain**.

| Step | Surface | Health | Round-two result |
| --- | --- | --- | --- |
| 1 | Learning | At risk | Authority copy improved, but the displayed source and write path are still a legacy compatibility projection presented as current provider context. |
| 2 | Policy advice | Mostly sound | Legacy policy is clearly non-authoritative; generic advisor copy still lacks an authoritative provider read model and slightly conflates authorization, safety, and execution. |
| 3 | Digests | At risk | Readability is much better, but delivery truth is collapsed and “Needs you” items have no durable links. |

## What is now working

- The top-level copy states that provider learning cannot authorize an effect.
- “Accept as context,” “Dismiss proposal,” “Reduce influence,” and “Archive context” replace misleading approve/demote/archive labels.
- Repeated Learning actions have object-specific `aria-label` values.
- At 375 px, visible Learning buttons are 44 px high and the page has no horizontal overflow.
- Field labels are persistent and associated with the Human context inputs.
- A skip link, named primary navigation, `aria-current`, one `h1`, table captions, column scopes, coherent digest headings, and `:focus-visible` styling are present.
- Accepted context switches from a desktop table to readable mobile records.
- The missing charter and missing `policy.toml` states explain that core grants and safety are unchanged.
- Policy diagnostics clearly labels `policy.toml` as a legacy compatibility projection with no v3 safety authority.
- Digests render semantic headings and lists at a constrained measure, with raw markdown in collapsed disclosure.
- Delivered digests show an explicit word label and a timezone-bearing timestamp.
- The empty states are specific and materially more reassuring than round one.

## Prioritized findings and direct fixes

### P0 — Learning still presents compatibility data as live provider memory

**Evidence**

- The page says “Review the context providers can retrieve,” “Accepted context and provider influence,” and “This becomes provider context.”
- Accepted rows come from `memories`; “Add context” calls the legacy `MemoryWriter`; review actions mutate the same compatibility rows.
- The daemon composition labels this memory implementation a compatibility projection and does not pass it to the v3 router. Current durable provider observations instead flow through `interactions` + `human_facts` into `createProviderObservationLoop` and the selected memory provider.
- The Provider charter empty state says “Providers receive no charter text from …” even though that path describes only the compatibility charter, not every selected provider’s private state.

**Why this blocks release**

The new wording correctly denies execution authority but still promises proposal influence that the current v3 provider path does not consume. A non-authoritative surface must also be truthful about whether it is actually an input.

**Direct fix**

Choose one of these paths; do not mix them:

1. **Ship the compatibility view honestly now.** Rename the page/sections to **Learning compatibility**, **Legacy context review**, **Legacy accepted context**, and **Legacy charter**. Replace “providers can retrieve,” “provider influence,” and “becomes provider context” with explicit copy that these rows do not feed the v3 provider router. Rename influence badges to **Legacy setting** values. Hide or clearly scope mutation controls to the named legacy consumer.
2. **Ship a real v3 Learning surface.** Build the read/write model from `interactions`, `human_facts`, provider observation invocations, and provider response references. Submit human context with `store.recordHumanFact`/`recordInteraction`, then show its durable state (`received`, `acknowledged`, `consumed`, or `rejected`), resolved provider instance, and observation result. Restrict targeting to scopes the provider-resolution path can actually resolve; do not accept arbitrary provider/repository strings that are never used by `contextForFact`.

Provider-owned memory contents may be shown only through provider-supported links/exports or a versioned projection. Do not infer them from private provider state.

**Acceptance**

- Every Learning row names its authoritative source and exact current consumer.
- Adding context produces a durable human fact and a visible provider-observation lifecycle, or is explicitly labeled legacy-only.
- “Provider influence” is not inferred from the compatibility `autonomy` column.
- Charter copy describes only the compatibility charter unless a selected provider contract confirms receipt.
- No page claims a provider retrieved context without a recorded provider invocation/outcome.

### P0 — Digest delivery state is still not canonical

**Evidence**

- `listDigests` selects only `day`, `rendered_md`, and `sent_at`.
- `DigestsPage` maps every null `sent_at` to **Not delivered · No receipt recorded**.
- The database already links `digests.outbox_id` to the closed outbox lifecycle, including `pending`, `sending`, `delivered`, `uncertain`, `failed`, `deferred`, and other terminal states.

**Why this blocks release**

Null `sent_at` does not prove non-delivery. In particular, an `uncertain` remote-send crash window must never be presented as safely not delivered, because that invites a duplicate retry.

**Direct fix**

Join each digest to its canonical outbox row and render the actual state:

- no `outbox_id`: **Generated only · No delivery job recorded**;
- `pending`: **Queued**;
- `sending`: **Sending** with claim age;
- `delivered`: **Delivered** with reconciled timestamp and receipt identity;
- `uncertain`: **Delivery uncertain · Verify remotely before retrying**;
- `failed`: **Delivery failed** with failure summary and retry posture;
- `deferred`, `suppressed`, `expired`, or rejected states: use their exact closed meaning.

If outbox is `delivered` but the archive projection lacks `sent_at`, show **Receipt recorded · Archive reconciliation pending** and trigger/point to reconciliation; do not degrade it to “Not delivered.”

**Acceptance**

- No UI delivery label is derived from `sent_at` alone.
- The preview/test matrix covers no outbox, pending, sending, delivered, uncertain, failed, suppressed/deferred, and delivered-before-projection-reconciliation.
- Delivered rows expose destination and remote receipt identity through disclosure.
- Uncertain rows explicitly prohibit blind retry.
- Tests assert the exact canonical outbox-to-label mapping.

### P1 — Consequential Learning actions are still immediate and unrecoverable

**Evidence**

- The new action names and `aria-label` values are good.
- Dismiss, reduce influence, and archive remain direct POST forms with no consequence preview, confirmation, undo, or visible audit link.
- Accept/dismiss/reduce/archive redirect to the same page without a success message. Only Add context has a `role="status"` confirmation.
- Rejection and archive both set the legacy row to `archived`, but the UI offers no history or recovery path.

**Direct fix**

- Put **Dismiss proposal**, **Reduce influence**, and **Archive context** behind a server-rendered confirmation step or an accessible inline disclosure containing the exact object, scope, before/after state, consumer, and non-effect on grants/safety.
- Keep **Accept as context** one-step only when the card already shows its full target, destination, and consequence; otherwise confirm it too.
- Redirect with a one-time result code and render a `role="status"` message naming the completed action, plus **Undo** or **View audit entry**.
- Add an Archived/Dismissed history view so recovery is real rather than decorative.
- Preserve POST/Redirect/GET and authentication/CSRF protections.

**Acceptance**

- No destructive or influence-reducing action can happen from one ambiguous click.
- Every mutation has a visible result announcement and immutable audit link.
- Focus returns to the next logical review item after completion and to the restored item after undo.
- Keyboard and screen-reader tests cover confirmation, cancellation, completion, and recovery.

### P1 — The unauthenticated read-only state exposes controls that fail out of context

**Evidence**

GET pages are deliberately readable without a session, while writes require authentication. The Learning page is rendered without write capability state, so unauthenticated users still see enabled mutation buttons. A failed POST returns a bare 401 message with a sign-in link rather than preserving the task and consequence context.

**Direct fix**

- Pass `canWrite`/session state into Learning.
- When unauthenticated, replace mutation controls with one **Sign in to review** action and keep the item read-only.
- After successful sign-in, return to the originating item/confirmation step.
- Render authorization failures in the shared shell with a clear reason, retained context, and recovery action.

**Acceptance**

- Read-only users never encounter a mutation control that can only end on a bare error page.
- Authentication recovery returns to the exact pending action without submitting it automatically.
- Signed-in status and mutation availability are perceivable without relying on color.

### P1 — Policy is honest about the legacy file but not yet a policy-advice read model

**Evidence**

- The compatibility projection is clearly separated and correctly says it has no v3 safety authority.
- The nav says **Policy advice**, while the page is **Policy diagnostics** and shows no selected policy provider, instance, version, health, freshness, or evaluation history.
- “Only core grants and the non-bypassable safety kernel can authorize and execute an effect” compresses three different responsibilities. A grant authorizes; safety permits or blocks at claim; the core executor executes.

**Direct fix**

- Until an authoritative advisor read model exists, name both nav and page **Policy diagnostics** and say “Provider policy status is not shown on this page.”
- When the read model exists, add selected policy provider/instance/version, route selector, health/freshness, and recent advisory verdicts, always labeled **Advisory**.
- Change the banner to: “A core grant supplies authority. Core safety may still block the effect at claim time. Only the core executor performs it.”
- Keep the compatibility file in the lower diagnostic section.

**Acceptance**

- The page never implies that safety grants authority or that a provider executes an effect.
- “Policy advice” appears as a destination only when actual provider advice/status is shown.
- Provider advice, grant match, safety decision, and execution outcome remain four distinct labels wherever an effect is explained.

### P1 — Mobile navigation hides the current destination at 320 px

**Evidence**

At 320 px there is no page-level horizontal overflow, but the primary nav is a clipped/scrollable 345 px row inside a 228 px viewport. On direct entry to Policy advice or Digests, `scrollLeft` remains `0`; the active link is completely off-screen. The screenshot visibly cuts “Policy advice” and does not show Digests.

**Direct fix**

At the narrow breakpoint, use a two-row/wrapping nav, a compact menu, or programmatically scroll the active link into view on page load. Preserve at least 44 px targets and a visible non-color active indicator. If horizontal scrolling remains, add an obvious overflow cue and scroll-snap behavior; do not rely on users discovering an invisible gesture.

**Acceptance**

- On direct load at 320 and 375 px, the active destination is fully visible for all five routes.
- All destinations are discoverable using keyboard, touch, and VoiceOver without horizontal page overflow.
- The header remains usable at 200% and 400% zoom.

### P1 — “Needs you” reads well but is not actionable or traceable

**Evidence**

The rebuilt digest correctly puts **Needs you** before changes, but neither item is a link. The archive row exposes no destination, digest outbox ID, receipt ID, owning incident/grant/migration object, or delivery timeline.

**Direct fix**

- Store structured digest item references when building the digest; do not recover IDs by parsing prose.
- Render each “Needs you” item as a link to its durable owner, with the owning object type and current state.
- Add a compact delivery disclosure containing generated time, destination, outbox state history, receipt/failure identity, and reconciliation posture.
- Keep raw markdown for copy parity, but make the structured projection primary.

**Acceptance**

- Every actionable digest item opens the exact incident, grant, migration decision, or delivery record it refers to.
- The user can distinguish content requiring a decision from delivery verification and informational changes.
- Broken/missing references render as an explicit unavailable target, not silent plain text.

### P1 — Error and validation recovery remains below the new visual standard

**Evidence**

- Empty states for no review, no accepted context, no human context, missing charter, missing policy, and no digests are now specific and good.
- Policy parse errors have a visible critical notice and raw configuration.
- Blank server-side note input silently redirects; arbitrary provider/repository strings are accepted without validation.
- Unknown/stale memory actions return bare 404 text; unauthenticated writes return bare 401 text.
- There is no shared in-shell error state that preserves the object and offers retry/recovery.

**Direct fix**

- Validate scope against resolvable provider-selection inputs before writing; show field-level errors linked with `aria-describedby`.
- Render stale/not-found/conflict/auth failures in the shared shell with the attempted object/action, safe consequence, and recovery path.
- Distinguish a proposal already reviewed by another client from a missing ID.
- Keep user input populated after validation failure.
- Add tests for invalid scope, stale proposal, repeated submission, unauthenticated action, provider observation rejection, malformed policy, and missing digest receipt/outbox.

**Acceptance**

- No recoverable governance error ends on bare text or silently returns to an unchanged page.
- All errors identify what did not change, especially grants and safety.
- Error summaries and field messages are keyboard-focusable and announced.

### P2 — Learning still lacks enough provenance for confident review

**Evidence**

The review card now shows tier, scope, author, and age, and accepted rows show source/confidence/evidence counts. It still does not show the evidence itself, provider instance, proposal invocation, retrieval/observation outcome, last use, or audit history.

**Direct fix**

Add a detail disclosure/drawer backed by the authoritative interaction/provider-invocation/audit records. Show why proposed, evidence timeline, resolved provider instance, provider response reference, state transitions, and exact scope. Keep the list concise.

**Acceptance**

- A reviewer can explain why the context exists and which provider observed it without inspecting SQLite.
- Evidence counts open the confirming/contradicting records.
- Provider failures and rejection reasons are visible and do not masquerade as accepted context.

## Final fix order

1. Replace the legacy Learning claims/write path or explicitly ship it as compatibility-only.
2. Join digest delivery to canonical outbox state and remove the null-`sent_at` inference.
3. Add safe confirmation/result/undo or audit recovery for Learning mutations.
4. Make read-only authentication state explicit and recoverable.
5. Correct Policy responsibility copy and align its name with the data actually shown.
6. Make the current mobile route visible at 320 px.
7. Add structured digest references/delivery disclosure and in-shell error states.
8. Add Learning provenance disclosure, then rerun keyboard, VoiceOver, contrast, 320/375 px, 200%/400% zoom, and forced-colors checks.

## Evidence limits

- The preview was inspected read-only; confirmation, success, undo, authentication recovery, and mutation race behavior were assessed from rendered forms and source rather than executed.
- The supplied/live data covers populated Learning, missing charter, missing policy, one delivered digest, and one no-receipt digest. It does not visually demonstrate provider-observation rejection, malformed policy, uncertain/failed/suppressed delivery, empty Learning, empty Digests, or stale-action states.
- Screenshots and DOM inspection do not prove WCAG conformance. VoiceOver, forced colors, contrast measurement, focus restoration, zoom, and reduced-motion behavior still need hands-on verification after the fixes.
