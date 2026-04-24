# CAR UI Style Guide

This document captures the design principles, patterns, and conventions used in the
Codex Autorunner web UI. Read this before making visual changes.

## Product Context

The web UI is a **power-user hub** — not the daily driver. Users interact with CAR day-to-day via Discord/Telegram. The web UI serves three jobs:

1. **Initial setup** — add repos, configure agents, set up chat integrations
2. **Debugging** — inspect run logs, contextspace, terminal sessions
3. **Deep-dives** — tickets, run histories, usage stats, workspace docs

Design decisions should serve these use cases. Do not add surface area for things users do in their chat client.

## Design Philosophy

The UI is a **developer tool dashboard** — not a consumer app. Prioritize:

- **Density over whitespace.** Screen real estate is precious, especially on mobile. Every pixel of vertical space should earn its keep.
- **Subtlety over decoration.** Borders, backgrounds, and shadows should be minimal. Let content breathe through spacing, not chrome.
- **Progressive disclosure.** Show the minimum needed; reveal detail on hover or expand.
- **One design system.** Do not fork the UI into "onboarding mode" and "power mode." Behavioral routing (empty hub → PMA) and empty-state copy do the contextual work. The component library stays unified.

## Design Tokens

All values live in `:root` in `styles.css`. Never use magic numbers — always reference tokens.

### Colors

| Token | Value | Usage |
|---|---|---|
| `--bg` | `#0a0c12` | Page background |
| `--panel` | `#10131c` | Card/panel background |
| `--panel-elevated` | `#141825` | Modals, elevated surfaces, PMA chat area |
| `--text` | `#e5ecff` | Primary text |
| `--muted` | `#7a8ba8` | Secondary/label text |
| `--accent` | `#6cf5d8` | Primary accent (teal) — actions, highlights, cursor |
| `--accent-2` | `#6ca8ff` | Secondary accent (blue) — secondary actions, info states |
| `--accent-glow` | `rgba(108,245,216,0.08)` | Subtle ambient glow for focus rings and hover states |
| `--border` | `#1a2033` | Default border |
| `--border-subtle` | `rgba(26,32,51,0.6)` | Lightweight separators |
| `--error` | `#ff5566` | Error states |
| `--success` | `#58d68d` | Success states |
| `--warning` | `#ffd166` | Warning states |

Do not introduce new accent colors. If a new semantic meaning needs color, use opacity/weight variants of existing tokens first.

### Spacing Scale (`--sp-*`)

| Token | Value | Typical use |
|---|---|---|
| `--sp-1` | 2px | Tight inline gaps |
| `--sp-2` | 4px | Card padding, list gaps |
| `--sp-3` | 6px | Section padding, control gaps |
| `--sp-4` | 8px | Standard gap, button padding |
| `--sp-5` | 12px | Generous section padding |
| `--sp-6` | 16px | Large section margins |
| `--sp-7` | 24px | Page-level spacing |
| `--sp-8` | 32px | Empty-state / PMA landing layouts |
| `--sp-9` | 48px | Hero sections in empty state |

### Radii

- `--radius`: 3px (default for cards, inputs, pills)
- `--radius-lg`: 6px (modals, large containers)
- `999px` for pill-shaped elements (mode toggles, badges)

## Typography

- **Font:** JetBrains Mono (monospace) throughout — this is a dev tool. No exceptions. `system-ui` and `-apple-system` are not acceptable as primary or body fonts. (The `launchFinishPageTemplate` Go template is exempt — it cannot import CSS tokens; use system font stack there only.)
- **Base size:** 13px body.
- **Hierarchy:**

  | Role | Size | Weight | Notes |
  |---|---|---|---|
  | Hero heading (empty/PMA state) | 20–22px | 700 | Anchors the PMA landing when hub has no repos |
  | Section titles / hub hero | 13–14px | 700 | |
  | Card titles | 12px | 600 | |
  | Body / metadata | 13px | 400 | Standard text |
  | Labels (`.label`) | 10–11px | 400–600 | Uppercase, `letter-spacing: 0.04–0.06em`, `--muted` color |
  | Small / compact | 9–10px | 400 | Stats, badges, secondary metadata |

- Never go below 8px for any readable text.
- Always use `font-variant-numeric: tabular-nums` for counters, token counts, timestamps, and stat values.

## Hub Information Architecture

### Empty State Routing

When the hub has zero repos, route directly to the **PMA tab** instead of the empty dashboard. The PMA is the first-run experience. The hub hero heading should read:

> "No repos yet — ask the PM Agent to get you started."

This is a behavioral/routing decision, not a layout redesign. The PMA tab UI itself requires no changes.

### Walkthrough Strip

A dismissible top-of-page progress strip that appears on first run (before the user has seen or dismissed it). It sits above the hub hero and collapses after all steps complete or the user closes it.

**Structure:**
- Step counter on left (e.g., "Step 2 of 4")
- Step title in center
- Prompt chip buttons on right (see component spec below)
- Close (×) at far right

**Persistence:** Dismissed state stored in `localStorage`. The walkthrough never reappears after explicit close.

**Prompt chip buttons** (`.walkthrough-chip`):
```
border-radius: 999px
border: 1px solid var(--accent)
color: var(--accent)
background: transparent
padding: var(--sp-2) var(--sp-4)
font-size: 11px
font-family: var(--font-mono)
cursor: pointer
```
Hover: `background: var(--accent-glow)`. Click navigates to the PMA tab and injects a preset prompt string into the PMA input. The prompt text is defined in code, not in the UI string.

Example chips: "Set up Discord →", "Set up Telegram →", "Add your first repo →", "Run my first ticket →".

### Hub Leaderboard Removal

The horizontal scrolling usage leaderboard (the list of dozens of repo names with token counts) is **removed from the hub landing**. It creates visual noise and is not useful as a landing element.

Move token/usage statistics to:
- Per-repo detail view (usage tab or stats section)
- A dedicated global Stats section accessible from the hub nav

The compact `.hub-stats-inline` (repos / running / missing counters) stays.

## Layout Patterns

### Hub Page Structure

```
.hub-shell
  .walkthrough-strip       → first-run only, dismissible (see above)
  header.hub-hero          → title + actions + mode toggle
  .hub-stats-inline        → compact stat counters
  section.hub-repo-panel   → collapsible repo list
  section.hub-agent-panel  → collapsible agent list
```

### Hero Header

- Uses CSS Grid: `grid-template-columns: minmax(0,1fr) auto auto`
- Three regions: text (title+version), action buttons, mode toggle
- On mobile: switches to 2-row grid — title+toggle on row 1, actions on row 2

### Collapsible Panels

Panel expand/collapse buttons (`.hub-panel-summary`) should be:
- **Single-line.** Title label on the left, state indicator on the right. No subtitle text.
- **Borderless.** Use transparent background with subtle hover highlight.
- **Compact.** 4px vertical padding.
- State text (Expanded/Show panel) uses small uppercase accent text, fades in on hover.

### Repo Cards

Cards use a flex row layout (grid on mobile):
- `.hub-repo-left`: pin indicator (hidden on mobile)
- `.hub-repo-center`: title, badges, metadata, flow progress
- `.hub-repo-right`: action buttons, right-aligned

Action buttons fade to 50% opacity and brighten on hover (desktop). On mobile, always
fully visible since there's no hover.

Long metadata lines (`.hub-repo-info-line`) must truncate with `text-overflow: ellipsis`.
Always ensure parent containers have `overflow: hidden` and `min-width: 0`.

### Filter/Sort/Search Controls

On desktop: single inline row with Flow select, Sort select, and search input.

On mobile: use the **search-on-top** pattern:
- CSS Grid on the container: `grid-template-columns: auto auto 1fr`
- Search input spans full width on row 1: `grid-column: 1 / -1; grid-row: 1`
- Filter selects auto-flow onto row 2 side by side

This is the standard pattern used by GitHub Mobile, Linear, and similar tools.

## Responsive Breakpoints

| Breakpoint | Target | Key behavior |
|---|---|---|
| `> 900px` | Desktop | Full layout, hover states, all controls visible |
| `≤ 900px` | Tablet | Hero stays 3-column, repo rows wrap, cards compact |
| `≤ 640px` | Mobile | Hero → 2-row grid, cards → grid layout, search-on-top, smaller fonts |
| `≤ 400px` | Small mobile | Further font/padding reduction |

### Mobile Rules

1. **Hero header:** 2-row grid. Row 1 = title + mode toggle. Row 2 = action buttons spanning full width. Hide version text and scan pill to save space.
2. **Repo cards:** Switch from flex to CSS Grid (`1fr auto`). Content left, actions stacked right. Pin indicator hidden.
3. **Filter controls:** Grid layout with search spanning full width on its own row above the filter dropdowns.
4. **Text overflow:** All metadata/info lines must truncate. Set `overflow: hidden` on parent containers and `min-width: 0` on grid/flex children.
5. **Touch targets:** Minimum 44px for primary actions (Apple HIG). Buttons get slightly more padding on mobile.
6. **No hover-dependent UI:** Anything hidden behind hover on desktop must be always-visible on mobile.

## Component Conventions

### Buttons

- Primary: `.primary.sm` — accent background, dark text
- Ghost: `.ghost.sm` — transparent with border, muted text
- Icon: `.icon-btn` — square, icon-only
- Size: always use `.sm` in hub/dashboard context

### Pills/Badges

- Place immediately after the element they describe
- Use `.pill`, `.pill-small` with status modifiers (`pill-idle`, `pill-warn`, `pill-error`)
- Flow status pills use specific source-color classes (discord=blue, telegram=teal, pma=gold)

### Stats

Use inline text rather than card-style boxes for stat counters. The `.hub-stats-inline`
pattern displays stats as `<value> label` spans with `--muted` color and bold values.

### Modals

- Max-width constrained, centered overlay
- Header: label + close button
- Body: form groups separated by spacing, not borders
- Actions: right-aligned, ghost cancel + primary confirm

## Motion

Every animation has a semantic job. Nothing decorative.

| Name | Duration | Easing | Use |
|---|---|---|---|
| micro | 50–100ms | ease | Button active, pill toggle, badge state |
| short | 150ms | ease-out | Walkthrough step slide |
| medium | 200ms | ease-out | Hub empty→loaded fade, modal open |
| counter | 250ms | ease-out | Stat counter roll on page load |

- PMA typing indicator: 3-dot pulse, `--muted` color. Standard chat convention.
- Do not add motion longer than 300ms anywhere in the data-view path. Power users find it in the way.
- Avoid motion that repeats without user interaction.

## Anti-Patterns

Never introduce:
- Purple/violet gradients as accent or decoration
- 3-column feature grids with icons in colored circles
- Gradient buttons as primary CTA
- `system-ui` or `-apple-system` as font (except the `launchFinishPageTemplate` exemption above)
- Horizontal scrolling lists at hub level
- Decorative blobs or background shapes
- Centered-everything layouts in data views (data views must be left-aligned)
- Uniform bubbly `border-radius` on all elements (keep 3px/6px/999px hierarchy)

## CSS Architecture

- **Single file:** All styles live in `styles.css`. No CSS modules or preprocessors.
- **Class naming:** `hub-` prefix for hub page, `pma-` for PM Agent, `ticket-` for tickets.
- **No `!important`:** Solve specificity with source order and selector structure.
- **Transitions:** Keep under 150ms. Use `ease` timing. Only transition properties that change.
- **Mobile overrides:** Place in the appropriate `@media` block. Don't duplicate — override only what changes.

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-23 | One design system, no visual fork for onboarding | Behavioral routing (empty hub → PMA) does the contextual work. Forking the component library adds maintenance cost with no UX gain. |
| 2026-04-23 | Empty hub → PMA tab routing | PMA is the right first-run surface. Better onboarding than an empty dashboard skeleton. |
| 2026-04-23 | First-run walkthrough with prompt chips | Guided setup without leaving the product. Chips fire preset PMA prompts for Discord/Telegram/repo setup using existing CAR guides. |
| 2026-04-23 | Remove horizontal usage leaderboard from hub | Visual noise on a page where users are trying to debug or set up repos. Stats moved to per-repo or a dedicated Stats section. |
| 2026-04-23 | Added --accent-glow token | Hover and focus states needed a non-destructive background treatment. rgba(108,245,216,0.08) uses the existing accent hue without adding a new color. |
| 2026-04-23 | Added --sp-8 (32px) and --sp-9 (48px) | PMA/empty-state layouts need more vertical breathing room than compact data views. |
| 2026-04-23 | Sharpened typography hierarchy | The existing 13px-dominant scale blurred section hierarchy. Added 20–22px/700 for empty-state hero; reinforced weight contrast at card and label levels. |
| 2026-04-23 | launchFinishPageTemplate font exemption | Go HTML template cannot import CSS tokens. Must use system font stack there; dark theme colors still apply. |

## Common Pitfalls

1. **Flex children overflowing:** Always set `min-width: 0` on flex children that should shrink. CSS flex items default to `min-width: auto` which prevents shrinking below content size.
2. **Grid children overflowing:** Same rule — `min-width: 0` and `overflow: hidden` on grid children with text content.
3. **Mobile hover states:** Any opacity/visibility change on hover must have a mobile override to show the element unconditionally.
4. **Search/filter controls on mobile:** Don't use single-line horizontal scroll. Use the grid-based search-on-top pattern.
5. **Inline-flex labels:** `<label>` elements with `display: inline-flex` won't stretch in grid cells. Override to `display: flex` or `display: block` on mobile.
6. **Fat section headers:** Panel expand/collapse buttons should be single-line. Don't add subtitle/description text — the section name is sufficient.
