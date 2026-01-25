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

async function main() {
  const staticDir = path.join(__dirname, '..', 'src', 'codex_autorunner', 'static');
  const pattern = path.join(staticDir, '**', '*.js').replace(/\\/g, '/');
  
  const files = await glob(pattern, {
    ignore: ['**/vendor/**', '**/node_modules/**']
  });
  
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
      
      console.log(`Added banner to: ${path.relative(process.cwd(), file)}`);
    } catch (err) {
      console.error(`Error processing ${file}:`, err.message);
      process.exit(1);
    }
  }
  
  console.log(`Added banners to ${files.length} file(s)`);
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
