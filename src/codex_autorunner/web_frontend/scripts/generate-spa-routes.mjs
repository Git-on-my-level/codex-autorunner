#!/usr/bin/env node
// Postbuild step: materialize the SvelteKit filesystem router as a route
// manifest the Python hub (FastAPI/Starlette) can consume at runtime.
//
// Why this exists: the hub must return the SPA shell (index.html) for any
// document request (refresh / open-in-new-tab) to a URL the client router can
// render. Previously that meant hand-maintaining a second, parallel list of
// FastAPI routes mirroring `src/routes/**/+page.svelte` — the two lists could
// (and did) drift. This script makes `src/routes/` the single source of
// truth: it walks the same filesystem SvelteKit uses for routing and emits
// `spa_routes.json` into the build output (`web_static/`, which IS shipped in
// the Python package — see pyproject.toml package-data).
//
// Segment translation (SvelteKit -> Starlette), mirrored in
// scripts/check_web_hub_spa_shell.py so the two stay in lockstep:
//   [foo]      -> {foo}
//   [[foo]]    -> optional: emit both with and without the segment
//   [...foo]   -> {foo:path}
//   literal    -> unchanged
//
// This manifest is a faithful, complete mirror of `src/routes/` (minus the
// root `/` page, which the hub always redirects). It does NOT encode any
// hub-side policy about which routes get bespoke handling (legacy redirects,
// worktree-scope validation, "nest tolerant" catch-alls, etc.) — that policy
// lives in app.py and is intentionally NOT derived from this manifest.

import { mkdirSync, readdirSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = join(__dirname, '..');
const ROUTES_DIR = join(FRONTEND_ROOT, 'src', 'routes');
const OUTPUT_DIR = join(FRONTEND_ROOT, '..', 'web_static');
const OUTPUT_PATH = join(OUTPUT_DIR, 'spa_routes.json');

/** Recursively find every `+page.svelte` under `dir`, returning absolute paths. */
function findPageFiles(dir) {
  const results = [];
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch (err) {
    if (err && err.code === 'ENOENT') return results;
    throw err;
  }
  for (const entry of entries) {
    if (entry.name === 'node_modules') continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...findPageFiles(full));
    } else if (entry.isFile() && entry.name === '+page.svelte') {
      results.push(full);
    }
  }
  return results;
}

/** Translate one SvelteKit route segment into its Starlette equivalent(s). */
function translateSegment(segment) {
  const optionalMatch = segment.match(/^\[\[([^\]]+)\]\]$/);
  if (optionalMatch) {
    return { kind: 'optional', param: optionalMatch[1].trim() };
  }
  const restMatch = segment.match(/^\[\.\.\.([^\]]+)\]$/);
  if (restMatch) {
    return { kind: 'literal', value: `{${restMatch[1].trim()}:path}` };
  }
  const dynamicMatch = segment.match(/^\[([^\]]+)\]$/);
  if (dynamicMatch) {
    return { kind: 'literal', value: `{${dynamicMatch[1].trim()}}` };
  }
  return { kind: 'literal', value: segment };
}

/**
 * Build every Starlette path template implied by one +page.svelte location.
 * Trailing `[[optional]]` segments expand into "with" and "without" variants
 * (matching SvelteKit's own optional-segment semantics); returns [] for the
 * root page (handled separately by the hub's `/` redirect).
 */
function routeTemplatesForPage(relDirParts) {
  const nonGroup = relDirParts.filter(
    (part) => !(part.startsWith('(') && part.endsWith(')'))
  );
  if (nonGroup.length === 0) return [];

  const translated = nonGroup.map(translateSegment);

  // Count a trailing run of optional segments (the only shape present today,
  // and the only one SvelteKit meaningfully supports for omission).
  let optionalRun = 0;
  for (let i = translated.length - 1; i >= 0; i -= 1) {
    if (translated[i].kind === 'optional') {
      optionalRun += 1;
    } else {
      break;
    }
  }

  const literalValues = translated.map((seg) =>
    seg.kind === 'optional' ? `{${seg.param}}` : seg.value
  );

  if (optionalRun === 0) {
    return ['/' + literalValues.join('/')];
  }

  const variants = [];
  for (let drop = 0; drop <= optionalRun; drop += 1) {
    const kept = literalValues.slice(0, literalValues.length - drop);
    if (kept.length > 0) {
      variants.push('/' + kept.join('/'));
    }
  }
  return variants;
}

function main() {
  const pages = findPageFiles(ROUTES_DIR);
  const routes = new Set();

  for (const pagePath of pages) {
    const relDir = relative(ROUTES_DIR, dirname(pagePath));
    if (relDir === '') continue; // root `/` page: hub always redirects, no shell entry needed
    const parts = relDir.split(sep);
    for (const template of routeTemplatesForPage(parts)) {
      routes.add(template);
    }
  }

  const manifest = {
    generated_at: new Date().toISOString(),
    routes: Array.from(routes).sort(),
  };

  mkdirSync(OUTPUT_DIR, { recursive: true });
  // Write atomically: a reader that catches a half-written file parses it as
  // malformed and silently falls back to broad SPA coverage, so the window
  // where the file exists but is incomplete must not exist.
  const tmpPath = `${OUTPUT_PATH}.tmp`;
  writeFileSync(tmpPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf-8');
  renameSync(tmpPath, OUTPUT_PATH);
  console.log(
    `Wrote ${manifest.routes.length} SPA route templates to ${relative(FRONTEND_ROOT, OUTPUT_PATH)}`
  );
}

main();
