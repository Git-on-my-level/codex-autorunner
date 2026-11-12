import { afterEach, describe, expect, test } from "bun:test";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { createActionBus } from "../../src/actions/index.ts";
import { createSafetyKernel } from "../../src/safety/index.ts";
import { FakeClock, memoryStore } from "../fakes.ts";
import { makeRunner, StubPolicy, tempState } from "./helpers.ts";

const cleanups: (() => void)[] = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

const TEMPLATES = `
[templates."safe.echo"]
argv = ["printf", "{text}"]
mutating = true
[templates."safe.echo".args]
text = { type = "string", required = true, max_len = 64 }
`;

describe("ActionBus v3 safety seam", () => {
  test("a template only executes after a core grant and records a terminal effect", async () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    writeFileSync(join(state.dir, "templates.toml"), TEMPLATES);
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const kernel = createSafetyKernel({ clock });
    const args = { template_id: "safe.echo", text: "hello" };
    const scope = { vendor: "test" };
    const lineage = { source_id: "test", request_id: "decision-safe" };
    const g = kernel.createGrant({
      intent_id: "grant-safe",
      scope,
      lineage,
      effect_type: "run_template",
      constraints: { args, action_class: "exec.safe.echo" },
      uses_remaining: 1,
    });
    const runner = makeRunner(() => ({ code: 0, stdout: "hello" }));
    const bus = createActionBus(store, state.config, new StubPolicy(), { runner: runner.runner, env: {}, safety: kernel });
    const result = await bus.runTemplate("safe.echo", { text: "hello" }, {
      decisionId: "decision-safe",
      mutating: true,
      grantId: g.id,
      intentId: "effect-safe",
      scope,
      lineage,
    });
    expect(result.ok).toBe(true);
    expect(runner.calls).toHaveLength(1);
    expect(kernel.ledger.getEffect("effect-safe")?.state).toBe("terminal_recorded");
    expect(kernel.ledger.getEffect("effect-safe")?.terminal_outcome).toBe("ok");
  });

  test("without a grant the compatibility ActionBus never reaches the runner", async () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    writeFileSync(join(state.dir, "templates.toml"), TEMPLATES);
    const runner = makeRunner();
    const kernel = createSafetyKernel();
    const bus = createActionBus(memoryStore(), state.config, new StubPolicy(), { runner: runner.runner, env: {}, safety: kernel });
    const result = await bus.runTemplate("safe.echo", { text: "hello" }, { decisionId: "decision-no-grant", mutating: true });
    expect(result.ok).toBe(false);
    expect(result.output).toContain("grant_required");
    expect(runner.calls).toHaveLength(0);
  });
});
