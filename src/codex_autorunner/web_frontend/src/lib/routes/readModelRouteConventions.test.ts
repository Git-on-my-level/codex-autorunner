import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const ROUTES_ROOT = fileURLToPath(new URL('../../routes', import.meta.url));

const KNOWN_DIRECT_READ_MODEL_PAGES = new Set<string>();

const DIRECT_READ_MODEL_PATTERNS = [
  /\bgetServicesReadModel\s*\(/,
  /\bgetAutomationWorkspace(?:Index)?\s*\(/,
  /\breadModels\.\w+\s*\(/,
  /['"`]\/hub\/read-models\//
];

function walkSveltePages(dir: string, files: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) {
      walkSveltePages(path, files);
      continue;
    }
    if (entry === '+page.svelte') files.push(path);
  }
  return files;
}

function walkRouteLoaders(dir: string, files: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) {
      walkRouteLoaders(path, files);
      continue;
    }
    if (entry === '+page.ts') files.push(path);
  }
  return files;
}

function hasDirectReadModelFetch(source: string): boolean {
  return DIRECT_READ_MODEL_PATTERNS.some((pattern) => pattern.test(source));
}

function hasReadModelEnsureCall(source: string): boolean {
  return /\bensure[A-Za-z0-9]+Loaded\s*\(/.test(source);
}

function hasNonBlockingLoader(source: string): boolean {
  return /\bblocking\s*:\s*false\b/.test(source);
}

describe('read-model route loading conventions', () => {
  it('keeps read-model snapshots behind route loaders and the shared entity store', () => {
    const violations: string[] = [];

    for (const file of walkSveltePages(ROUTES_ROOT)) {
      const relativePath = relative(ROUTES_ROOT, file);
      if (KNOWN_DIRECT_READ_MODEL_PAGES.has(relativePath)) continue;
      const source = readFileSync(file, 'utf8');
      if (!hasDirectReadModelFetch(source)) continue;
      const routeLoad = join(dirname(file), '+page.ts');
      violations.push(
        existsSync(routeLoad)
          ? relativePath
          : `${relativePath} (missing sibling +page.ts route loader)`
      );
    }

    expect(violations).toEqual([]);
  });

  it('keeps route read-model loaders non-blocking so cached screens stay responsive', () => {
    const violations: string[] = [];

    for (const file of walkRouteLoaders(ROUTES_ROOT)) {
      const source = readFileSync(file, 'utf8');
      if (!hasReadModelEnsureCall(source)) continue;
      if (hasNonBlockingLoader(source)) continue;
      violations.push(relative(ROUTES_ROOT, file));
    }

    expect(violations).toEqual([]);
  });
});
