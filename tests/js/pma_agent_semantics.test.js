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
    throw new Error("network disabled in PMA agent semantics tests");
  };
}

async function importPMA() {
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
  return await import(`${pmaUrl}?semantics=${Date.now()}-${Math.random()}`);
}

test("existing PMA thread locks the current agent for the next message", async () => {
  const dom = new JSDOM(`<!doctype html><html><body></body></html>`, {
    url: "http://localhost/hub/",
  });
  installDomGlobals(dom);
  const { __pmaTest } = await importPMA();

  const semantics = __pmaTest.resolvePMAAgentSemantics({
    threadInfo: {
      thread_id: "thr_123",
      agent: "hermes",
      profile: "m4-pma",
    },
    selectedAgent: "codex",
    selectedProfile: "",
  });

  assert.equal(semantics.hasExistingThread, true);
  assert.equal(semantics.agentForNextMessage, "hermes");
  assert.equal(semantics.profileForNextMessage, "m4-pma");
  assert.equal(semantics.agentSelectorLabel, "Agent for new thread");
  assert.equal(semantics.modelSelectorLabel, "Model for next message");
});

test("new PMA chat uses the selected agent default", async () => {
  const dom = new JSDOM(`<!doctype html><html><body></body></html>`, {
    url: "http://localhost/hub/",
  });
  installDomGlobals(dom);
  const { __pmaTest } = await importPMA();

  const semantics = __pmaTest.resolvePMAAgentSemantics({
    threadInfo: null,
    selectedAgent: "codex",
    selectedProfile: "",
  });

  assert.equal(semantics.hasExistingThread, false);
  assert.equal(semantics.agentForNextMessage, "codex");
  assert.equal(semantics.profileForNextMessage, "");
});

test("PMA selector labels and locked controls render honestly", async () => {
  const dom = new JSDOM(
    `<!doctype html><html><body>
      <label id="pma-chat-agent-select-label"></label>
      <select id="pma-chat-agent-select"></select>
      <select id="pma-chat-profile-select"><option value="m4-pma">m4-pma</option></select>
      <label id="pma-chat-model-select-label"></label>
      <select id="pma-chat-model-select"></select>
      <input id="pma-chat-model-input" />
      <span id="pma-thread-info-agent"></span>
      <button id="pma-chat-new-thread"></button>
    </body></html>`,
    { url: "http://localhost/hub/" }
  );
  installDomGlobals(dom);
  const { __pmaTest } = await importPMA();

  const semantics = __pmaTest.resolvePMAAgentSemantics({
    threadInfo: { thread_id: "thr_123", agent: "hermes" },
    selectedAgent: "codex",
    selectedProfile: "",
  });
  __pmaTest.applyPMAAgentSemantics(semantics);

  assert.equal(
    document.getElementById("pma-chat-agent-select-label").textContent,
    "Agent for new thread"
  );
  assert.equal(
    document.getElementById("pma-chat-model-select-label").textContent,
    "Model for next message"
  );
  assert.equal(document.getElementById("pma-chat-agent-select").disabled, true);
  assert.match(
    document.getElementById("pma-chat-agent-select").title,
    /locked/
  );
  assert.match(
    document.getElementById("pma-chat-new-thread").title,
    /choose the agent/
  );
});
