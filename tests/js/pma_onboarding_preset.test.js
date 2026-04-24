import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

const ROOT = process.cwd();

function read(relPath) {
  return fs.readFileSync(path.join(ROOT, relPath), "utf8");
}

test("PMA onboarding keeps the standard title and moves guidance into chat/composer", () => {
  const appTs = read("src/codex_autorunner/static_src/app.ts");
  const pmaTs = read("src/codex_autorunner/static_src/pma.ts");

  assert.doesNotMatch(
    appTs,
    /Get started — ask the PM Agent to add your first repo/,
    "no onboarding-specific replacement title remains in app.ts"
  );
  assert.match(
    pmaTs,
    /parsePendingPromptPreset/,
    "PMA parses structured onboarding presets"
  );
  assert.match(
    pmaTs,
    /import \* as agentControlsModule from "\.\/agentControls\.js";/,
    "PMA namespace-imports agent controls so stale cached chunks cannot fail ESM linking"
  );
  assert.match(
    pmaTs,
    /pmaChat\.addAssistantMessage\(pending\.assistantIntro, true, \{[\s\S]*tag: "onboarding:intro"/,
    "PMA seeds an assistant onboarding intro message"
  );
  assert.match(
    pmaTs,
    /elements\.input\.value = pending\.prompt/,
    "PMA prefills the composer from the onboarding preset"
  );
});

test("direct PMA entry applies onboarding after empty repo count resolves", () => {
  const appTs = read("src/codex_autorunner/static_src/app.ts");

  assert.match(
    appTs,
    /const scheduled = scheduleOnboardingPromptIfFirstRun\(\);/,
    "app schedules onboarding when empty-state eligibility is confirmed"
  );
  assert.match(
    appTs,
    /if \(scheduled && requestedPMA\) \{\s*applyScheduledOnboardingPrompt\(\);/s,
    "app drains the scheduled onboarding preset when PMA is already open"
  );
  assert.match(
    appTs,
    /let latestRepoCount: number \| null = null;/,
    "app stores the latest hub repo count in case the event fires before PMA probing finishes"
  );
  assert.match(
    appTs,
    /if \(latestRepoCount !== null\) \{\s*handleRepoCount\(latestRepoCount\);/s,
    "app replays the latest repo count after PMA capability state is resolved"
  );
});
