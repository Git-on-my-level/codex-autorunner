/**
 * The real {@link LlmRunner}: one bounded turn against a provider through the
 * Vercel AI SDK. Triage never imports this directly — it goes through the
 * LlmRunner port, so every test runs against ScriptedLlm at $0 and no network.
 *
 * Model strings are "provider/model" (see config.providers.triage), e.g.
 * "anthropic/claude-haiku-4-5" or "openai/gpt-5-mini".
 */
import { generateText, jsonSchema, tool as aiTool, type ToolSet } from "ai";
import { createAnthropic } from "@ai-sdk/anthropic";
import { createOpenAI } from "@ai-sdk/openai";
import type { LlmRunner, LlmTurnResult } from "../ports.ts";

export type ProviderId = "anthropic" | "openai";

export interface ModelSpec {
  provider: ProviderId;
  model: string;
}

/** "anthropic/claude-haiku-4-5" → {provider:"anthropic", model:"claude-haiku-4-5"}. */
export function parseModelSpec(spec: string): ModelSpec {
  const slash = spec.indexOf("/");
  if (slash <= 0) {
    // Bare model names default to Anthropic (the configured triage provider).
    return { provider: "anthropic", model: spec };
  }
  const provider = spec.slice(0, slash);
  const model = spec.slice(slash + 1);
  if (provider !== "anthropic" && provider !== "openai") {
    throw new Error(`unsupported LLM provider "${provider}" in "${spec}" (expected anthropic/ or openai/)`);
  }
  if (!model) throw new Error(`missing model name in "${spec}"`);
  return { provider, model };
}

/**
 * USD per million tokens, longest-prefix match. Approximate on purpose: this
 * drives the daily budget rail, which is meant to be conservative, not exact.
 * Unknown models price at 0 and are audited by the caller as unpriced.
 */
export const MODEL_PRICES: Record<string, { in: number; out: number }> = {
  "anthropic/claude-haiku": { in: 1, out: 5 },
  "anthropic/claude-sonnet": { in: 3, out: 15 },
  "anthropic/claude-opus": { in: 15, out: 75 },
  "openai/gpt-5-mini": { in: 0.25, out: 2 },
  "openai/gpt-5": { in: 1.25, out: 10 },
  "openai/gpt-4o-mini": { in: 0.15, out: 0.6 },
  "openai/gpt-4o": { in: 2.5, out: 10 },
};

export function estimateCostUsd(qualifiedModel: string, tokensIn: number, tokensOut: number): number {
  let best: { in: number; out: number } | null = null;
  let bestLen = -1;
  for (const [prefix, price] of Object.entries(MODEL_PRICES)) {
    if (qualifiedModel.startsWith(prefix) && prefix.length > bestLen) {
      best = price;
      bestLen = prefix.length;
    }
  }
  if (!best) return 0;
  return (tokensIn / 1e6) * best.in + (tokensOut / 1e6) * best.out;
}

export interface LlmRunnerOptions {
  /** Wall-clock cap for one turn (DESIGN §5: ~60s per run). */
  timeoutMs?: number;
  /** Output token cap per turn. */
  maxOutputTokens?: number;
}

/**
 * Build the production runner. Provider clients read their API keys from the
 * environment (ANTHROPIC_API_KEY / OPENAI_API_KEY) and are constructed lazily,
 * so importing this module never requires credentials.
 */
export function createLlmRunner(spec: string, opts: LlmRunnerOptions = {}): LlmRunner {
  const { provider, model } = parseModelSpec(spec);
  const qualified = `${provider}/${model}`;
  const timeoutMs = opts.timeoutMs ?? 60_000;

  const resolveModel = () => {
    if (provider === "anthropic") return createAnthropic({})(model);
    return createOpenAI({})(model);
  };

  return {
    async turn(input): Promise<LlmTurnResult> {
      const tools: ToolSet = {};
      for (const t of input.tools) {
        // No `execute`: the AI SDK returns the tool call and CAR's own executor
        // runs it, so policy is enforced outside the model loop.
        tools[t.name] = aiTool({
          description: t.description,
          inputSchema: jsonSchema(t.schema as never),
        });
      }

      const signal = AbortSignal.timeout(timeoutMs);
      const result = await generateText({
        model: resolveModel(),
        system: input.system,
        messages: input.messages.map((m) => ({
          // The port models tool output as plain text; feed it back as user turns
          // so the seam stays provider-agnostic.
          role: m.role === "assistant" ? ("assistant" as const) : ("user" as const),
          content: m.content,
        })),
        tools,
        toolChoice: "required",
        maxOutputTokens: opts.maxOutputTokens ?? 2048,
        abortSignal: signal,
      });

      const tokensIn = result.usage.inputTokens ?? 0;
      const tokensOut = result.usage.outputTokens ?? 0;
      return {
        toolCalls: result.toolCalls.map((c) => ({
          tool: c.toolName,
          args: (c.input ?? {}) as Record<string, unknown>,
        })),
        tokensIn,
        tokensOut,
        costUsd: estimateCostUsd(qualified, tokensIn, tokensOut),
        model: qualified,
      };
    },
  };
}
