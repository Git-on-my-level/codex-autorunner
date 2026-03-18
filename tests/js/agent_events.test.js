import assert from "node:assert/strict";
import { test } from "node:test";

const { parseAppServerEvent } = await import(
  "../../src/codex_autorunner/static/generated/agentEvents.js"
);

test("buffers OpenCode assistant text parts until role resolution", () => {
  const partFirst = parseAppServerEvent({
    id: "part-1",
    received_at: 1000,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "part-text-1",
            messageID: "assistant-1",
            type: "text",
            text: "Working through the ticket",
          },
          delta: {
            text: "Working through the ticket",
          },
        },
      },
    },
  });

  assert.equal(partFirst, null);

  const roleUpdate = parseAppServerEvent({
    id: "role-1",
    received_at: 1001,
    message: {
      method: "message.updated",
      params: {
        properties: {
          info: {
            id: "assistant-1",
            role: "assistant",
          },
        },
      },
    },
  });

  assert.ok(roleUpdate);
  assert.equal(roleUpdate.event.kind, "output");
  assert.equal(roleUpdate.event.title, "Agent");
  assert.equal(roleUpdate.event.summary, "Working through the ticket");
  assert.equal(roleUpdate.event.itemId, "assistant-1");
});

test("ignores OpenCode user text parts after role resolution", () => {
  parseAppServerEvent({
    id: "role-user-1",
    received_at: 1000,
    message: {
      method: "message.updated",
      params: {
        properties: {
          info: {
            id: "user-1",
            role: "user",
          },
        },
      },
    },
  });

  const parsed = parseAppServerEvent({
    id: "user-part-1",
    received_at: 1001,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "part-user-1",
            messageID: "user-1",
            type: "text",
            text: "Please fix this",
          },
          delta: {
            text: "Please fix this",
          },
        },
      },
    },
  });

  assert.equal(parsed, null);
});

test("parses OpenCode reasoning and tool parts", () => {
  const reasoning = parseAppServerEvent({
    id: "reasoning-1",
    received_at: 1002,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "reason-1",
            type: "reasoning",
          },
          delta: {
            text: "Tracing the event pipeline",
          },
        },
      },
    },
  });

  assert.ok(reasoning);
  assert.equal(reasoning.event.kind, "thinking");
  assert.equal(reasoning.event.summary, "Tracing the event pipeline");
  assert.equal(reasoning.mergeStrategy, "append");

  const tool = parseAppServerEvent({
    id: "tool-1",
    received_at: 1003,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "tool-part-1",
            type: "tool",
            tool: "bash",
            input: "pwd",
            state: {
              status: "running",
            },
          },
        },
      },
    },
  });

  assert.ok(tool);
  assert.equal(tool.event.kind, "tool");
  assert.equal(tool.event.summary, "bash");
  assert.match(tool.event.detail, /pwd/);
  assert.match(tool.event.detail, /running/);
});
