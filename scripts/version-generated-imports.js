#!/usr/bin/env node

/**
 * Stamp generated intra-bundle import specifiers with a stable per-build id.
 *
 * Browsers resolve static relative ESM imports literally. If `app.js?v=new`
 * imports `./pma.js`, the child URL is still just `./pma.js` unless we stamp it.
 * That allows stale and fresh generated chunks to mix after a rebuild.
 *
 * This script rewrites generated relative `.js` specifiers like:
 *   from "./foo.js"
 *   export { ... } from "./foo.js"
 *   import("./foo.js")
 * to:
 *   "./foo.js?v=<build-id>"
 *
 * The build id is computed from normalized generated JS content so rerunning the
 * script on unchanged output is idempotent.
 */

import crypto from "crypto";
import fs from "fs";
import path from "path";
import { glob } from "glob";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const GENERATED_DIR = path.join(
  __dirname,
  "..",
  "src",
  "codex_autorunner",
  "static",
  "generated"
);

const IMPORT_FROM_RE =
  /\b(from\s+["'])(\.\.?\/[^"'?#]+\.js)(?:\?v=[^"']+)?(["'])/g;
const SIDE_EFFECT_IMPORT_RE =
  /\b(import\s+["'])(\.\.?\/[^"'?#]+\.js)(?:\?v=[^"']+)?(["'])/g;
const DYNAMIC_IMPORT_RE =
  /\b(import\s*\(\s*["'])(\.\.?\/[^"'?#]+\.js)(?:\?v=[^"']+)?(["']\s*\))/g;

function rewriteWithVersion(content, version) {
  const append = (_match, prefix, specifier, suffix) =>
    `${prefix}${specifier}?v=${version}${suffix}`;
  return content
    .replace(IMPORT_FROM_RE, append)
    .replace(SIDE_EFFECT_IMPORT_RE, append)
    .replace(DYNAMIC_IMPORT_RE, append);
}

function normalizeContent(content) {
  return rewriteWithVersion(content, "__CAR_GENERATED_BUILD_ID__");
}

async function main() {
  const pattern = path.join(GENERATED_DIR, "**", "*.js").replace(/\\/g, "/");
  const files = (await glob(pattern, {
    ignore: ["**/vendor/**", "**/node_modules/**"],
  })).sort();

  const digest = crypto.createHash("sha256");
  for (const file of files) {
    const content = fs.readFileSync(file, "utf8");
    digest.update(path.relative(GENERATED_DIR, file).replace(/\\/g, "/"));
    digest.update("\0");
    digest.update(normalizeContent(content));
    digest.update("\0");
  }
  const buildId = digest.digest("hex");

  const updated = [];
  for (const file of files) {
    const current = fs.readFileSync(file, "utf8");
    const next = rewriteWithVersion(current, buildId);
    if (next === current) {
      continue;
    }
    fs.writeFileSync(file, next, "utf8");
    updated.push(path.relative(process.cwd(), file));
  }

  if (updated.length === 0) {
    console.log("Generated import versions unchanged");
    return;
  }
  console.log(
    `Stamped versioned intra-bundle imports for ${updated.length} generated file(s)`
  );
}

main().catch((err) => {
  console.error("Error:", err);
  process.exit(1);
});
