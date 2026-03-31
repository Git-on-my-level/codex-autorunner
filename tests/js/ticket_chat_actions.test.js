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
