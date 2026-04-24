import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import path from "node:path";
import { pathToFileURL } from "node:url";

function installDomGlobals(dom) {
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    writable: true,
    value: dom.window.navigator,
  });
  globalThis.HTMLElement = dom.window.HTMLElement;
  globalThis.HTMLInputElement = dom.window.HTMLInputElement;
  globalThis.HTMLSelectElement = dom.window.HTMLSelectElement;
  globalThis.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
  globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
  globalThis.HTMLDivElement = dom.window.HTMLDivElement;
  globalThis.Node = dom.window.Node;
  globalThis.Event = dom.window.Event;
  globalThis.CustomEvent = dom.window.CustomEvent;
  globalThis.DOMParser = dom.window.DOMParser;
  globalThis.localStorage = dom.window.localStorage;
  globalThis.sessionStorage = dom.window.sessionStorage;
  globalThis.history = dom.window.history;
  globalThis.location = dom.window.location;
  globalThis.MutationObserver = dom.window.MutationObserver;
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  globalThis.fetch = async () => {
    throw new Error("network disabled in pma import smoke test");
  };
}

test("generated pma module imports without ESM linking errors", async () => {
  const dom = new JSDOM(`<!doctype html><html><body></body></html>`, {
    url: "http://127.0.0.1:4173/?uiMock=onboarding&view=pma&carOnboarding=1",
  });
  installDomGlobals(dom);

  const pmaUrl = pathToFileURL(
    path.join(
      process.cwd(),
      "src",
      "codex_autorunner",
      "static",
      "generated",
      "pma.js"
    )
  ).href;

  const mod = await import(`${pmaUrl}?smoke=${Date.now()}`);

  assert.ok(mod, "expected PMA module namespace to load");
  assert.equal(typeof mod.initPMA, "function");
});
