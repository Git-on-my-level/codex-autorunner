import { describe, expect, test } from "bun:test";
import { FakeActionBus, FakeClock, memoryStore, testConfig } from "../fakes.ts";
import { authorizeTelegramUpdate } from "../../src/surfaces/telegram/bot.ts";
import type { HandlerDeps } from "../../src/surfaces/telegram/handlers.ts";
import { FakeMemoryWriter, testSafety } from "./helpers.ts";

function deps(overrides: Record<string, unknown> = {}): HandlerDeps {
  const store = memoryStore(new FakeClock());
  return {
    store,
    actions: new FakeActionBus(),
    memoryWriter: new FakeMemoryWriter(),
    safety: testSafety(store),
    host: "mac-studio",
    config: testConfig({
      telegram: { enabled: true, chat_id: "-100", allowed_user_ids: ["12345"], ...overrides },
    }),
  };
}

describe("Telegram bot-boundary authentication", () => {
  test("requires both the configured chat and explicit user identity", () => {
    const d = deps();
    expect(authorizeTelegramUpdate(d, "-100", 12345)).toBe(true);
    expect(authorizeTelegramUpdate(d, "-100", undefined)).toBe(false);
    expect(authorizeTelegramUpdate(d, "-100", 99999)).toBe(false);
    expect(authorizeTelegramUpdate(d, "-999", 12345)).toBe(false);
  });

  test("an empty chat or actor allowlist fails closed", () => {
    expect(authorizeTelegramUpdate(deps({ chat_id: "" }), "-100", 12345)).toBe(false);
    expect(authorizeTelegramUpdate(deps({ allowed_user_ids: [] }), "-100", 12345)).toBe(false);
  });
});
