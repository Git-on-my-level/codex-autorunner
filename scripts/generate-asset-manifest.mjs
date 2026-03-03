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
import crypto from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function computeFileHash(filePath) {
  const stat = fs.statSync(filePath);
  if (stat.isDirectory()) {
    return null;
  }
  const content = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(content).digest('hex').slice(0, 12);
}

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

  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), 'utf8');
  
  console.log(`Generated manifest at ${path.relative(process.cwd(), manifestPath)}`);
  console.log(`  - ${manifest.generated.length} generated files`);
  console.log(`  - ${manifest.manual.length} manual files`);
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
