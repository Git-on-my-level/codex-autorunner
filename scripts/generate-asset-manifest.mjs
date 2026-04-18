#!/usr/bin/env node
/**
 * Generate asset manifest JSON for frontend assets.
 * This script runs after TypeScript compilation to create a manifest
 * of all static assets (both manual and generated).
 */

import fs from 'fs';
import path from 'path';
import { glob } from 'glob';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function main() {
  const staticDir = path.join(__dirname, '..', 'src', 'codex_autorunner', 'static');
  const manifestPath = path.join(staticDir, 'assets.json');

  const manifest = {
    version: "1",
    generated: [],
    manual: []
  };

  const generatedPattern = path.join(staticDir, 'generated', '**', '*.js').replace(/\\/g, '/');
  const generatedFiles = await glob(generatedPattern);
  
  for (const file of generatedFiles) {
    const stat = fs.statSync(file);
    if (!stat.isFile()) continue;
    const relPath = path.relative(staticDir, file).replace(/\\/g, '/');
    manifest.generated.push({ path: relPath });
  }

  const manualPatterns = [
    '*.html',
    '*.css',
    'vendor/**/*'
  ];
  
  for (const pattern of manualPatterns) {
    const fullPattern = path.join(staticDir, pattern).replace(/\\/g, '/');
    const files = await glob(fullPattern);
    for (const file of files) {
      const stat = fs.statSync(file);
      if (!stat.isFile()) continue;
      const relPath = path.relative(staticDir, file).replace(/\\/g, '/');
      if (!relPath.includes('generated')) {
        manifest.manual.push({ path: relPath });
      }
    }
  }

  manifest.generated.sort((a, b) => a.path.localeCompare(b.path));
  manifest.manual.sort((a, b) => a.path.localeCompare(b.path));

  const nextContent = JSON.stringify(manifest, null, 2);
  const prevContent = fs.existsSync(manifestPath)
    ? fs.readFileSync(manifestPath, 'utf8')
    : null;

  if (prevContent === nextContent) {
    console.log('Static asset manifest unchanged');
    return;
  }

  fs.writeFileSync(manifestPath, nextContent, 'utf8');

  console.log(
    `Updated static asset manifest: ${path.relative(process.cwd(), manifestPath)} (${manifest.generated.length} generated, ${manifest.manual.length} manual)`,
  );
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
