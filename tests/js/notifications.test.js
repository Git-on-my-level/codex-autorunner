import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <div id="toast"></div>
    <div id="notifications-modal">
      <div id="notifications-modal-body"></div>
      <button id="notifications-modal-close" type="button"></button>
    </div>
  </body></html>`,
  { url: "http://localhost/" }
);

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.Node = dom.window.Node;
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.localStorage = dom.window.localStorage;
globalThis.sessionStorage = dom.window.sessionStorage;

const { __notificationsTest } = await import(
  "../../src/codex_autorunner/static/generated/notifications.js"
);

test("browser-scope fallback stays runtime-scoped without localStorage", () => {
  const originalGetItem = globalThis.localStorage.getItem.bind(globalThis.localStorage);
  const originalSetItem = globalThis.localStorage.setItem.bind(globalThis.localStorage);
  globalThis.localStorage.getItem = () => {
    throw new Error("storage unavailable");
  };
  globalThis.localStorage.setItem = () => {
    throw new Error("storage unavailable");
  };

  try {
    const first = __notificationsTest.getHubHintScopeKey();
    const second = __notificationsTest.getHubHintScopeKey();

    assert.equal(first, second);
    assert.match(first, /^web:browser:/);
    assert.notEqual(first, "web:browser:unknown");
  } finally {
    globalThis.localStorage.getItem = originalGetItem;
    globalThis.localStorage.setItem = originalSetItem;
  }
});
