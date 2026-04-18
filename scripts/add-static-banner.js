#!/usr/bin/env node

/**
 * Add generated file banner to compiled JS files.
 * This script should be run after TypeScript compilation.
 */

import fs from 'fs';
import path from 'path';
import { glob } from 'glob';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BANNER = '// GENERATED FILE - do not edit directly. Source: static_src/\n';
const VERBOSE = process.env.CAR_VERBOSE_STATIC_POSTBUILD === '1';

function renderSummary(updatedFiles) {
  if (updatedFiles.length === 0) {
    console.log('Static banners unchanged');
    return;
  }

  if (VERBOSE || updatedFiles.length <= 5) {
    console.log(`Updated static banners (${updatedFiles.length}):`);
    for (const file of updatedFiles) {
      console.log(`- ${file}`);
    }
    return;
  }

  console.log(
    `Updated static banners for ${updatedFiles.length} generated file(s); set CAR_VERBOSE_STATIC_POSTBUILD=1 to list paths`,
  );
}

async function main() {
  const staticDir = path.join(__dirname, '..', 'src', 'codex_autorunner', 'static', 'generated');
  const pattern = path.join(staticDir, '**', '*.js').replace(/\\/g, '/');

  const files = await glob(pattern, {
    ignore: ['**/vendor/**', '**/node_modules/**']
  });
  const updatedFiles = [];

  for (const file of files) {
    try {
      const content = fs.readFileSync(file, 'utf8');

      // Skip if banner already exists
      if (content.startsWith(BANNER.trim())) {
        continue;
      }

      // Add banner at the beginning
      const newContent = BANNER + content;
      fs.writeFileSync(file, newContent, 'utf8');
      updatedFiles.push(path.relative(process.cwd(), file));
    } catch (err) {
      console.error(`Error processing ${file}:`, err.message);
      process.exit(1);
    }
  }

  renderSummary(updatedFiles);
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
