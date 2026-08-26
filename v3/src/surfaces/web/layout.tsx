/**
 * Shared page chrome: nav + minimal inline CSS. Zero external assets — this must
 * render correctly fully offline on localhost. No htmx, no CDN scripts.
 */
import type { FC, PropsWithChildren } from "hono/jsx";

/**
 * All in-page links/forms use this absolute "/ui" prefix rather than relative
 * paths, since pages are served from a sub-app mounted at "/ui" — relative hrefs
 * would resolve against the current page path (e.g. /ui/memory), not the mount
 * root, and silently break navigation.
 */
export const UI_ROOT = "/ui";

const NAV = [
  { href: UI_ROOT, label: "Inbox" },
  { href: `${UI_ROOT}/incidents`, label: "Incidents" },
  { href: `${UI_ROOT}/memory`, label: "Memory" },
  { href: `${UI_ROOT}/policy`, label: "Policy" },
  { href: `${UI_ROOT}/digests`, label: "Digests" },
];

const CSS = `
  :root {
    color-scheme: light dark;
    --fg: #1a1a1a; --bg: #ffffff; --muted: #6b6b6b; --border: #d9d9d9;
    --accent: #2952cc; --danger: #b3261e; --ok: #1b7a3d; --chip-bg: #f0f0f2;
  }
  @media (prefers-color-scheme: dark) {
    :root { --fg: #e8e8e8; --bg: #14161a; --muted: #9a9a9a; --border: #33363c;
      --accent: #7da3ff; --danger: #ff6b60; --ok: #56d987; --chip-bg: #202329; }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  header { border-bottom: 1px solid var(--border); padding: 10px 20px; display: flex; align-items: baseline; gap: 18px; }
  header .brand { font-weight: 700; letter-spacing: 0.02em; }
  header nav { display: flex; gap: 14px; }
  header nav a { color: var(--muted); text-decoration: none; font-size: 13px; }
  header nav a:hover, header nav a.active { color: var(--accent); }
  main { padding: 18px 20px 60px; max-width: 1100px; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  h2 { font-size: 15px; margin: 26px 0 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
  h3 { font-size: 13px; margin: 16px 0 6px; color: var(--muted); }
  table { border-collapse: collapse; width: 100%; margin-bottom: 10px; }
  th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }
  tr:hover td { background: var(--chip-bg); }
  a { color: var(--accent); }
  code, pre { font: 12.5px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
  pre { background: var(--chip-bg); padding: 10px 12px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
  .chip { display: inline-block; padding: 1px 8px; border-radius: 10px; background: var(--chip-bg); font-size: 11px; border: 1px solid var(--border); }
  .chip.severity-urgent { border-color: var(--danger); color: var(--danger); }
  .chip.severity-attention { border-color: #c98a1a; color: #c98a1a; }
  .chip.severity-notice { border-color: var(--accent); color: var(--accent); }
  .chip.autonomy-granted { border-color: var(--ok); color: var(--ok); }
  .chip.autonomy-suggest { border-color: #c98a1a; color: #c98a1a; }
  .chip.autonomy-none { color: var(--muted); }
  .muted { color: var(--muted); }
  .filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; align-items: center; }
  .filters input, .filters select { font: inherit; padding: 4px 6px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--fg); }
  form.inline { display: inline; }
  button, input[type=submit] { font: inherit; padding: 3px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--chip-bg); color: var(--fg); cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.danger:hover { border-color: var(--danger); color: var(--danger); }
  .actions-cell { white-space: nowrap; }
  .section { margin-bottom: 30px; }
  .empty { color: var(--muted); font-style: italic; padding: 8px 0; }
  textarea { width: 100%; font: 12.5px/1.4 ui-monospace, monospace; background: var(--chip-bg); color: var(--fg); border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
  .pager { margin-top: 10px; }
  .pill-row { display: flex; gap: 6px; flex-wrap: wrap; }
`;

export const Layout: FC<PropsWithChildren<{ title: string; active?: string }>> = ({ title, active, children }) => (
  <html lang="en">
    <head>
      <meta charSet="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{title} — CAR</title>
      <style>{CSS}</style>
    </head>
    <body>
      <header>
        <span class="brand">CAR</span>
        <nav>
          {NAV.map((item) => (
            <a href={item.href} class={active === item.href ? "active" : ""}>
              {item.label}
            </a>
          ))}
        </nav>
      </header>
      <main>{children}</main>
    </body>
  </html>
);

export function severityChip(sev: string) {
  return <span class={`chip severity-${sev}`}>{sev}</span>;
}

export function autonomyChip(autonomy: string) {
  return <span class={`chip autonomy-${autonomy}`}>{autonomy}</span>;
}
