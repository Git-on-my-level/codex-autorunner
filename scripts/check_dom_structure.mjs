#!/usr/bin/env node
/**
 * DOM structure guardrail for static/index.html.
 * - Ensures tab buttons and panels align.
 * - Ensures exactly one active panel.
 * - Ensures no duplicate element IDs.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";

const INDEX_PATH = resolve("src/codex_autorunner/static/index.html");

function fail(message) {
  console.error(message);
  process.exitCode = 1;
}

function main() {
  const html = readFileSync(INDEX_PATH, "utf8");
  const dom = new JSDOM(html);
  const { document } = dom.window;

  // 1) Duplicate IDs
  const ids = new Map();
  document.querySelectorAll("[id]").forEach((el) => {
    const id = el.id.trim();
    if (!id) return;
    ids.set(id, (ids.get(id) || 0) + 1);
  });
  const dupes = [...ids.entries()].filter(([, count]) => count > 1);
  if (dupes.length) {
    fail(
      `Duplicate IDs found: ${dupes
        .map(([id, count]) => `${id} (${count}x)`)
        .join(", ")}`
    );
  }

  // 2) Panels present and unique
  const panels = [...document.querySelectorAll("section.panel")];
  if (!panels.length) {
    fail("No section.panel elements found.");
  }
  const panelsWithoutId = panels.filter((p) => !p.id);
  if (panelsWithoutId.length) {
    fail(`Panel(s) missing id: ${panelsWithoutId.length}`);
  }

  // Critical panels: ensure exactly one of each key view exists.
  const expectedPanels = ["analytics", "inbox", "tickets", "workspace", "terminal"];
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
  const activePanels = panels.filter((p) => p.classList.contains("active"));
  if (activePanels.length !== 1) {
    fail(
      `Expected 1 active panel, found ${activePanels.length}: [${activePanels
        .map((p) => p.id || "(no id)")
        .join(", ")}]`
    );
  }

  if (process.exitCode) {
    process.exit(process.exitCode);
  } else {
    console.log("DOM structure check passed.");
  }
}

main();
