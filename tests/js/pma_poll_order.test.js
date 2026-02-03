import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

test("PMA starts polling for turn meta before awaiting chat response", () => {
  const filePath = path.join(
    process.cwd(),
    "src",
    "codex_autorunner",
    "static_src",
    "pma.ts"
  );
  const content = fs.readFileSync(filePath, "utf8");

  const fetchIndex = content.indexOf("const responsePromise = fetch(");
  const pollIndex = content.indexOf("void pollForTurnMeta", fetchIndex);
  const awaitIndex = content.indexOf("const res = await responsePromise", pollIndex);

  assert.ok(fetchIndex !== -1, "expected fetch promise initialization");
  assert.ok(pollIndex !== -1, "expected pollForTurnMeta call");
  assert.ok(awaitIndex !== -1, "expected await of response promise");
  assert.ok(
    fetchIndex < pollIndex && pollIndex < awaitIndex,
    "expected polling to start before awaiting the response"
  );
});
