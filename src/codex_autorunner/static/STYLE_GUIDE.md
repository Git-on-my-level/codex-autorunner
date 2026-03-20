# CAR UI Style Guide

This document captures the design principles, patterns, and conventions used in the
Codex Autorunner web UI. Read this before making visual changes.

## Design Philosophy

The UI is a **developer tool dashboard** — not a consumer app. Prioritize:

- **Density over whitespace.** Screen real estate is precious, especially on mobile. Every pixel of vertical space should earn its keep.
- **Subtlety over decoration.** Borders, backgrounds, and shadows should be minimal. Let content breathe through spacing, not chrome.
- **Progressive disclosure.** Show the minimum needed; reveal detail on hover or expand.

## Design Tokens

All values live in `:root` in `styles.css`. Never use magic numbers — always reference tokens.

### Colors

| Token | Value | Usage |
|---|---|---|
| `--bg` | `#0a0c12` | Page background |
| `--panel` | `#10131c` | Card/panel background |
| `--panel-elevated` | `#141825` | Modals, elevated surfaces |
| `--text` | `#e5ecff` | Primary text |
| `--muted` | `#7a8ba8` | Secondary/label text |
| `--accent` | `#6cf5d8` | Primary accent (teal) |
| `--accent-2` | `#6ca8ff` | Secondary accent (blue) |
| `--border` | `#1a2033` | Default border |
| `--border-subtle` | `rgba(26,32,51,0.6)` | Lightweight separators |
| `--error` | `#ff5566` | Error states |
| `--success` | `#58d68d` | Success states |
| `--warning` | `#ffd166` | Warning states |

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

### Radii

- `--radius`: 3px (default for cards, inputs, pills)
- `--radius-lg`: 6px (modals, large containers)
- `999px` for pill-shaped elements (mode toggles, badges)

## Typography

- **Font:** JetBrains Mono (monospace) throughout — this is a dev tool.
- **Base size:** 13px body.
- **Hierarchy:**
  - Section titles / hero: 13–14px, `font-weight: 700`
  - Card titles: 12px, `font-weight: 600`
  - Labels (`.label`): 10–11px, uppercase, `letter-spacing: 0.04–0.06em`, `--muted` color
  - Body/metadata: 10px
  - Small/compact: 9px
- Never go below 8px for any readable text.

## Layout Patterns

### Hub Page Structure

```
.hub-shell
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

## CSS Architecture

- **Single file:** All styles live in `styles.css`. No CSS modules or preprocessors.
- **Class naming:** `hub-` prefix for hub page, `pma-` for PM Agent, `ticket-` for tickets.
- **No `!important`:** Solve specificity with source order and selector structure.
- **Transitions:** Keep under 150ms. Use `ease` timing. Only transition properties that change.
- **Mobile overrides:** Place in the appropriate `@media` block. Don't duplicate — override only what changes.

## Common Pitfalls

1. **Flex children overflowing:** Always set `min-width: 0` on flex children that should shrink. CSS flex items default to `min-width: auto` which prevents shrinking below content size.
2. **Grid children overflowing:** Same rule — `min-width: 0` and `overflow: hidden` on grid children with text content.
3. **Mobile hover states:** Any opacity/visibility change on hover must have a mobile override to show the element unconditionally.
4. **Search/filter controls on mobile:** Don't use single-line horizontal scroll. Use the grid-based search-on-top pattern.
5. **Inline-flex labels:** `<label>` elements with `display: inline-flex` won't stretch in grid cells. Override to `display: flex` or `display: block` on mobile.
6. **Fat section headers:** Panel expand/collapse buttons should be single-line. Don't add subtitle/description text — the section name is sufficient.
