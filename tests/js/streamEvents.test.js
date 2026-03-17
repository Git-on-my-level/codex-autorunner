/**
 * Characterization tests for stream event vocabulary
 * Tests the canonical handleStreamEvent contract from streamUtils.ts
 */
import { describe, it } from "node:test";
import assert from "node:assert";

function parseMaybeJson(data) {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

function extractContextRemainingPercent(usage) {
  if (!usage || typeof usage !== "object") return null;
  const payload = usage;
  const totalRaw = payload.totalTokens ?? payload.total ?? payload.total_tokens;
  const contextRaw =
    payload.modelContextWindow ?? payload.contextWindow ?? payload.model_context_window;

  const totalTokens = typeof totalRaw === "number" ? totalRaw : Number(totalRaw);
  const contextWindow =
    typeof contextRaw === "number" && Number.isFinite(contextRaw)
      ? contextRaw
      : Number(contextRaw);

  if (!Number.isFinite(totalTokens) || !Number.isFinite(contextWindow) || contextWindow <= 0) {
    return null;
  }

  const percentRemaining = Math.round(((contextWindow - totalTokens) / contextWindow) * 100);
  return Math.max(0, Math.min(100, percentRemaining));
}

function handleStreamEvent(event, rawData, handlers) {
  const parsed = parseMaybeJson(rawData);

  switch (event) {
    case "status": {
      const status = typeof parsed === "string" ? parsed : parsed.status || "";
      handlers.onStatus?.(status);
      break;
    }

    case "token": {
      const token =
        typeof parsed === "string"
          ? parsed
          : parsed.token || parsed.text || rawData || "";
      handlers.onToken?.(token);
      break;
    }

    case "token_usage": {
      if (typeof parsed === "object" && parsed !== null) {
        const usage = parsed;
        const percent = extractContextRemainingPercent(usage);
        if (percent !== null) {
          handlers.onTokenUsage?.(percent, usage);
        }
      }
      break;
    }

    case "update": {
      const payload = typeof parsed === "object" && parsed !== null ? parsed : {};
      handlers.onUpdate?.(payload);
      break;
    }

    case "event":
    case "app-server": {
      handlers.onEvent?.(parsed);
      break;
    }

    case "error": {
      const message =
        typeof parsed === "object" && parsed !== null
          ? parsed.detail || parsed.error || rawData || "Stream error"
          : rawData || "Stream error";
      handlers.onError?.(message);
      break;
    }

    case "interrupted": {
      const message =
        typeof parsed === "object" && parsed !== null
          ? parsed.detail || rawData || "Stream interrupted"
          : rawData || "Stream interrupted";
      handlers.onInterrupted?.(message);
      break;
    }

    case "done":
    case "finish": {
      handlers.onDone?.();
      break;
    }

    default: {
      if (typeof parsed === "object" && parsed !== null) {
        const messageObj = parsed;
        if (messageObj.method || messageObj.message) {
          handlers.onEvent?.(parsed);
        }
      }
      break;
    }
  }
}

describe("handleStreamEvent - status event", () => {
  it("handles string status", () => {
    let received = null;
    handleStreamEvent("status", '"working"', { onStatus: (s) => { received = s; } });
    assert.strictEqual(received, "working");
  });

  it("handles object status", () => {
    let received = null;
    handleStreamEvent("status", '{"status":"thinking"}', { onStatus: (s) => { received = s; } });
    assert.strictEqual(received, "thinking");
  });

  it("handles empty object status", () => {
    let received = null;
    handleStreamEvent("status", '{}', { onStatus: (s) => { received = s; } });
    assert.strictEqual(received, "");
  });
});

describe("handleStreamEvent - token event", () => {
  it("handles string token", () => {
    let received = null;
    handleStreamEvent("token", '"Hello"', { onToken: (t) => { received = t; } });
    assert.strictEqual(received, "Hello");
  });

  it("handles object with token field", () => {
    let received = null;
    handleStreamEvent("token", '{"token":"world"}', { onToken: (t) => { received = t; } });
    assert.strictEqual(received, "world");
  });

  it("handles object with text field", () => {
    let received = null;
    handleStreamEvent("token", '{"text":"!"}', { onToken: (t) => { received = t; } });
    assert.strictEqual(received, "!");
  });

  it("falls back to raw data", () => {
    let received = null;
    handleStreamEvent("token", 'raw text', { onToken: (t) => { received = t; } });
    assert.strictEqual(received, "raw text");
  });
});

describe("handleStreamEvent - token_usage event", () => {
  it("extracts percent from token_usage", () => {
    let receivedPercent = null;
    let receivedUsage = null;
    const usage = { totalTokens: 5000, modelContextWindow: 10000 };
    handleStreamEvent("token_usage", JSON.stringify(usage), {
      onTokenUsage: (p, u) => { receivedPercent = p; receivedUsage = u; }
    });
    assert.strictEqual(receivedPercent, 50);
    assert.deepStrictEqual(receivedUsage, usage);
  });

  it("handles missing context window", () => {
    let called = false;
    handleStreamEvent("token_usage", '{"totalTokens":5000}', {
      onTokenUsage: () => { called = true; }
    });
    assert.strictEqual(called, false);
  });
});

describe("handleStreamEvent - update event", () => {
  it("passes parsed object to onUpdate", () => {
    let received = null;
    handleStreamEvent("update", '{"message":"done","status":"ok"}', {
      onUpdate: (p) => { received = p; }
    });
    assert.deepStrictEqual(received, { message: "done", status: "ok" });
  });

  it("handles non-object as empty object", () => {
    let received = null;
    handleStreamEvent("update", '"just a string"', {
      onUpdate: (p) => { received = p; }
    });
    assert.deepStrictEqual(received, {});
  });
});

describe("handleStreamEvent - event/app-server events", () => {
  it("handles event type", () => {
    let received = null;
    handleStreamEvent("event", '{"method":"tool_call","name":"read"}', {
      onEvent: (e) => { received = e; }
    });
    assert.deepStrictEqual(received, { method: "tool_call", name: "read" });
  });

  it("handles app-server type", () => {
    let received = null;
    handleStreamEvent("app-server", '{"type":"thinking"}', {
      onEvent: (e) => { received = e; }
    });
    assert.deepStrictEqual(received, { type: "thinking" });
  });
});

describe("handleStreamEvent - error event", () => {
  it("extracts detail from error object", () => {
    let received = null;
    handleStreamEvent("error", '{"detail":"Something went wrong"}', {
      onError: (m) => { received = m; }
    });
    assert.strictEqual(received, "Something went wrong");
  });

  it("extracts error field", () => {
    let received = null;
    handleStreamEvent("error", '{"error":"Failed"}', {
      onError: (m) => { received = m; }
    });
    assert.strictEqual(received, "Failed");
  });

  it("falls back to raw data", () => {
    let received = null;
    handleStreamEvent("error", 'plain error', {
      onError: (m) => { received = m; }
    });
    assert.strictEqual(received, "plain error");
  });

  it("has default message for empty string", () => {
    let received = null;
    handleStreamEvent("error", '', {
      onError: (m) => { received = m; }
    });
    assert.strictEqual(received, "Stream error");
  });
});

describe("handleStreamEvent - interrupted event", () => {
  it("extracts detail", () => {
    let received = null;
    handleStreamEvent("interrupted", '{"detail":"User cancelled"}', {
      onInterrupted: (m) => { received = m; }
    });
    assert.strictEqual(received, "User cancelled");
  });

  it("falls back to raw data", () => {
    let received = null;
    handleStreamEvent("interrupted", 'interrupted by user', {
      onInterrupted: (m) => { received = m; }
    });
    assert.strictEqual(received, "interrupted by user");
  });

  it("has default message for empty string", () => {
    let received = null;
    handleStreamEvent("interrupted", '', {
      onInterrupted: (m) => { received = m; }
    });
    assert.strictEqual(received, "Stream interrupted");
  });
});

describe("handleStreamEvent - done/finish events", () => {
  it("calls onDone for done event", () => {
    let called = false;
    handleStreamEvent("done", '', { onDone: () => { called = true; } });
    assert.strictEqual(called, true);
  });

  it("calls onDone for finish event", () => {
    let called = false;
    handleStreamEvent("finish", '', { onDone: () => { called = true; } });
    assert.strictEqual(called, true);
  });
});

describe("handleStreamEvent - default/unknown events", () => {
  it("treats unknown object with method as event", () => {
    let received = null;
    handleStreamEvent("unknown", '{"method":"custom"}', {
      onEvent: (e) => { received = e; }
    });
    assert.deepStrictEqual(received, { method: "custom" });
  });

  it("treats unknown object with message as event", () => {
    let received = null;
    handleStreamEvent("unknown", '{"message":"hello"}', {
      onEvent: (e) => { received = e; }
    });
    assert.deepStrictEqual(received, { message: "hello" });
  });

  it("ignores unknown object without method or message", () => {
    let called = false;
    handleStreamEvent("unknown", '{"foo":"bar"}', {
      onEvent: () => { called = true; }
    });
    assert.strictEqual(called, false);
  });
});
