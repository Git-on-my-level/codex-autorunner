# PMA Hub Frontend — Design System

This doc captures the visual and interaction language for the SvelteKit frontend
under `src/codex_autorunner/pma_frontend/`. It exists so future agents can ship
new pages and components that look and feel native to the rest of the app
without re-deriving the rules every time.

If you are about to add a new page, panel, list, card, or dialog, **read this
first**. If you are about to override `app.css` globals or add new design
tokens, also read this first.

## North Star

The hub is a focused, internal product for engineers. The visual taste is
**Linear and Notion**, with a small dose of Stripe Dashboard restraint:

- **Quiet by default, loud only where it matters.** Status, action, and live
  state get color. Everything else stays in the neutral grey scale.
- **Tight, inline, single-row headers.** A page header is a label, not a
  marketing banner. No large hero panels, no gradients spanning the page.
- **Cards do work.** Each row/card carries a clear identity (avatar, name),
  meta (branch, time, counts), and an action surface (hover chevron, click
  anywhere). Avoid double-nesting cards inside panels inside cards.
- **Density without crowding.** Prefer `--space-2`/`--space-3` between
  related elements, `--space-4`/`--space-5` between sections. Don't waste a
  half-screen on a header that says "Repos".
- **Hover earns affordance.** Borders strengthen, shadows lift gently,
  hidden chevrons slide in. Never animate layout-shifting properties.
- **Each fact lives in exactly one place.** If the breadcrumb already
  carries `#5b78de`, don't repeat it in the header subtitle. If the
  scope is a clickable ingress link, don't also render it as plain
  meta text. When two surfaces show the same value, delete the weaker
  one — usually the read-only twin of the actionable one.
- **Suppress empty signals.** A row that reads `idle · idle · elapsed
  n/a · queue 0` is communicating nothing four times. Hide bars and
  pills when the only thing they have to say is "nothing is happening."
  Only render status surfaces when the value is non-default and
  meaningful (`running`, `waiting`, `blocked`, `failed`, queue > 0,
  elapsed known).

Reference implementations:

- `src/lib/components/RepoWorktreeViews.svelte` (index branch) — header,
  KPI strip, repo cards, nested worktree tree.
- `src/lib/components/PmaMemoryView.svelte` — header, segmented tabs,
  reader card with sunken header bar.
- `src/lib/components/SettingsView.svelte` — long config page using flat
  `.settings-section` blocks separated by hairline dividers, no nested
  panel chrome. Use this pattern for any page whose primary job is to
  show many small settings groups.

When picking a reference, prefer the structural one (one of the three
above) over a leaf component. Layout patterns proliferate; copy a known
good shape rather than inventing a new one.

## Tokens

All design tokens live in `src/app.css` under `:root`. **Always use the
tokens. Never hardcode colors, radii, shadows, or spacing in component
styles.**

### Color
- Surface scale: `--color-bg`, `--color-surface`, `--color-surface-muted`,
  `--color-surface-sunken`. Page background is `--color-bg`. Cards are
  `--color-surface`. Sunken bars (table headers, nested-row backdrops,
  empty states) are `--color-surface-sunken`. Chips and inputs use
  `--color-surface-muted`.
- Ink scale: `--color-ink` (titles), `--color-ink-soft` (body),
  `--color-ink-muted` (meta), `--color-ink-faint` (decorative dots,
  separators, timestamps).
- Semantic: `--color-success`, `--color-warning`, `--color-danger`,
  `--color-accent` (indigo `#5b5fc7` — the brand). Each has a `-soft`
  variant for chip backgrounds. Status dots/strips use the solid; chip
  fills use the `-soft` with the solid as text.
- Borders: `--color-border-subtle` for everyday divisions and card
  borders, `--color-border` for inputs, `--color-border-strong` for hover
  states.

### Spacing
- Steps: `--space-1` (4px) → `--space-10` (40px). Mostly use 2/3/4/5/6.
- Inside a card: `--space-3` to `--space-5` padding.
- Between sibling cards: `--space-2` to `--space-3` gap.
- Between page sections: `--space-3` to `--space-5` gap on the
  `.page-stack`.

### Radii
- 6–8px for chips, pills, buttons, inputs.
- 10–12px for cards and panels.
- 14px max — only for the outermost surfaces (hero, modal). Never higher.

### Shadows
- `--shadow-1`: resting card shadow (almost invisible). Use sparingly.
- Custom hover lift for cards:
  `0 8px 24px -16px rgb(15 15 20 / 0.18), 0 2px 6px -3px rgb(15 15 20 / 0.06)`.
  Don't invent new shadow values; copy this one.
- `--shadow-2`: modals only.
- `--shadow-focus`: focus rings (already wired on globals).

### Typography
- Body font: Inter (already loaded). Mono: JetBrains Mono.
- Sizes: `--font-size-0` (12px) for meta and chips, `--font-size-1` (13px)
  for secondary UI, `--font-size-2` (15px) for primary body, `--font-size-3`
  (17px) for card titles, `--font-size-4` (20px) for page H1, `--font-size-5`
  (26px) only for high-density dashboards / detail H1.
- Weights: 500 for muted, 550–600 for body bold, 650 for headings and
  numerics. Never go above 700.
- Letter spacing: `-0.01em` to `-0.022em` on headings, default elsewhere.
- Tabular numerics: always `font-variant-numeric: tabular-nums` on counts,
  KPIs, and timestamps.

### Motion
- Use `--transition-fast` (120ms) for hover state transitions.
- Use `--transition-base` (180ms) for layout-affecting transitions
  (chevron slide, lift).
- All easing should reference `--ease-out`. Never write custom cubics.
- Respect `prefers-reduced-motion` (already globally honored).

## Page Skeleton

Every full-page route renders into the workspace shell and should follow
this structure:

```svelte
<section class="page-stack <page-name>-page">
  <PageHero title="…" subtitle="…">
    {#snippet stats()}<dl class="hero-stats">…</dl>{/snippet}
  </PageHero>

  <!-- Degraded/partial-page issue banners (use the snippet pattern) -->

  <!-- Primary content: list, card grid, or panel -->
</section>
```

Use the shared `PageHero` component for the title + subtitle + optional
right-aligned KPI strip. **Don't reach for a hand-rolled `.section-heading`
with `<p class="eyebrow">`.** The eyebrow shape is the most common way
this design system gets violated; if you can't express the page using
`PageHero`, that's a signal to extend `PageHero`, not to fork.

Hard rules for hero headers:

- **One row, baseline-aligned.** Title and stats sit on the same baseline
  on desktop. Below `760px`, the stats wrap underneath.
- **No background fill, no border on the hero itself.** The header is
  bare type on the page background. Save chrome for content cards.
- **Subtitle is one line.** If you need more, you're solving the wrong
  problem — push detail into the cards or a separate help affordance.
- **No eyebrow text** ("REPO OWNERSHIP", "OVERVIEW", "LOCAL MODE"). The
  topbar breadcrumb already tells the user where they are. If a page is
  ambiguous without an eyebrow, fix the breadcrumb or the title — don't
  paper over it.

### Header rows for dense detail surfaces

Some surfaces (chat detail, ticket detail) carry more identity than a
single subtitle row can hold without wrapping into mush. Use up to
three left-aligned rows in the header:

1. Title (`<h1>`).
2. Scope/parent identity — usually a single clickable ingress link
   (`worktree → repo`, `repo → org`). The most scannable handle for
   "where am I?" sits here, not buried in meta.
3. Meta row — kind badge, status, ticket id, locked-config tag, etc.
   Allow this row to wrap; it's the lowest-density tier.

For live state and locked configuration that doesn't belong in the
left meta column, use a **right-side aside** in the header
(`.chat-header-aside` is the reference). Cap the aside at ~50% width,
align right-stacked, and hide it entirely when nothing inside it has
content. Don't render an empty aside just to "balance" the header.

### Inline KPI strip (`.hero-stats`)

Drop a `.hero-stats` `<dl>` into the hero `stats` snippet when there are
1–4 small numbers worth promoting. Tokens are wired in `app.css`; do not
duplicate them per page.

- Each entry is `<dd>{value}</dd><dt>{label}</dt>`. Value first because it
  is the load-bearing element; label below in muted ink.
- When a number is "interesting" (active runs > 0, pending approvals > 0,
  missing docs), tint both value and label with the matching semantic
  color (`--color-success`, `--color-warning`, `--color-danger`). **Never
  tint the zero state** — a green "0" reads as success when it's really
  "nothing to celebrate yet."
- Stats can be `<a>` anchors when clicking should jump to the relevant
  section; they pick up hover background automatically.

## Cards

A card is a self-contained, clickable unit (or a unit that contains a
single primary action). The canonical layout:

```
[ avatar ] [ title  + status pill ]              [ count chips ] [ → ]
           [ meta · meta · meta · time         ]
```

Rules:

- The whole card is the link (`<a class="repo-card" href=…>`). Avoid nested
  anchors. If you need secondary actions, put them as separate buttons
  underneath, not inside the link.
- Border: `1px solid var(--color-border-subtle)`, radius 12px, background
  `--color-surface`.
- Hover: border becomes `--color-border-strong`, add the lift shadow,
  reveal the chevron `→` (opacity 0 → 1, translateX(-4px) → 0).
- Status accent: a 3px left strip absolutely positioned, color matched to
  status. Off when status is `idle`/neutral.
- Padding: `var(--space-4) var(--space-5)`. Reduce to `--space-3` when the
  card has a nested child list (worktrees) so the parent doesn't feel
  detached.

### Avatar

Identity glyph for repos, agents, and any other entity with a name:

- 40px square, 10px radius (squircle).
- Background: `color-mix(in srgb, var(--accent) 12%, white)`.
- Foreground: same accent at full strength.
- Inner ring: `inset 0 0 0 1px color-mix(in srgb, var(--accent) 18%, transparent)`.
- Initials are 1–2 uppercase letters extracted from the label
  (`repoInitials()` in `RepoWorktreeViews.svelte`).
- Accent color hashes deterministically from the label using a fixed
  8-color palette (`repoAccent()`). Reuse this helper, don't invent new
  colors per entity type.

### Title row

- Name: `var(--font-size-2)` weight 600, ellipsis when overflowing.
- Status pill immediately to the right (lowercase, `.status-pill.<status>`).
- Reserve any other actions for the right edge.

### Meta row

- `var(--font-size-0)`, color `var(--color-ink-muted)`, line-height 1.4.
- Items separated by an explicit `·` dot in a `<span class="meta-dot">`,
  faint color, opacity 0.7.
- A small uppercase chip (`var(--color-surface-muted)` background, 10px,
  weight 600, 0.04em tracking) is fine for "type" tags like
  `repo`/`worktree`. Use it sparingly — at most one per row.
- Branches and paths render in JetBrains Mono with a leading `⎇` glyph at
  `--color-ink-faint`.
- The relative time goes last, in `--color-ink-faint`, tabular-nums.

### Count chips

For numeric counts (runs, tickets, open items):

```html
<span class="count-chip is-active">
  <strong>{n}</strong><em>runs</em>
</span>
```

- Pill, 999px radius, 24px min-height, neutral grey by default.
- When the value is meaningful (`is-active`, `is-tickets`, `is-warning`),
  switch to the soft variant of that color and tint the text.
- Don't render a count chip for zero unless it's contextually needed.
- Plural the noun based on the count (`run` vs `runs`).

## Nested lists (trees)

When a card has children that belong to it (worktrees under a repo,
threads under a chat), nest them inside the parent card with a connector
rail rather than a flat indented list:

- Children list sits on a `--color-surface-sunken` strip inside the parent
  card, separated by `border-top: 1px solid var(--color-border-subtle)`.
- Each child uses absolute-positioned rail + connector lines:
  - Vertical rail: 1px wide, `--color-border-strong`, runs from the top of
    the children area down through the dot of each child.
  - Horizontal connector: 12px wide, joins the rail to the child's dot.
- Each child has a colored status dot (6px), with a 3px white halo
  (`box-shadow: 0 0 0 3px var(--color-surface)`) so it punches through
  the rail.
- Child content uses `--font-size-1` (13px), one tier smaller than the
  parent card. Less padding, no avatar.

See `.worktree-list` / `.worktree-card` in `RepoWorktreeViews.svelte` for
the complete pattern.

## Identity and disambiguation

Lists frequently contain rows whose human-facing names collide ("New PMA
chat" × 4, "Untitled ticket" × 8, default workspace names). Names alone
are not addresses. **A row must always carry one stable, scannable
identifier in addition to its name** so that a user can refer to "the
third one" or correlate with logs.

The canonical identifier is a short id chip:

```
<span class="chat-id-tag">#a1b2c3</span>
```

- 6 mono characters of the resource's stable id.
- Lives in the meta row of the card, never in the title.
- Renders even when the name is unique — consistency beats cleverness, and
  it gives users a copyable handle.

This pattern applies anywhere the system might surface a default-named
resource: chats, runs, tickets, drafts, scratch repos. Backend should
also try to give resources meaningful names (first-message excerpt, repo
slug, etc.); the id chip is the floor, not the ceiling.

## Status communication

Pick one and only one of these per element. Never combine more than two:

| Mechanism | Use for |
| --- | --- |
| `.status-pill.<status>` (lowercase, soft fill + solid text) | Discrete state on a title row |
| 3px left accent strip on a card | Scannable state across many cards in a list |
| Colored dot on a child row | Compact tree/list rows where a pill would be too loud |
| Tinted count chip (`is-active`, `is-tickets`) | Highlight a meaningful nonzero number |
| Tinted hero KPI | Page-level summary |
| Thin tinted progress bar | Long-running work where remaining time matters more than discrete state |

Never use color alone for state. Always pair with a label or icon.

### Active vs terminal states

Loud status surfaces (pills, accent strips, sticky banners) are for
**active** states the user can still influence: `running`, `waiting`,
`blocked`, `failed`. Terminal/stable states (`done`, `idle`) should
either be silent or fold into an existing meta row.

- After a turn finishes, the assistant's reply itself signals "done."
  A persistent `done · 4s elapsed` chip on top of that is duplicated
  signal. If completion timing is genuinely useful (audit, debugging),
  inline it into the existing subtitle as one more meta token (`done ·
  completed · 4s elapsed`) — don't promote it to a chip again.
- An `idle` row is almost always noise. If you can't say anything more
  than the word `idle`, render nothing.

### Status dots — two scales

Status dots come in two sizes; pick the one that matches the surrounding
density.

| Variant | Size | Halo | Use |
| --- | --- | --- | --- |
| Nested row dot | 6px | 3px surface halo | Tree/connector rows where the dot punches through a rail |
| Inline meta dot | 6px | none | Row meta lines, header subtitles, anywhere the dot lives in a flowing text run |

Both share the same `--color-*` semantic palette via `.status-dot.status-<status>`.
Never invent intermediate sizes; if the scale doesn't fit, you're using
the wrong mechanism.

### Progress indicators

Use `.progress-track` for any long-running, indeterminate-but-bounded
work. The default is intentionally subtle:

- 2px tall, full width, neutral surface track.
- Fill color comes from a `status-<status>` modifier on the track itself
  (`status-running` → success, `status-waiting` → warning, etc.).
- **Never use `--color-accent` (brand purple) for the fill.** Purple in
  this system means "click me / active selection," not "in progress." A
  thick purple bar reads as a CTA, not a status indicator.
- Pair the bar with a textual percent or step label nearby. The bar alone
  is not enough — many runs sit at 64% indefinitely.

## Tabs

Use the segmented-pill pattern (see `.memory-tabs-v2` in `PmaMemoryView`):

- Container: `1px solid var(--color-border-subtle)`, radius 10px, 4px
  inner padding, `--color-surface` background.
- Tab buttons: 6×10px padding, 7px radius, transparent border, muted text.
- Hover: `--color-surface-muted` background, ink text.
- Active: same `--color-surface-muted` background plus
  `inset 0 0 0 1px var(--color-border-subtle)` to read as a pressed pill,
  weight 600 ink text.
- Optional small status dot before the label (6px), with a soft halo
  on the active tab.
- Optional mono `<small>` filename suffix for file/document tabs.

## Buttons

Three variants, no more. Pick deliberately — every extra primary button
on a page is a vote against the one that actually matters.

1. **Primary action** (`.send-button`, `.new-chat-button`): accent fill,
   white text, weight 550, soft accent shadow. **One per surface.** If
   you find yourself adding a second, ask whether it should be a ghost
   button or an icon button.
2. **Secondary / ghost** (`.ghost-button`, `.memory-copy-button`): 1px
   border, surface background, ink-soft text. Hover strengthens border
   and switches to `--color-surface-muted`. Use for inline actions inside
   a card header, or for any action that should not compete with the
   page's primary CTA.
3. **Icon button** (`.icon-button`): 30×30px, transparent until hover,
   ink-faint icon. Use only when an inline label would clutter the row.
   Always `aria-label`.

Heights: 28–34px depending on density. Inputs match button heights.

### Dirty-state buttons (form save)

Form save buttons should not exist as a permanently-loud primary CTA next
to a settings panel header. Instead:

- Render as `.ghost-button` by default. Disabled, label reads "Saved" or
  similar terminal verb.
- When the form becomes dirty (any field differs from the last saved
  snapshot), add a `.dirty` modifier: accent border + accent text, still
  ghost-shaped. Label switches to the action ("Save preferences").
- After a successful save, snapshot the new values and return to disabled
  ghost — the button announces stability rather than demanding clicks.

This pattern keeps the page calm at rest and unambiguous when there's
work to commit. It applies to settings, profile, preferences, and any
other "I'm editing in place, hit save when done" surface. **Don't use it
for transactional actions** (Send message, Create chat, Approve) — those
stay as solid primary buttons because the action is the point.

## Empty / loading / error states

- All three render via `.state-panel` (already styled in `app.css`).
- Empty: `compact-empty` + `empty-state` modifier inside the relevant
  panel; never as a full-page splash unless the route literally has
  nothing.
- Loading: `.state-panel.loading-state` with the shimmer `.state-icon`.
- Error: `.state-panel.error` with a Retry button.
- Partial-page degradations: render the `degradedIssues` snippet
  per-section. Don't fail the whole page when only one slice failed.

### Empty state as primary canvas

When a primary view has no content yet (a chat with no messages, a
ticket queue with no tickets, a memory page with no notes), the
otherwise-blank center column is the **best** place for setup or
configuration affordances. Don't squeeze them into the page header
where they steal density from identity/meta and force the user to
look in two places at once.

Concrete pattern:

- Header stays minimal — title + scope + meta only.
- The empty state replaces the would-be content with a focused setup
  card centered on `margin: auto 0`. Just the controls the user
  actually needs to start, no marketing copy or explanatory paragraphs
  about what they're about to do (the labels carry that signal).
- Once the view has any real content, the setup card disappears and
  the controls either move to a compact passive display in the header
  or vanish entirely (see Lifecycle controls).

This is how the chat detail's "pick agent / model / effort" lives
when a chat is fresh, instead of stacked in the header.

## Lifecycle controls

A control is either editable for the entire life of the surface, or
it's a one-time-at-start choice. **Pick the model that matches reality
and never present an "edit" UI for something that's actually locked.**

- **Editable throughout** (e.g. composer textarea, search input,
  filter chips): always show as live controls.
- **Locked after first action** (e.g. agent for a chat — switching it
  silently forks a new chat anyway): show editable only in the empty
  state. Once the surface has activity, render the value as **plain
  read-only text** — usually a small mono tag in the meta row. Never
  render a disabled `<select>` with explanatory hint text; a disabled
  control invites a click that has no good outcome.
- **Editable but consequential** (e.g. model on a chat that supports
  mid-conversation switching): keep editable; place behind a chip +
  popover so the loud "controls" chrome doesn't compete with the
  primary canvas.

Boilerplate prose like "Approvals apply during turns according to the
selected approval policy and sandbox mode" does not belong next to
the composer. If the rule never changes with state, it's documentation
— move it to the help system or the docs folder, not the runtime UI.

## Navigation and deep links

Selectable detail views must be **path-addressable**, not query-string
addressable. `/chats/abc123` beats `/chats?detail=chat:abc123&chat=abc123`
on every axis: bookmarkable, shareable, breadcrumbable, back-buttonable,
and visible in the URL bar without scanning.

Conventions:

- One file per master-detail page using SvelteKit optional params
  (`/chats/[[chatId]]/+page.svelte`). The same component handles both
  list and detail; it reads `page.params.<id>` to decide which mode
  to render.
- Selecting a row pushes the path via `goto`. Closing the detail
  navigates back to the list path. Both directions update history so
  the browser back button works.
- Add a structured route in `breadcrumbs.ts` for any new path-based
  detail view. Keep most-specific patterns first.
- The breadcrumb is the **canonical back affordance**. Don't ship a
  separate "back to list" button when the breadcrumb already does
  the job.

### Master-detail toggles

Toggle widgets (`Chats / Detail` segmented control) are a fallback for
viewports that can't fit both panes side-by-side. They are not a
primary navigation pattern. Hide them whenever the URL already
addresses the state — at narrow viewports, navigating between list
and detail happens via the URL (clicking a card pushes the detail
path; clicking the breadcrumb returns to the list).

`MasterDetail.svelte` accepts `showSwitch={false}` for exactly this
case. Reach for it any time you give the page proper deep links.

## Forms and inputs

- Inputs: 1px border, 6–8px radius, `--color-surface` or
  `--color-surface-muted` background. Min-height matches buttons.
- Search fields use the muted background with no visible border until
  hover (see `.search-field input`).
- Labels render above the input as a small uppercase tag, weight 500,
  `--color-ink-muted`.
- Modals use the `.modal-backdrop` + `.approval-modal` shell.

## Accessibility

- Color contrast: muted ink is barely AA. Anything below
  `--color-ink-muted` is decoration only — never the only carrier of
  information.
- Always pair color with text. Status dots have adjacent labels. Count
  chips include the noun.
- `aria-label` every icon-only button, every nav, every list region.
- Pulse animations and shimmer states must respect
  `prefers-reduced-motion` (the global block in `app.css` neutralizes
  most transitions; verify yours by setting Reduce Motion).
- Tab order follows visual order. Don't reorder with `tabindex`.
- **Heading hierarchy.** Every page has exactly one `<h1>`, supplied by
  `PageHero`. Section titles are `<h2>`, subheads inside a section are
  `<h3>`. Don't skip a level for visual reasons; restyle the level
  instead. A page with no `<h1>` (e.g. a chat surface where the active
  thread title is the most prominent text) must still emit an `<h1>` —
  promote the existing heading or add a visually-styled `<h1>` rather
  than a visually-hidden one.

## Browser baseline

This app targets the latest two versions of evergreen browsers (Chrome,
Edge, Firefox, Safari). The following modern CSS features are explicitly
allowed and used in `app.css`:

- `color-mix(in srgb, …)` — for status-tinted backgrounds derived from
  semantic tokens. Use when you need a token-relative color but a
  pre-baked `-soft` variant doesn't exist. Don't introduce a new token
  for a one-off tint.
- `:has(...)` — for parent selectors that depend on child presence (e.g.
  flipping a grid template when a row contains a chip).
- `:where()`, `:is()`, logical properties (`margin-inline`,
  `padding-block`), `dvh`/`svh` units, `gap` on flex.

When a feature is too new for the baseline (e.g. CSS nesting at the time
of writing), add a comment in `app.css` calling out the polyfill or
fallback path. Don't silently rely on it.

## Svelte and code conventions

- Svelte 5 runes throughout: `$state`, `$derived`, `$props`, `$effect`.
  No legacy stores in new code.
- Component scripts: `<script lang="ts">`. Props go through `$props()` with
  an inline type literal; default optional props with `= null` / `= []`
  in the destructure.
- Prefer scoped `<style>` blocks per component for new visual rules.
  Promote to `app.css` only when ≥3 components share the rule.
- Use `:global()` to pierce a child component's styles (e.g. EditableMarkdown's
  body inside the memory reader). Never hand-edit a child component to
  satisfy the parent's layout.
- Classes are kebab-case BEM-ish (`.repo-card`, `.repo-card-body`,
  `.repo-card-counts`). No utility soup, no Tailwind in this app.
- View models live under `src/lib/viewModels/`; components consume the
  shaped output. Don't reach into raw API data from components.

## Responsive

The breakpoints are `1020px`, `760px`, `460px` (already declared in
`app.css`). Apply these inside component `<style>` blocks too:

- ≥ 1020: full hero with right-side KPIs, two-column dashboard grids.
- 760–1020: tighten gaps; allow 2-column lists to collapse to 1.
- < 760: hero collapses to vertical stack, count chips wrap onto a new
  row inside the card, chevrons hide, nested rails narrow.

## Anti-patterns

Don't:

- Wrap a list in a panel that has its own H2 if the page already has a
  hero. Pick one heading per slice of content.
- Stack borders inside borders (panel border + card border + inner row
  border). Always remove one of them. Long config pages should use the
  flat `.settings-section` pattern (hairline dividers between sections)
  rather than panel-in-panel.
- Use brand purple (`--color-accent`) for non-action affordances. Purple
  means "click me" or "active selection." A green dot means running, a
  yellow dot means waiting — purple does not mean "info" or "in
  progress." This applies to progress bars, status indicators, "live"
  badges, and any other non-interactive affordance.
- Invent new shadows, radii, or grey values. If the token doesn't exist,
  add it to `:root` in `app.css` with a justification, then use it.
- Use uppercase eyebrows on every page. They were a phase. Drop them.
  If you find yourself writing `<p class="eyebrow">`, that's the signal
  to use `PageHero` instead.
- Render a page heading as `<h2>` with no `<h1>` above it. Either promote
  to `<h1>` or supply one via `PageHero`. Don't add visually-hidden
  headings to satisfy a linter — the page hierarchy should match what the
  user sees.
- Render zero counts in a loud color, or pluralize "1 runs."
- Treat the human-facing name as a unique identifier. Always pair
  default-named or collision-prone resources with a `#shortid` chip.
- Add a permanent loud primary CTA to a settings or preferences surface.
  Use the dirty-state ghost pattern instead.
- Build new layout primitives by copying CSS into a component's `<style>`
  block when the same shape exists in `app.css` or as a shared component.
  Promote the pattern, then use it.
- Render the same value twice on the same surface. If the breadcrumb
  shows `#abc123`, drop the chip in the subtitle. If the ingress link
  shows `repo / worktree`, drop the duplicate scope text in meta. Pick
  the more useful (usually actionable) representation; delete the rest.
- Render an "edit" UI for a control that's actually locked. A disabled
  `<select>` is a worse signal than plain read-only text — it invites
  hover and click and then disappoints. If the control can't change,
  display the value, not the form.
- Render an empty status pill ("idle · idle · elapsed n/a · queue 0").
  Hide the surface when it has nothing to say.
- Ship a query-string deep link (`?detail=chat:abc&chat=abc`) for a
  selectable row when the route supports a path segment. Path-based
  URLs are the convention for selectable detail views; query strings
  are for filters and transient flags.
- Use a master-detail toggle as the primary nav. The breadcrumb is
  the canonical back affordance; toggles are a fallback for narrow
  viewports where deep links also handle navigation. Hide the toggle
  with `showSwitch={false}` once the page is path-addressable.
- Keep boilerplate prose in the runtime UI ("Approvals apply during
  turns…"). If the rule never changes with state, it belongs in the
  docs, not the chrome.

## Build

After visual changes:

```bash
cd src/codex_autorunner/pma_frontend && pnpm run build
```

This regenerates `pma_static/`. The hub serves these directly; no extra
deploy step.

## When in doubt

Open the two reference components, copy the closest existing pattern,
then trim. The goal is for any new page to feel like it has been here
since day one.
