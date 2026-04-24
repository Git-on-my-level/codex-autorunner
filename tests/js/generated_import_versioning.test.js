import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

const ROOT = process.cwd();
const GENERATED_DIR = path.join(
  ROOT,
  "src",
  "codex_autorunner",
  "static",
  "generated"
);

const IMPORT_SPEC_RE =
  /\b(?:import|export)\b[\s\S]*?\bfrom\s+["'](\.\.?\/[^"'?#]+\.js(?:\?v=[^"']+)?)["']|\bimport\s*\(\s*["'](\.\.?\/[^"'?#]+\.js(?:\?v=[^"')]+)?)["']\s*\)/g;

function listGeneratedJs(dir) {
  const files = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...listGeneratedJs(fullPath));
      continue;
    }
    if (entry.isFile() && entry.name.endsWith(".js")) {
      files.push(fullPath);
    }
  }
  return files.sort();
}

test("generated intra-bundle import specifiers are versioned", () => {
  const offenders = [];
  for (const file of listGeneratedJs(GENERATED_DIR)) {
    const rel = path.relative(ROOT, file).replace(/\\/g, "/");
    const content = fs.readFileSync(file, "utf8");
    for (const match of content.matchAll(IMPORT_SPEC_RE)) {
      const specifier = match[1] || match[2];
      if (!specifier) {
        continue;
      }
      if (!specifier.startsWith("./") && !specifier.startsWith("../")) {
        continue;
      }
      if (!specifier.endsWith(".js") && !specifier.includes(".js?v=")) {
        continue;
      }
      if (!specifier.includes("?v=")) {
        offenders.push(`${rel}: ${specifier}`);
      }
    }
  }

  assert.deepEqual(
    offenders,
    [],
    `Found unversioned generated import specifiers:\n${offenders.join("\n")}`
  );
});

test("pma.js references streamUtils with a stamped version suffix", () => {
  const content = fs.readFileSync(
    path.join(GENERATED_DIR, "pma.js"),
    "utf8"
  );
  assert.match(
    content,
    /from "\.\/streamUtils\.js\?v=[0-9a-f]+"/,
    "PMA should import streamUtils through a versioned generated URL"
  );
});
