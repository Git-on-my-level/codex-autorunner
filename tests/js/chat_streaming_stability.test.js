import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  `<!doctype html><html><body>
    <textarea id="test-chat-input"></textarea>
    <button id="test-chat-send"></button>
    <button id="test-chat-cancel"></button>
    <button id="test-chat-new-thread"></button>
    <div id="test-chat-status"></div>
    <div id="test-chat-error"></div>
    <div id="test-chat-stream"></div>
    <div id="test-chat-events"></div>
    <div id="test-chat-events-list"></div>
    <div id="test-chat-events-count"></div>
    <button id="test-chat-events-toggle"></button>
    <div id="test-chat-messages"></div>
  </body></html>`,
  { url: "http://localhost/" }
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

const { createDocChat } = await import(
  "../../src/codex_autorunner/static/generated/docChatCore.js"
);

function makeConfig() {
  return {
    idPrefix: "test-chat",
    limits: { eventVisible: 20, eventMax: 50 },
    styling: {
      eventClass: "chat-event",
      eventTitleClass: "chat-event-title",
      eventSummaryClass: "chat-event-summary",
      eventDetailClass: "chat-event-detail",
      eventMetaClass: "chat-event-meta",
      eventsEmptyClass: "chat-events-empty",
      messagesClass: "chat-message",
      messageRoleClass: "chat-message-role",
      messageContentClass: "chat-message-content",
      messageMetaClass: "chat-message-meta",
      messageUserClass: "chat-message-user",
      messageAssistantClass: "chat-message-assistant",
      messageAssistantThinkingClass: "chat-message-assistant-thinking",
      messageAssistantFinalClass: "chat-message-assistant-final",
    },
  };
}

test("streaming token updates do not rebuild existing messages", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("Hello");
  chat.state.status = "running";
  chat.state.streamText = "";
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  const childCountAfterFirstRender = messagesEl.children.length;
  assert.ok(childCountAfterFirstRender >= 1, "should have at least the user message");

  const userMsgEl = messagesEl.querySelector(".chat-message-user");
  assert.ok(userMsgEl, "user message should exist");
  const userMsgIdentity = userMsgEl.textContent;

  chat.state.streamText = "Token 1";
  chat.render();

  const userMsgAfterToken = messagesEl.querySelector(".chat-message-user");
  assert.ok(userMsgAfterToken, "user message should still exist after token");
  assert.equal(
    userMsgAfterToken.textContent,
    userMsgIdentity,
    "user message DOM should be preserved across token updates"
  );
  assert.equal(
    userMsgAfterToken,
    userMsgEl,
    "user message element should be the same DOM node (not rebuilt)"
  );
});

test("multiple streaming tokens preserve stable message count and identity", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("First");
  chat.addAssistantMessage("Previous response", true);
  chat.state.status = "running";
  chat.state.streamText = "";
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  const stableChildren = Array.from(messagesEl.querySelectorAll(".chat-message-user, .chat-message-assistant-final"));

  assert.equal(stableChildren.length, 2, "should have user + previous assistant message");

  for (let i = 1; i <= 10; i++) {
    chat.state.streamText = `Token ${i}`;
    chat.render();
  }

  const stableAfterStreaming = Array.from(
    messagesEl.querySelectorAll(".chat-message-user, .chat-message-assistant-final")
  );
  assert.equal(stableAfterStreaming.length, 2, "stable messages should not multiply");

  assert.equal(
    stableAfterStreaming[0],
    stableChildren[0],
    "user message DOM identity preserved across 10 token renders"
  );
  assert.equal(
    stableAfterStreaming[1],
    stableChildren[1],
    "previous assistant message DOM identity preserved across 10 token renders"
  );
});

test("streaming bubble shows rendered markdown content", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("Write code");
  chat.state.status = "running";
  chat.state.streamText = "Here is code:\n```\nprint('hello')\n```";
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  const streamingEl = messagesEl.querySelector(".chat-message-assistant-thinking");
  assert.ok(streamingEl, "streaming bubble should exist");

  const contentEl = streamingEl.querySelector(".chat-message-content");
  assert.ok(contentEl, "streaming content element should exist");
  assert.match(contentEl.innerHTML, /<pre class="md-code">/, "code block should be rendered");
});

test("streaming bubble content updates without full DOM rebuild", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("Go");
  chat.state.status = "running";
  chat.state.streamText = "Hello";
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  const streamingEl = messagesEl.querySelector(".chat-message-assistant-thinking");
  assert.ok(streamingEl);
  const streamingIdentity = streamingEl;

  chat.state.streamText = "Hello world, this is a longer response";
  chat.render();

  const streamingAfter = messagesEl.querySelector(".chat-message-assistant-thinking");
  assert.ok(streamingAfter);
  assert.equal(
    streamingAfter,
    streamingIdentity,
    "streaming bubble should be same DOM node, not rebuilt"
  );

  const contentEl = streamingAfter.querySelector(".chat-message-content");
  assert.ok(contentEl);
  assert.match(contentEl.innerHTML, /Hello world/, "streaming content should show updated text");
});

test("adding a new assistant message triggers correct rebuild", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("Ask");
  chat.state.status = "running";
  chat.state.streamText = "Streaming...";
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  assert.ok(messagesEl.querySelector(".chat-message-assistant-thinking"), "should have streaming bubble");

  chat.state.status = "done";
  chat.state.streamText = "";
  chat.addAssistantMessage("Final answer", true);
  chat.render();

  assert.ok(
    !messagesEl.querySelector(".chat-message-assistant-thinking"),
    "streaming bubble should be gone after final message"
  );
  const finalEl = messagesEl.querySelector(".chat-message-assistant-final");
  assert.ok(finalEl, "final assistant message should exist");
  assert.match(finalEl.textContent, /Final answer/);
});

test("user message markdown renders code blocks and bullets", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("Here is a list:\n- item one\n- item two\n\n```\ncode\n```");
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  const userContent = messagesEl.querySelector(".chat-message-user .chat-message-content");
  assert.ok(userContent);
  assert.match(userContent.innerHTML, /<ul>/, "bullet list should render");
  assert.match(userContent.innerHTML, /<pre class="md-code">/, "code block should render");
});

test("user message markdown renders inline code and bold", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("Use `npm install` to add **dependencies**");
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  const userContent = messagesEl.querySelector(".chat-message-user .chat-message-content");
  assert.ok(userContent);
  assert.match(userContent.innerHTML, /<code>npm install<\/code>/, "inline code should render");
  assert.match(userContent.innerHTML, /<strong>dependencies<\/strong>/, "bold should render");
});

test("thread reset clears all DOM and resets incremental state", () => {
  const chat = createDocChat(makeConfig());
  chat.setTarget("test");
  chat.addUserMessage("Before reset");
  chat.state.status = "running";
  chat.state.streamText = "Streaming...";
  chat.render();

  const messagesEl = document.getElementById("test-chat-messages");
  assert.ok(messagesEl.children.length > 0, "should have content before reset");

  chat.state.messages = [];
  chat.state.streamText = "";
  chat.state.status = "idle";
  chat.clearEvents();
  chat.render();

  const userMsgs = messagesEl.querySelectorAll(".chat-message-user");
  const assistantMsgs = messagesEl.querySelectorAll(".chat-message-assistant");
  assert.equal(userMsgs.length, 0, "user messages should be cleared after reset");
  assert.equal(assistantMsgs.length, 0, "assistant messages should be cleared after reset");
});
