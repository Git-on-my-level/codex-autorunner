import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(`<!doctype html><html><body></body></html>`, {
  url: "http://localhost/hub/",
});

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.HTMLInputElement = dom.window.HTMLInputElement;
globalThis.HTMLSelectElement = dom.window.HTMLSelectElement;
globalThis.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.localStorage = dom.window.localStorage;

const { __ticketEditorTest } = await import(
  "../../src/codex_autorunner/static/generated/ticketEditor.js"
);

test("ticket editor undo snapshots treat profile-only edits as distinct", () => {
  const original = {
    body: "Body",
    frontmatter: {
      agent: "hermes",
      done: false,
      ticketId: "tkt_profile_undo",
      title: "Demo",
      model: "",
      reasoning: "",
      profile: "m4-pma",
    },
  };
  const changedProfile = {
    body: "Body",
    frontmatter: {
      ...original.frontmatter,
      profile: "fast",
    },
  };

  assert.equal(__ticketEditorTest.sameUndoSnapshot(original, original), true);
  assert.equal(
    __ticketEditorTest.sameUndoSnapshot(original, changedProfile),
    false
  );
});
