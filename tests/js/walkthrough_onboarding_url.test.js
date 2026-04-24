import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

const ROOT = process.cwd();

test("walkthrough supports carOnboarding=1 query param for fresh onboarding QA", () => {
  const p = path.join(ROOT, "src/codex_autorunner/static_src/walkthrough.ts");
  const content = fs.readFileSync(p, "utf8");
  assert.match(content, /export const CAR_ONBOARDING_URL_PARAM/, "exports param name for docs/build checks");
  assert.match(content, /carOnboarding/, "param key matches URL convention");
  assert.match(content, /removeItem\(DISMISS_KEY\)/, "clears dismiss so strip can show");
  assert.match(content, /clearChatHistory\(PMA_LOCAL_CHAT, "pma"\)/, "clears locally persisted PMA chat for true empty slate");
  assert.match(content, /removeItem\(PMA_ONBOARDING_PRESET_KEY\)/, "clears pending PMA onboarding preset for clean run");
  assert.match(content, /searchParams\.delete/, "strips the param from the address bar");
});

test("walkthrough schedules a structured PMA onboarding preset", () => {
  const p = path.join(ROOT, "src/codex_autorunner/static_src/walkthrough.ts");
  const content = fs.readFileSync(p, "utf8");
  assert.match(content, /ONBOARDING_ASSISTANT_INTRO/, "defines assistant-side onboarding copy");
  assert.match(content, /setting up CAR on this machine/i, "defines the setup prefill prompt");
  assert.match(content, /assistantIntro: ONBOARDING_ASSISTANT_INTRO/, "stores assistant intro in the preset");
  assert.match(content, /prompt: ONBOARDING_PROMPT/, "stores user prompt in the preset");
  assert.match(content, /JSON\.stringify\(preset\)/, "serializes the structured onboarding preset into sessionStorage");
});
