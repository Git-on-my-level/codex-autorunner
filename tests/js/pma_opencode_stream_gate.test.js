import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

test("PMA does not start live turn streams for OpenCode", () => {
  const filePath = path.join(
    process.cwd(),
    "src",
    "codex_autorunner",
    "static_src",
    "pma.ts"
  );
  const content = fs.readFileSync(filePath, "utf8");

  assert.match(
    content,
    /if \(\(meta\.agent \|\| "codex"\)\.trim\(\)\.toLowerCase\(\) === "opencode"\) return;/
  );
  assert.match(
    content,
    /threadId && turnId && agent\.trim\(\)\.toLowerCase\(\) !== "opencode"/
  );
});
