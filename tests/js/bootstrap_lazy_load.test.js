import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

const ROOT = process.cwd();

test("bootstrap.ts strips token from URL and stores in sessionStorage", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "bootstrap.ts"),
    "utf8"
  );

  assert.match(content, /params\.delete\("token"\)/, "should strip token param");
  assert.match(content, /sessionStorage\.setItem/, "should store token in sessionStorage");
  assert.match(content, /history\.replaceState/, "should replace URL to hide token");
});

test("bootstrap.ts detects base prefix from known path prefixes", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "bootstrap.ts"),
    "utf8"
  );

  const prefixes = ["/repos/", "/hub/", "/api/", "/static/", "/cat/"];
  for (const prefix of prefixes) {
    assert.ok(content.includes(prefix), `expected ${prefix} in prefix detection`);
  }
});

test("bootstrap.ts normalizes trailing slashes in base prefix", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "bootstrap.ts"),
    "utf8"
  );

  assert.match(content, /endsWith\("\/"\)/, "should check for trailing slashes");
  assert.match(content, /slice\(0, -1\)/, "should strip trailing slashes");
});

test("bootstrap.ts reads asset version from query param or script src", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "bootstrap.ts"),
    "utf8"
  );

  assert.match(content, /__assetSuffix/, "should set __assetSuffix");
  assert.match(content, /querySelector.*data-car-bootstrap/, "should read bootstrap script src");
});

test("bootstrap.ts adds stylesheet links with version suffix", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "bootstrap.ts"),
    "utf8"
  );

  assert.match(content, /addStylesheet.*styles\.css/, "should add main stylesheet");
  assert.match(content, /addStylesheet.*xterm\.css/, "should add xterm vendor stylesheet");
});

test("bootstrap.ts polls for version changes in development", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "bootstrap.ts"),
    "utf8"
  );

  assert.match(content, /localhost/, "should check for localhost");
  assert.match(content, /setInterval/, "should poll for version changes");
  assert.match(content, /window\.location\.replace/, "should reload on version change");
});

test("bootstrap.ts skips version checks when uiMock is present (screenshot / QA mode)", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "bootstrap.ts"),
    "utf8"
  );
  assert.match(content, /skipVersionProbe.*uiMock/s, "should detect uiMock query param");
  assert.match(content, /!skipVersionProbe/, "should gate checkVersion and dev polling on uiMock");
});

test("app.ts uses dynamic import() for hub module", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  assert.match(
    content,
    /importVersionedModule<typeof import\("\.\/hub\.js"\)>\(\s*"\.\/hub\.js"/,
    "should lazy-import hub.js with asset versioning"
  );
});

test("app.ts uses dynamic import() for PMA module", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  assert.match(
    content,
    /importVersionedModule<typeof import\("\.\/pma\.js"\)>\(\s*"\.\/pma\.js"/,
    "should lazy-import pma.js with asset versioning"
  );
});

test("app.ts uses dynamic import() for repo shell modules", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  assert.match(content, /importVersionedModule<typeof import\("\.\/archive\.js"\)>/, "should lazy-import archive.js with asset versioning");
  assert.match(content, /importVersionedModule<typeof import\("\.\/tickets\.js"\)>/, "should lazy-import tickets.js with asset versioning");
  assert.match(content, /importVersionedModule<typeof import\("\.\/terminal\.js"\)>/, "should lazy-import terminal.js with asset versioning");
  assert.match(content, /importVersionedModule<typeof import\("\.\/messages\.js"\)>/, "should lazy-import messages.js with asset versioning");
  assert.match(content, /importVersionedModule<typeof import\("\.\/liveUpdates\.js"\)>/, "should lazy-import liveUpdates.js with asset versioning");
});

test("app.ts deduplicates module loading with promise caching", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  assert.match(content, /\?\?\s*=\s*importVersionedModule<typeof import\("\.\/hub\.js"\)>/, "hub module should use nullish coalescing for dedup");
  assert.match(content, /\?\?\s*=\s*importVersionedModule<typeof import\("\.\/pma\.js"\)>/, "pma module should use nullish coalescing for dedup");
});

test("assetLoader.ts appends bootstrap asset suffix to dynamic imports", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "assetLoader.ts"),
    "utf8"
  );

  assert.match(content, /__assetSuffix/, "should read bootstrap asset suffix");
  assert.match(
    content,
    /import\(`\$\{path\}\$\{getAssetSuffix\(\)\}`\)/,
    "should append suffix to dynamic imports"
  );
});

test("app.ts probes PMA availability before showing PMA mode", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  assert.match(content, /probePMAEnabled/, "should have PMA probe function");
  assert.match(content, /\/hub\/pma\/agents/, "should probe /hub/pma/agents endpoint");
});

test("app.ts disables PMA toggle when PMA is not available", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  assert.match(content, /btn\.disabled\s*=\s*true/, "should disable PMA button when unavailable");
  assert.match(content, /aria-disabled/, "should set aria-disabled");
});

test("app.ts selects hub vs repo shell based on REPO_ID", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  const hubShell = content.indexOf("async function initHubShell");
  assert.ok(hubShell !== -1, "should have initHubShell");
  assert.match(content, /consumeOnboardingUrlReset\(\)/, "should run onboarding reset before PMA init");
  const firstLineAfterInit = content.indexOf("async function initHubShell");
  const afterBrace = content.indexOf("{", firstLineAfterInit);
  const window = content.slice(afterBrace, afterBrace + 200);
  assert.match(window, /consumeOnboardingUrlReset\(\)/, "consumeOnboarding should be first in initHubShell body");

  const bootstrapIndex = content.indexOf("function bootstrap()");
  assert.ok(bootstrapIndex !== -1, "should have bootstrap function");
  assert.match(content, /initUiMockFromUrl/, "should init ui mock from URL for hub screenshot scenarios");

  const repoCheck = content.indexOf("if (!REPO_ID)", bootstrapIndex);
  assert.ok(repoCheck !== -1, "should check REPO_ID to select shell");

  assert.ok(
    content.indexOf("initHubShell", repoCheck) !== -1,
    "should init hub shell when no REPO_ID"
  );
  assert.ok(
    content.indexOf("initRepoShell", repoCheck) !== -1,
    "should init repo shell when REPO_ID present"
  );
});

test("app.ts dismisses boot loader element on startup", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "app.ts"),
    "utf8"
  );

  assert.match(content, /car-boot-loader/, "should reference boot loader element");
  assert.match(content, /\.remove\(\)/, "should remove boot loader element");
});

test("env.ts reads REPO_ID and BASE_PATH from bootstrap globals", () => {
  const content = fs.readFileSync(
    path.join(ROOT, "src", "codex_autorunner", "static_src", "env.ts"),
    "utf8"
  );

  assert.match(content, /__CAR_BASE_PREFIX/, "should read base prefix from bootstrap");
  assert.match(content, /__CAR_REPO_ID/, "should read repo id from bootstrap");
  assert.match(content, /__CAR_BASE_PATH/, "should read base path from bootstrap");
  assert.match(content, /export const REPO_ID/, "should export REPO_ID");
  assert.match(content, /export const BASE_PATH/, "should export BASE_PATH");
});
