import { readdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * A `var(--token)` whose token is never declared does not fall back to a
 * sensible default — the declaration is invalid at computed-value time and the
 * property drops to its inherited or initial value. That failure is silent: a
 * popover loses its background and floats transparently over the page, a
 * heading loses its colour, a card loses its radius, and nothing warns. We hit
 * exactly that with `--color-surface-raised`, `--shadow-sm`, `--radius-sm`, and
 * `--color-text` before this test existed.
 *
 * So: every token referenced without an explicit fallback must be declared in
 * the token layer, or set at runtime by a component through an inline style.
 */

const SRC_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const TOKEN_FILES = ['app.css', 'theme-presets.css'];

/**
 * Tokens that components assign per-instance through `style="--x: …"` rather
 * than declaring globally. They are legitimately absent from the token layer,
 * so the source of truth for each one is the component that sets it.
 */
const RUNTIME_ASSIGNED_TOKENS = new Set([
  '--accent',
  '--chat-status-overlay-clearance',
  '--delay',
  '--glyph-accent',
  '--notice-timeout',
  '--palette-accent',
  '--progress',
  '--repo-accent',
  '--virtual-list-gap',
  '--virtual-list-item-min-height'
]);

function collectSourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === '.svelte-kit') continue;
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      collectSourceFiles(path, out);
      continue;
    }
    if (extname(path) === '.css' || extname(path) === '.svelte') out.push(path);
  }
  return out;
}

function declaredTokens(): Set<string> {
  const declared = new Set<string>();
  for (const file of TOKEN_FILES) {
    const source = readFileSync(join(SRC_ROOT, file), 'utf8');
    for (const match of source.matchAll(/(--[a-z0-9-]+)\s*:/g)) declared.add(match[1]);
  }
  return declared;
}

/** `var(--x)` references, skipping `var(--x, fallback)` which degrades safely. */
function referencesWithoutFallback(source: string): string[] {
  const found: string[] = [];
  for (const match of source.matchAll(/var\(\s*(--[a-z0-9-]+)\s*\)/g)) found.push(match[1]);
  return found;
}

describe('design tokens', () => {
  const declared = declaredTokens();
  const files = collectSourceFiles(SRC_ROOT);

  it('finds the token layer and the stylesheets that consume it', () => {
    expect(declared.size).toBeGreaterThan(40);
    expect(files.length).toBeGreaterThan(20);
  });

  it('declares every token referenced without a fallback', () => {
    const missing = new Map<string, string[]>();
    for (const file of files) {
      for (const token of referencesWithoutFallback(readFileSync(file, 'utf8'))) {
        if (declared.has(token) || RUNTIME_ASSIGNED_TOKENS.has(token)) continue;
        const users = missing.get(token) ?? [];
        users.push(file.slice(SRC_ROOT.length));
        missing.set(token, users);
      }
    }
    expect(Object.fromEntries(missing)).toEqual({});
  });

  it('defines the same token set in every bundled theme', () => {
    const appCss = readFileSync(join(SRC_ROOT, 'app.css'), 'utf8');
    const rootBlock = appCss.slice(appCss.indexOf(':root {'), appCss.indexOf('[data-theme="dark"]'));
    const colorTokens = [...rootBlock.matchAll(/(--color-[a-z0-9-]+)\s*:/g)].map((m) => m[1]);
    // Every theme overrides the palette; a theme that skips one of these
    // inherits a light-mode colour into a dark surface.
    const required = ['--color-bg', '--color-surface', '--color-ink', '--color-accent'];
    for (const token of required) expect(colorTokens).toContain(token);

    const presets = readFileSync(join(SRC_ROOT, 'theme-presets.css'), 'utf8');
    const themeBlocks = presets.split(/\[data-theme="[a-z-]+"\]\s*\{/).slice(1);
    expect(themeBlocks.length).toBeGreaterThan(0);
    for (const block of themeBlocks) {
      for (const token of required) expect(block).toContain(`${token}:`);
    }
  });

  it('keeps the UI and mono families separate so prose is not set in mono', () => {
    const appCss = readFileSync(join(SRC_ROOT, 'app.css'), 'utf8');
    expect(appCss).toMatch(/--font-ui:/);
    expect(appCss).toMatch(/--font-mono:/);
    // The document default is the proportional face; mono is opt-in per element.
    expect(appCss).toMatch(/body\s*\{[^}]*font-family:\s*var\(--font-ui\)/);
  });

  it('routes every mono declaration through the token', () => {
    // A hand-written `ui-monospace, SFMono-Regular, …` stack drifts from the
    // token: it misses JetBrains Mono, and changing the family later leaves
    // these behind. `--font-mono` itself is the one place the stack is spelled.
    const offenders: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, 'utf8');
      for (const match of source.matchAll(/font-family:[^;]*;/g)) {
        if (match[0].includes('--font-mono:')) continue;
        if (/ui-monospace|SFMono-Regular|JetBrains Mono/.test(match[0])) {
          offenders.push(`${file.slice(SRC_ROOT.length)}: ${match[0].trim()}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
