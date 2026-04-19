import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <textarea id="ticket-chat-input"></textarea>
    <button id="ticket-chat-send"></button>
    <button id="ticket-chat-cancel"></button>
    <button id="ticket-chat-new-thread"></button>
    <div id="ticket-chat-status"></div>
    <div id="ticket-chat-error"></div>
    <div id="ticket-chat-stream"></div>
    <div id="ticket-chat-events"></div>
    <div id="ticket-chat-events-list"></div>
    <div id="ticket-chat-events-count"></div>
    <button id="ticket-chat-events-toggle"></button>
    <div id="ticket-chat-messages"></div>
    <select id="ticket-chat-agent-select"></select>
    <select id="ticket-chat-profile-select"></select>
    <select id="ticket-chat-model-select"></select>
    <input id="ticket-chat-model-input" type="text" />
    <select id="ticket-chat-reasoning-select"></select>
  </body></html>`,
  { url: "http://localhost/hub/" }
);

globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.HTMLInputElement = dom.window.HTMLInputElement;
globalThis.HTMLSelectElement = dom.window.HTMLSelectElement;
globalThis.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
globalThis.Node = dom.window.Node;
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.DOMParser = dom.window.DOMParser;
globalThis.localStorage = dom.window.localStorage;

const { __ticketChatActionsTest } = await import(
  "../../src/codex_autorunner/static/generated/ticketChatActions.js"
);

test("ticket chat preserves manual model override when the select is disabled", () => {
  localStorage.clear();
  localStorage.setItem("car.pma.agent.hermes.model", "hermes/stored-model");

  const modelSelect = document.getElementById("ticket-chat-model-select");
  const manualInput = document.getElementById("ticket-chat-model-input");

  modelSelect.innerHTML = `<option value="">Manual override</option>`;
  modelSelect.disabled = true;
  modelSelect.value = "";

  manualInput.value = "hermes/free-form-model";
  assert.equal(
    __ticketChatActionsTest.resolveTicketChatModel("hermes", {
      modelSelect,
      modelInput: manualInput,
    }),
    "hermes/free-form-model"
  );

  manualInput.value = "   ";
  assert.equal(
    __ticketChatActionsTest.resolveTicketChatModel("hermes", {
      modelSelect,
      modelInput: manualInput,
    }),
    "hermes/stored-model"
  );
});

test("ticket chat local keys are scoped by agent and profile", () => {
  assert.notEqual(
    __ticketChatActionsTest.buildScopedTicketChatTarget(
      7,
      "ticket-chat-key",
      "hermes",
      "m4-pma"
    ),
    __ticketChatActionsTest.buildScopedTicketChatTarget(
      7,
      "ticket-chat-key",
      "hermes",
      "fast"
    )
  );

  assert.notEqual(
    __ticketChatActionsTest.pendingKeyForTicket(7, "ticket-chat-key", "hermes", "m4-pma"),
    __ticketChatActionsTest.pendingKeyForTicket(7, "ticket-chat-key", "hermes", "fast")
  );
});

test("ticket chat can recover a pending turn even after the current selection changes", () => {
  localStorage.clear();
  const scopedTarget = __ticketChatActionsTest.buildScopedTicketChatTarget(
    7,
    "ticket-chat-key",
    "hermes",
    "m4-pma"
  );
  const pendingKey = `car.ticketChat.pending.${scopedTarget}`;
  localStorage.setItem(
    pendingKey,
    JSON.stringify({
      clientTurnId: "ticket-turn-1",
      message: "continue",
      startedAtMs: Date.now(),
      target: scopedTarget,
    })
  );

  const recovered = __ticketChatActionsTest.findPendingTicketTurn(
    7,
    "ticket-chat-key",
    "codex",
    ""
  );
  assert.ok(recovered);
  assert.equal(recovered.pendingKey, pendingKey);
  assert.equal(recovered.target, scopedTarget);
  assert.deepEqual(
    __ticketChatActionsTest.parseScopedTicketChatTarget(recovered.target),
    {
      baseTarget: "ticket-chat-key",
      agent: "hermes",
      profile: "m4-pma",
    }
  );
});

test("ticket chat applyTicketChatResult sets isStale from backend is_stale signal", () => {
  const state = __ticketChatActionsTest.getTicketChatState();
  __ticketChatActionsTest.applyTicketChatResult({
    status: "ok",
    has_draft: true,
    content: "updated content",
    patch: "--- a\n+++ b\n",
    agent_message: "done",
    created_at: "2025-01-01T00:00:00Z",
    base_hash: "abc123",
    is_stale: true,
  });
  assert.equal(state.draft.isStale, true);
  assert.equal(state.draft.content, "updated content");

  __ticketChatActionsTest.applyTicketChatResult({
    status: "ok",
    has_draft: true,
    content: "fresh content",
    patch: "",
    agent_message: "fresh",
    created_at: "2025-01-01T00:00:00Z",
    base_hash: "def456",
    is_stale: false,
  });
  assert.equal(state.draft.isStale, false);
  assert.equal(state.draft.content, "fresh content");
});
