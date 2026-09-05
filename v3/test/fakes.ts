/**
 * Shared test fakes. Workstreams code against these instead of live services.
 */
import { Store, openDb, type Clock } from "../src/store/db.ts";
import { CarConfig } from "../src/config/config.ts";
import { matchNeverAutoApprove } from "../src/policy/index.ts";
import type {
  ActionBus,
  ChannelPort,
  DeliveryResult,
  EscalationMessage,
  LlmRunner,
  LlmTurnResult,
  MemoryHit,
  MemoryReader,
  PolicyPort,
} from "../src/ports.ts";

export class FakeClock implements Clock {
  constructor(public current: Date = new Date("2026-08-26T12:00:00Z")) {}
  now(): Date {
    return this.current;
  }
  advance(ms: number): void {
    this.current = new Date(this.current.getTime() + ms);
  }
}

export function memoryStore(clock: Clock = new FakeClock()): Store {
  return new Store(openDb(":memory:"), clock);
}

export function testConfig(overrides: Record<string, unknown> = {}): CarConfig {
  return CarConfig.parse({ state_dir: "/tmp/car-test", ...overrides });
}

export class FakeChannel implements ChannelPort {
  escalations: EscalationMessage[] = [];
  notifies: { text: string; carSessionId?: string }[] = [];
  digests: string[] = [];
  sendEscalation(msg: EscalationMessage): void {
    this.escalations.push(msg);
  }
  sendNotify(text: string, carSessionId?: string): void {
    this.notifies.push({ text, carSessionId });
  }
  sendDigest(markdown: string): void {
    this.digests.push(markdown);
  }
}

export class FakeActionBus implements ActionBus {
  delivered: { carSessionId: string; payload: unknown }[] = [];
  templates: { templateId: string; args: unknown }[] = [];
  deliverResult: DeliveryResult = "delivered";
  async deliver(carSessionId: string, _channel: unknown, payload: unknown): Promise<DeliveryResult> {
    this.delivered.push({ carSessionId, payload });
    return this.deliverResult;
  }
  async runTemplate(templateId: string, args: Record<string, unknown>): Promise<{ ok: boolean; output: string }> {
    this.templates.push({ templateId, args });
    return { ok: true, output: "" };
  }
}

export class AllowAllPolicy implements PolicyPort {
  check(): "auto" {
    return "auto";
  }
  gate(): null {
    return null;
  }
  escalateOnly(): boolean {
    return false;
  }
  /**
   * Deliberately *not* permissive: this fake exists to take class-level policy
   * out of a test's way, and the content rail is not class-level policy. Using
   * the real matcher here means no test can accidentally assert that CAR
   * auto-approves a force push.
   */
  autoApprovalBlock(text: string): string | null {
    return matchNeverAutoApprove(text);
  }
}

export class EmptyMemoryReader implements MemoryReader {
  assembleContext(): { charter: string; hits: MemoryHit[] } {
    return { charter: "", hits: [] };
  }
  search(): MemoryHit[] {
    return [];
  }
  get(): null {
    return null;
  }
  grantedRules(): MemoryHit[] {
    return [];
  }
}

/**
 * Scripted LLM: returns canned tool-call turns in order. Triage tests assert
 * decisions and policy blocks, never prose. $0.
 */
export class ScriptedLlm implements LlmRunner {
  private turns: LlmTurnResult[];
  calls = 0;
  constructor(script: { tool: string; args: Record<string, unknown> }[][]) {
    this.turns = script.map((toolCalls) => ({
      toolCalls,
      tokensIn: 100,
      tokensOut: 50,
      costUsd: 0.001,
      model: "fake/scripted",
    }));
  }
  async turn(): Promise<LlmTurnResult> {
    const turn = this.turns[this.calls];
    this.calls++;
    if (!turn) throw new Error("ScriptedLlm: script exhausted");
    return turn;
  }
}
