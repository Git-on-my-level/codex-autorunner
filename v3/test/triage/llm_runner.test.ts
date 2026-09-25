/**
 * The real LlmRunner's pure parts: model-spec parsing and cost estimation.
 * The network path is never exercised — every triage test uses ScriptedLlm.
 */
import { describe, expect, test } from "bun:test";
import { MODEL_PRICES, createLlmRunner, estimateCostUsd, parseModelSpec } from "../../src/triage/llm.ts";

describe("parseModelSpec", () => {
  test("splits provider/model", () => {
    expect(parseModelSpec("anthropic/claude-haiku-4-5")).toEqual({
      provider: "anthropic",
      model: "claude-haiku-4-5",
    });
    expect(parseModelSpec("openai/gpt-5-mini")).toEqual({ provider: "openai", model: "gpt-5-mini" });
  });

  test("model names containing slashes keep them", () => {
    expect(parseModelSpec("openai/org/model-x").model).toBe("org/model-x");
  });

  test("a bare model name defaults to anthropic", () => {
    expect(parseModelSpec("claude-haiku-4-5")).toEqual({ provider: "anthropic", model: "claude-haiku-4-5" });
  });

  test("an unsupported provider is rejected loudly", () => {
    expect(() => parseModelSpec("ollama/llama3")).toThrow(/unsupported LLM provider/);
  });

  test("a missing model name is rejected", () => {
    expect(() => parseModelSpec("anthropic/")).toThrow(/missing model name/);
  });
});

describe("estimateCostUsd", () => {
  test("prices a known model by longest matching prefix", () => {
    // haiku: $1/Mtok in, $5/Mtok out
    expect(estimateCostUsd("anthropic/claude-haiku-4-5", 1_000_000, 0)).toBeCloseTo(1, 6);
    expect(estimateCostUsd("anthropic/claude-haiku-4-5", 0, 1_000_000)).toBeCloseTo(5, 6);
  });

  test("gpt-5-mini wins over the shorter gpt-5 prefix", () => {
    const mini = estimateCostUsd("openai/gpt-5-mini", 1_000_000, 0);
    expect(mini).toBeCloseTo(MODEL_PRICES["openai/gpt-5-mini"]!.in, 6);
    expect(mini).toBeLessThan(estimateCostUsd("openai/gpt-5", 1_000_000, 0));
  });

  test("an unknown model prices at zero rather than guessing", () => {
    expect(estimateCostUsd("anthropic/some-future-model", 1_000_000, 1_000_000)).toBe(0);
  });
});

describe("createLlmRunner", () => {
  test("constructing a runner needs no API key and makes no request", () => {
    const runner = createLlmRunner("anthropic/claude-haiku-4-5");
    expect(typeof runner.turn).toBe("function");
  });

  test("a bad provider fails at construction, not mid-incident", () => {
    expect(() => createLlmRunner("bedrock/claude")).toThrow();
  });
});
