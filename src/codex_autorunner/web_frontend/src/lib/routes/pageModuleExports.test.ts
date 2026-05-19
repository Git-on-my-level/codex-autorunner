import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const ROUTES_ROOT = fileURLToPath(new URL('../../routes', import.meta.url));

const ALLOWED_RUNTIME_EXPORTS = new Set([
  'load',
  'prerender',
  'csr',
  'ssr',
  'trailingSlash',
  'config',
  'entries'
]);

function walkPageModules(dir: string, files: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) {
      walkPageModules(path, files);
      continue;
    }
    if (entry === '+page.ts' || entry === '+page.server.ts') {
      files.push(path);
    }
  }
  return files;
}

function runtimeExportNames(source: string): string[] {
  const names = new Set<string>();

  for (const match of source.matchAll(/^export (?:async )?(?:function|const|let|var) (\w+)/gm)) {
    names.add(match[1]);
  }

  for (const match of source.matchAll(/^export \{\s*([^}]+)\s*\}(?:\s+from\s+['"][^'"]+['"])?;?$/gm)) {
    for (const part of match[1].split(',')) {
      const trimmed = part.trim();
      if (!trimmed) continue;
      const exported = trimmed.split(/\s+as\s+/).pop()?.trim();
      if (exported) names.add(exported);
    }
  }

  return [...names];
}

function isAllowedExport(name: string): boolean {
  return ALLOWED_RUNTIME_EXPORTS.has(name) || name.startsWith('_');
}

describe('SvelteKit page module exports', () => {
  it('keeps runtime exports limited to SvelteKit route entrypoints', () => {
    const violations: string[] = [];

    for (const file of walkPageModules(ROUTES_ROOT)) {
      const source = readFileSync(file, 'utf8');
      const invalid = runtimeExportNames(source).filter((name) => !isAllowedExport(name));
      if (invalid.length === 0) continue;
      violations.push(`${relative(ROUTES_ROOT, file)}: ${invalid.join(', ')}`);
    }

    expect(violations).toEqual([]);
  });
});
