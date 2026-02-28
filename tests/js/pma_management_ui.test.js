import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

test("PMA memory view includes target and managed-thread controls", () => {
  const htmlPath = path.join(
    process.cwd(),
    "src",
    "codex_autorunner",
    "static",
    "index.html"
  );
  const html = fs.readFileSync(htmlPath, "utf8");

  [
    "pma-target-ref",
    "pma-targets-add",
    "pma-targets-list",
    "pma-targets-clear",
    "pma-managed-threads-list",
    "pma-managed-thread-detail",
    "pma-managed-thread-archive",
    "pma-managed-thread-resume",
  ].forEach((id) => {
    assert.ok(html.includes(`id=\"${id}\"`), `expected control ${id} in PMA HTML`);
  });
});

test("PMA UI calls REST endpoints for targets and managed-thread actions", () => {
  const tsPath = path.join(
    process.cwd(),
    "src",
    "codex_autorunner",
    "static_src",
    "pma.ts"
  );
  const content = fs.readFileSync(tsPath, "utf8");

  [
    /api\(\"\/hub\/pma\/targets\",[\s\S]*method:\s*\"GET\"/,
    /api\(\"\/hub\/pma\/targets\",[\s\S]*method:\s*\"POST\"/,
    /api\(`\/hub\/pma\/targets\/\$\{encodeURIComponent\(targetKey\)\}`,[\s\S]*method:\s*\"DELETE\"/,
    /api\(\"\/hub\/pma\/targets\",[\s\S]*method:\s*\"DELETE\"/,
    /api\(\"\/hub\/pma\/threads\?limit=50\",[\s\S]*method:\s*\"GET\"/,
    /\/hub\/pma\/threads\/\$\{encodeURIComponent\(managedThreadId\)\}/,
    /\/hub\/pma\/threads\/\$\{encodeURIComponent\(selectedManagedThreadId\)\}\/archive/,
    /\/hub\/pma\/threads\/\$\{encodeURIComponent\(selectedManagedThreadId\)\}\/resume/,
  ].forEach((pattern) => {
    assert.match(content, pattern);
  });
});
