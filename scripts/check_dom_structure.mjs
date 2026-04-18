#!/usr/bin/env node
/**
 * DOM structure guardrail for static/index.html.
 * - Ensures tab buttons and panels align.
 * - Ensures exactly one active panel.
 * - Ensures no duplicate element IDs.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const INDEX_PATH = resolve("src/codex_autorunner/static/index.html");
const CONTEXTSPACE_JS_PATH = resolve("src/codex_autorunner/static/generated/contextspace.js");

function fail(message) {
  console.error(message);
  process.exitCode = 1;
}

function main() {
  const html = readFileSync(INDEX_PATH, "utf8");
  // 1) Duplicate IDs
  const ids = new Map();
  const idRegex = /\bid="([^"]+)"/g;
  let idMatch;
  while ((idMatch = idRegex.exec(html)) !== null) {
    const id = idMatch[1].trim();
    if (!id) return;
    ids.set(id, (ids.get(id) || 0) + 1);
  }
  const dupes = [...ids.entries()].filter(([, count]) => count > 1);
  if (dupes.length) {
    fail(
      `Duplicate IDs found: ${dupes
        .map(([id, count]) => `${id} (${count}x)`)
        .join(", ")}`
    );
  }

  // 2) Panels present and unique
  const sectionRegex = /<section\b([^>]*)>/gi;
  const panels = [];
  let sectionMatch;
  while ((sectionMatch = sectionRegex.exec(html)) !== null) {
    const attrs = sectionMatch[1];
    const classMatch = /\bclass="([^"]*)"/i.exec(attrs);
    const idMatchSection = /\bid="([^"]*)"/i.exec(attrs);
    const classList = classMatch ? classMatch[1].split(/\s+/).filter(Boolean) : [];
    if (!classList.includes("panel")) continue;
    panels.push({
      id: idMatchSection ? idMatchSection[1] : "",
      classList,
    });
  }
  if (!panels.length) {
    fail("No section.panel elements found.");
  }
  const panelsWithoutId = panels.filter((p) => !p.id);
  if (panelsWithoutId.length) {
    fail(`Panel(s) missing id: ${panelsWithoutId.length}`);
  }

  // Critical panels: ensure exactly one of each key view exists.
  const expectedPanels = ["analytics", "inbox", "tickets", "contextspace", "terminal"];
  for (const id of expectedPanels) {
    const matches = panels.filter((p) => p.id === id);
    if (!matches.length) {
      fail(`Missing panel #${id}`);
    }
    if (matches.length > 1) {
      fail(`Duplicate panel #${id} (${matches.length} found)`);
    }
  }

  // 3) Exactly one active panel
  const activePanels = panels.filter((p) => p.classList.includes("active"));
  if (activePanels.length !== 1) {
    fail(
      `Expected 1 active panel, found ${activePanels.length}: [${activePanels
        .map((p) => p.id || "(no id)")
        .join(", ")}]`
    );
  }

  // 4) Contextspace bootstrap guard should target #contextspace panel.
  const contextspaceJs = readFileSync(CONTEXTSPACE_JS_PATH, "utf8");
  if (contextspaceJs.includes('document.getElementById("workspace")')) {
    fail(
      'Contextspace bootstrap guard is checking "#workspace"; expected "#contextspace".'
    );
  }

  // 5) Shell containers: hub-shell, pma-shell, repo-shell must exist.
  const shellIds = ["hub-shell", "pma-shell", "repo-shell"];
  for (const shellId of shellIds) {
    if (!html.includes(`id="${shellId}"`)) {
      fail(`Missing shell container #${shellId}`);
    }
  }

  // 6) Hub and PMA mode toggle buttons must have data-hub-mode attributes.
  const modeButtons = html.match(/data-hub-mode="[^"]*"/g) || [];
  const modeValues = modeButtons.map((m) => m.match(/data-hub-mode="([^"]*)"/)[1]);
  const uniqueModes = new Set(modeValues);
  if (!uniqueModes.has("manual") || !uniqueModes.has("pma")) {
    fail(
      `Expected data-hub-mode="manual" and data-hub-mode="pma", found: ${[...uniqueModes].join(", ")}`
    );
  }
  if (modeButtons.length < 4) {
    fail(
      `Expected at least 4 mode toggle buttons (2 in hub, 2 in PMA), found ${modeButtons.length}`
    );
  }

  // 7) Boot loader element must exist for dismissBootLoader().
  if (!html.includes('id="car-boot-loader"')) {
    fail('Missing #car-boot-loader element needed by app.ts dismissBootLoader()');
  }

  // 8) Bootstrap and loader script tags must reference generated/ directory.
  if (!html.includes('src="static/generated/bootstrap.js')) {
    fail('Missing bootstrap script tag referencing static/generated/bootstrap.js');
  }
  if (!html.includes('src="static/generated/loader.js')) {
    fail('Missing loader script tag referencing static/generated/loader.js');
  }

  if (process.exitCode) {
    process.exit(process.exitCode);
  } else {
    console.log("DOM structure check passed.");
  }
}

main();
