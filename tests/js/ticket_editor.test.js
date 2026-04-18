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

test("ticket editor treats Hermes aliases as non-canonical agent options", () => {
  assert.equal(__ticketEditorTest.isHermesAliasAgentId("hermes"), false);
  assert.equal(__ticketEditorTest.isHermesAliasAgentId("hermes-m4-pma"), true);
  assert.equal(__ticketEditorTest.isHermesAliasAgentId("hermes_fast"), true);
});

test("undo snapshot treats body-only edits as distinct", () => {
  const original = {
    body: "Original body",
    frontmatter: {
      agent: "codex",
      done: false,
      ticketId: "tkt_body_undo",
      title: "Demo",
      model: "",
      reasoning: "",
      profile: "",
    },
  };
  const changedBody = {
    body: "Modified body content",
    frontmatter: { ...original.frontmatter },
  };

  assert.equal(__ticketEditorTest.sameUndoSnapshot(original, original), true);
  assert.equal(
    __ticketEditorTest.sameUndoSnapshot(original, changedBody),
    false
  );
});

test("undo snapshot treats title changes as distinct", () => {
  const original = {
    body: "Body",
    frontmatter: {
      agent: "codex",
      done: false,
      ticketId: "tkt_title_undo",
      title: "Old title",
      model: "",
      reasoning: "",
      profile: "",
    },
  };
  const changedTitle = {
    body: "Body",
    frontmatter: {
      ...original.frontmatter,
      title: "New title",
    },
  };

  assert.equal(
    __ticketEditorTest.sameUndoSnapshot(original, changedTitle),
    false
  );
});

test("undo snapshot treats model changes as distinct", () => {
  const original = {
    body: "Body",
    frontmatter: {
      agent: "codex",
      done: false,
      ticketId: "tkt_model_undo",
      title: "Demo",
      model: "gpt-4",
      reasoning: "",
      profile: "",
    },
  };
  const changedModel = {
    body: "Body",
    frontmatter: {
      ...original.frontmatter,
      model: "gpt-5",
    },
  };

  assert.equal(
    __ticketEditorTest.sameUndoSnapshot(original, changedModel),
    false
  );
});

test("undo snapshot treats reasoning changes as distinct", () => {
  const original = {
    body: "Body",
    frontmatter: {
      agent: "codex",
      done: false,
      ticketId: "tkt_reasoning_undo",
      title: "Demo",
      model: "",
      reasoning: "medium",
      profile: "",
    },
  };
  const changedReasoning = {
    body: "Body",
    frontmatter: {
      ...original.frontmatter,
      reasoning: "high",
    },
  };

  assert.equal(
    __ticketEditorTest.sameUndoSnapshot(original, changedReasoning),
    false
  );
});

test("undo snapshot treats agent changes as distinct", () => {
  const original = {
    body: "Body",
    frontmatter: {
      agent: "codex",
      done: false,
      ticketId: "tkt_agent_undo",
      title: "Demo",
      model: "",
      reasoning: "",
      profile: "",
    },
  };
  const changedAgent = {
    body: "Body",
    frontmatter: {
      ...original.frontmatter,
      agent: "opencode",
    },
  };

  assert.equal(
    __ticketEditorTest.sameUndoSnapshot(original, changedAgent),
    false
  );
});

test("undo snapshot treats done flag changes as distinct", () => {
  const original = {
    body: "Body",
    frontmatter: {
      agent: "codex",
      done: false,
      ticketId: "tkt_done_undo",
      title: "Demo",
      model: "",
      reasoning: "",
      profile: "",
    },
  };
  const changedDone = {
    body: "Body",
    frontmatter: {
      ...original.frontmatter,
      done: true,
    },
  };

  assert.equal(
    __ticketEditorTest.sameUndoSnapshot(original, changedDone),
    false
  );
});

test("undo snapshot returns false when previous is undefined", () => {
  const next = {
    body: "Body",
    frontmatter: {
      agent: "codex",
      done: false,
      ticketId: "tkt_undef",
      title: "Demo",
      model: "",
      reasoning: "",
      profile: "",
    },
  };

  assert.equal(__ticketEditorTest.sameUndoSnapshot(undefined, next), false);
});

test("Hermes alias detection rejects plain hermes id and non-hermes strings", () => {
  assert.equal(__ticketEditorTest.isHermesAliasAgentId("hermes"), false);
  assert.equal(__ticketEditorTest.isHermesAliasAgentId("codex"), false);
  assert.equal(__ticketEditorTest.isHermesAliasAgentId("opencode"), false);
  assert.equal(__ticketEditorTest.isHermesAliasAgentId(""), false);
});
