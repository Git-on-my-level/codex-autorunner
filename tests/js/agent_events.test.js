import assert from "node:assert/strict";
import { test } from "node:test";

const { parseAppServerEvent, resetOpenCodeEventState } = await import(
  "../../src/codex_autorunner/static/generated/agentEvents.js"
);

test("buffers OpenCode assistant text parts until role resolution", () => {
  resetOpenCodeEventState();
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
          delta: "Working through the ticket",
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

  const continued = parseAppServerEvent({
    id: "part-2",
    received_at: 1002,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "part-text-2",
            messageID: "assistant-1",
            type: "text",
            text: "Working through the ticket still",
          },
          delta: " still",
        },
      },
    },
  });

  assert.ok(continued);
  assert.equal(continued.event.itemId, "assistant-1");
  assert.equal(continued.mergeStrategy, "append");
});

test("parses OpenCode message.part.delta events with direct ids", () => {
  resetOpenCodeEventState();
  parseAppServerEvent({
    id: "role-1",
    received_at: 1100,
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

  const parsed = parseAppServerEvent({
    id: "part-delta-1",
    received_at: 1101,
    message: {
      method: "message.part.delta",
      params: {
        properties: {
          partID: "part-text-1",
          messageID: "assistant-1",
          delta: " world",
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "output");
  assert.equal(parsed.event.itemId, "assistant-1");
  assert.equal(parsed.event.summary, " world");
  assert.equal(parsed.mergeStrategy, "append");
});

test("ignores OpenCode user text parts after role resolution", () => {
  resetOpenCodeEventState();
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
          delta: "Please fix this",
        },
      },
    },
  });

  assert.equal(parsed, null);
});

test("parses OpenCode reasoning and tool parts", () => {
  resetOpenCodeEventState();
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
          delta: "Tracing the event pipeline",
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

test("remembers OpenCode part types for delta-only reasoning events", () => {
  resetOpenCodeEventState();
  const first = parseAppServerEvent({
    id: "reasoning-typed",
    received_at: 1200,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "reason-1",
            type: "reasoning",
          },
          delta: "thinking",
        },
      },
    },
  });
  const second = parseAppServerEvent({
    id: "reasoning-delta",
    received_at: 1201,
    message: {
      method: "message.part.delta",
      params: {
        properties: {
          partID: "reason-1",
          delta: " more",
        },
      },
    },
  });

  assert.ok(first);
  assert.equal(first.event.kind, "thinking");
  assert.ok(second);
  assert.equal(second.event.kind, "thinking");
  assert.equal(second.event.summary, " more");
  assert.equal(second.mergeStrategy, "append");
});

test("ignores delta-only tool updates without part payload", () => {
  resetOpenCodeEventState();
  const first = parseAppServerEvent({
    id: "tool-typed",
    received_at: 1300,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "tool-1",
            type: "tool",
            tool: "bash",
          },
        },
      },
    },
  });

  const second = parseAppServerEvent({
    id: "tool-delta-only",
    received_at: 1301,
    message: {
      method: "message.part.delta",
      params: {
        properties: {
          partID: "tool-1",
          delta: "ignored tool delta",
        },
      },
    },
  });

  assert.ok(first);
  assert.equal(first.event.kind, "tool");
  assert.equal(second, null);
});

test("replaces cumulative OpenCode text snapshots when delta is absent", () => {
  resetOpenCodeEventState();

  parseAppServerEvent({
    id: "role-1",
    received_at: 1000,
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

  const snapshot = parseAppServerEvent({
    id: "part-1",
    received_at: 1001,
    message: {
      method: "message.part.updated",
      params: {
        properties: {
          part: {
            id: "part-text-1",
            messageID: "assistant-1",
            type: "text",
            text: "hello world",
          },
        },
      },
    },
  });

  assert.ok(snapshot);
  assert.equal(snapshot.event.itemId, "assistant-1");
  assert.equal(snapshot.event.summary, "hello world");
  assert.equal(snapshot.mergeStrategy, "replace");
});
test("parses compacted session.diff payloads as file events", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "diff-1",
    received_at: 1004,
    preview: "src/a.py, src/b.py, src/c.py, src/d.py +2 more files",
    message: {
      method: "session.diff",
      params: {
        message: "src/a.py, src/b.py, src/c.py, src/d.py +2 more files",
        properties: {
          diff_count: 6,
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "file");
  assert.equal(parsed.event.title, "File change");
  assert.equal(parsed.event.summary, "src/a.py, src/b.py, src/c.py, src/d.py +2 more files");
  assert.equal(parsed.event.detail, "6 file changes");
});

test("falls back from legacy diff updated statuses to semantic file text", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "diff-legacy",
    received_at: 1005,
    preview: "2 diff entries",
    message: {
      method: "session.diff",
      params: {
        status: "diff updated",
        properties: {
          diff_count: 2,
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "file");
  assert.equal(parsed.event.summary, "2 file changes");
  assert.equal(parsed.event.detail, "");
});

test("normalizes singular legacy diff entry preview when diff_count matches", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "diff-legacy-singular",
    received_at: 1005.05,
    preview: "1 diff entry",
    message: {
      method: "session.diff",
      params: {
        status: "diff updated",
        properties: {
          diff_count: 1,
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "file");
  assert.equal(parsed.event.summary, "1 file change");
  assert.equal(parsed.event.detail, "");
});

test("parses legacy diff entry previews without diff_count", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "diff-legacy-preview-only",
    received_at: 1005.1,
    preview: "7 diff entries",
    message: {
      method: "session.diff",
      params: {
        status: "diff updated",
        properties: {},
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "file");
  assert.equal(parsed.event.summary, "7 file changes");
  assert.equal(parsed.event.detail, "");
});

test("renders empty session.diff as zero file changes", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "diff-empty",
    received_at: 1005.25,
    preview: "0 file changes",
    message: {
      method: "session.diff",
      params: {
        message: "0 file changes",
        properties: {
          diff_count: 0,
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "file");
  assert.equal(parsed.event.summary, "0 file changes");
  assert.equal(parsed.event.detail, "");
});

test("prefers raw session.diff file paths over noisy legacy text", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "diff-raw-text",
    received_at: 1005.5,
    message: {
      method: "session.diff",
      params: {
        message: "raw diff payload that should not be shown",
        status: "diff updated",
        properties: {
          diff: [{ path: "src/alpha.ts" }, { path: "src/beta.ts" }],
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "file");
  assert.equal(parsed.event.summary, "src/alpha.ts, src/beta.ts");
  assert.equal(parsed.event.detail, "");
});

test("caps raw session.diff file extraction work for large payloads", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "diff-raw-large",
    received_at: 1006,
    message: {
      method: "session.diff",
      params: {
        properties: {
          diff: Array.from({ length: 50 }, (_, index) => ({ path: `src/${index}.ts` })),
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.summary, "src/0.ts, src/1.ts, src/2.ts, src/3.ts +46 more");
  assert.equal(parsed.event.detail, "50 file changes");
});

test("renders object-based status values without object coercion noise", () => {
  resetOpenCodeEventState();
  const parsed = parseAppServerEvent({
    id: "status-object",
    received_at: 2000,
    message: {
      method: "session.status",
      params: {
        status: {
          type: "busy",
        },
      },
    },
  });

  assert.ok(parsed);
  assert.equal(parsed.event.kind, "status");
  assert.equal(parsed.event.title, "Status");
  assert.equal(parsed.event.summary, "busy");
});
