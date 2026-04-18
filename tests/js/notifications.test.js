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

test("capability hint lookup prefers the matching repo", () => {
  const match = __notificationsTest.findCapabilityHint(
    [
      { itemType: "capability_hint", hintId: "voice_enablement", repoId: "repo-a" },
      { itemType: "capability_hint", hintId: "voice_enablement", repoId: "repo-b" },
    ],
    { hintId: "voice_enablement", repoId: "repo-b" }
  );

  assert.equal(match?.repoId, "repo-b");
});

test("capability hint returns first matching hint when multiple repos share the same hint id", () => {
  const match = __notificationsTest.findCapabilityHint(
    [
      { itemType: "capability_hint", hintId: "voice_enablement", repoId: "repo-a" },
      { itemType: "capability_hint", hintId: "voice_enablement", repoId: "repo-b" },
    ],
    { hintId: "voice_enablement", repoId: "repo-b" }
  );

  assert.ok(match);
  assert.equal(match.hintId, "voice_enablement");
});

test("capability hint returns undefined for empty hint list", () => {
  const match = __notificationsTest.findCapabilityHint(
    [],
    { hintId: "voice_enablement", repoId: "repo-a" }
  );

  assert.equal(match, undefined);
});

test("capability hint returns undefined when hint id does not match", () => {
  const match = __notificationsTest.findCapabilityHint(
    [
      { itemType: "capability_hint", hintId: "other_feature", repoId: "repo-a" },
    ],
    { hintId: "voice_enablement", repoId: "repo-a" }
  );

  assert.equal(match, undefined);
});

test("browser-scope key is stable across multiple calls in the same session", () => {
  const first = __notificationsTest.getHubHintScopeKey();
  const second = __notificationsTest.getHubHintScopeKey();
  const third = __notificationsTest.getHubHintScopeKey();

  assert.equal(first, second);
  assert.equal(second, third);
  assert.match(first, /^web:browser:/);
});

test("browser-scope key does not use unknown as identifier", () => {
  const key = __notificationsTest.getHubHintScopeKey();
  assert.notEqual(key, "web:browser:unknown");
  assert.match(key, /^web:browser:[^u]/);
});

test("browser-scope fallback remains stable when localStorage is intermittently unavailable", () => {
  const originalGetItem = globalThis.localStorage.getItem.bind(globalThis.localStorage);
  const originalSetItem = globalThis.localStorage.setItem.bind(globalThis.localStorage);

  globalThis.localStorage.getItem = () => {
    throw new Error("storage unavailable");
  };
  globalThis.localStorage.setItem = () => {
    throw new Error("storage unavailable");
  };

  try {
    const key1 = __notificationsTest.getHubHintScopeKey();

    globalThis.localStorage.getItem = originalGetItem;
    globalThis.localStorage.setItem = originalSetItem;

    const key2 = __notificationsTest.getHubHintScopeKey();

    globalThis.localStorage.getItem = () => {
      throw new Error("storage unavailable again");
    };
    globalThis.localStorage.setItem = () => {
      throw new Error("storage unavailable again");
    };

    const key3 = __notificationsTest.getHubHintScopeKey();

    assert.equal(key1, key3, "scope key should remain stable across storage availability changes");
  } finally {
    globalThis.localStorage.getItem = originalGetItem;
    globalThis.localStorage.setItem = originalSetItem;
  }
});
