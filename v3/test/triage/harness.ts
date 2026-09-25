/**
 * Triage test harness. In-memory SQLite, a FakeClock we drive by hand, and
 * ScriptedLlm for every LLM path — no network, $0.
 */
import { CarEvent } from "../../src/contract/events.ts";
import type { Store } from "../../src/store/db.ts";
import type { CarConfig } from "../../src/config/config.ts";
import type { MemoryHit, MemoryReader, MemoryWriter, PolicyPort } from "../../src/ports.ts";
import {
  AllowAllPolicy,
  EmptyMemoryReader,
  FakeActionBus,
  FakeChannel,
  FakeClock,
  ScriptedLlm,
  memoryStore,
  testConfig,
} from "../fakes.ts";
import { createTriage, type TriageEngine } from "../../src/triage/index.ts";

export type ScriptTurn = { tool: string; args: Record<string, unknown> }[];

/** MemoryReader that hands back a fixed set of granted rules. */
export class StubMemoryReader extends EmptyMemoryReader {
  constructor(
    public granted: MemoryHit[] = [],
    public searchHits: MemoryHit[] = [],
    public charter = "",
  ) {
    super();
  }
  override assembleContext(): { charter: string; hits: MemoryHit[] } {
    return { charter: this.charter, hits: this.searchHits };
  }
  override grantedRules(): MemoryHit[] {
    return this.granted;
  }
  override search(): MemoryHit[] {
    return this.searchHits;
  }
}

export class FakeMemoryWriter implements MemoryWriter {
  proposals: { kind: string; content: Record<string, unknown> }[] = [];
  outcomes: unknown[] = [];
  addFromDavid(): string {
    return "mem_david";
  }
  propose(kind: string, content: Record<string, unknown>): string {
    this.proposals.push({ kind, content });
    return `mem_prop_${this.proposals.length}`;
  }
  recordOutcome(input: unknown): void {
    this.outcomes.push(input);
  }
  setAutonomy(): void {}
}

export function grantedRule(over: Partial<MemoryHit> = {}): MemoryHit {
  return {
    id: "mem_granted_1",
    tier: "rule",
    kind: "autonomy",
    content: { match: "attention.permission", action_class: "approve_permission" },
    confidence: 0.95,
    autonomy: "granted",
    status: "active",
    ...over,
  };
}

export interface EventOverrides {
  idempotency_key?: string;
  ts?: string;
  type?: string;
  severity?: string;
  title?: string;
  body?: string;
  requires_response?: boolean;
  response_channel?: unknown;
  payload?: Record<string, unknown>;
  expires_at?: string;
  session?: unknown;
  source?: Record<string, unknown>;
}

let keySeq = 0;

export function makeEvent(over: EventOverrides = {}): CarEvent {
  keySeq++;
  return CarEvent.parse({
    contract: "car.event.v1",
    idempotency_key: over.idempotency_key ?? `claude-code:sess-1:Question:k${keySeq}`,
    ts: over.ts ?? "2026-08-26T12:00:00Z",
    source: over.source ?? { vendor: "claude-code", host: "mac-studio", adapter: "hook-http" },
    session:
      over.session === undefined
        ? {
            vendor: "claude-code",
            native_id: "sess-1",
            host: "mac-studio",
            cwd: "/Users/dazheng/omi",
            repo: "github.com/x/omi",
            title: "fix BLE reconnect",
          }
        : over.session,
    type: over.type ?? "attention.question",
    severity: over.severity ?? "attention",
    requires_response: over.requires_response ?? true,
    response_channel: over.response_channel ?? null,
    title: over.title ?? "Agent asks: proceed?",
    body: over.body ?? "",
    payload: over.payload ?? {},
    ...(over.expires_at ? { expires_at: over.expires_at } : {}),
  });
}

export interface Harness {
  clock: FakeClock;
  store: Store;
  config: CarConfig;
  policy: PolicyPort;
  actions: FakeActionBus;
  channel: FakeChannel;
  memory: StubMemoryReader;
  writer: FakeMemoryWriter;
  llm: ScriptedLlm;
  triage: TriageEngine;
  /** Ingest one event; returns its id. */
  emit(over?: EventOverrides, opts?: { actor?: "external" | "car" }): string;
  /** Advance the clock past the coalesce window and tick. */
  settle(): Promise<number>;
  rows<T = Record<string, unknown>>(sql: string, ...params: unknown[]): T[];
  incidents(): Record<string, unknown>[];
  decisions(): Record<string, unknown>[];
  escalations(): Record<string, unknown>[];
  actionRows(): Record<string, unknown>[];
  eventRow(id: string): Record<string, unknown>;
  auditVerbs(): string[];
}

export function harness(
  opts: {
    script?: ScriptTurn[];
    config?: Record<string, unknown>;
    policy?: PolicyPort;
    /** Build the policy against this harness's own store (for the real engine). */
    policyFactory?: (store: Store, config: CarConfig) => PolicyPort;
    memory?: StubMemoryReader;
  } = {},
): Harness {
  const clock = new FakeClock();
  const store = memoryStore(clock);
  const config = testConfig(opts.config ?? {});
  const policy = opts.policyFactory?.(store, config) ?? opts.policy ?? new AllowAllPolicy();
  const actions = new FakeActionBus();
  const channel = new FakeChannel();
  const memory = opts.memory ?? new StubMemoryReader();
  const writer = new FakeMemoryWriter();
  const llm = new ScriptedLlm(opts.script ?? []);

  const triage = createTriage(store, config, {
    policy,
    actions,
    channel,
    memoryReader: memory,
    memoryWriter: writer,
    llm,
  });

  const rows = <T = Record<string, unknown>>(sql: string, ...params: unknown[]): T[] =>
    store.db.query(sql).all(...(params as never[])) as T[];

  return {
    clock,
    store,
    config,
    policy,
    actions,
    channel,
    memory,
    writer,
    llm,
    triage,
    emit(over = {}, o = {}) {
      const res = store.ingestEvent(makeEvent(over), o);
      return res.event_id;
    },
    async settle() {
      clock.advance((config.triage.coalesce_seconds + 1) * 1000);
      return triage.tick();
    },
    rows,
    incidents: () => rows("SELECT * FROM incidents ORDER BY opened_at"),
    decisions: () => rows("SELECT * FROM decisions ORDER BY created_at"),
    escalations: () => rows("SELECT * FROM escalations ORDER BY created_at"),
    actionRows: () => rows("SELECT * FROM actions"),
    eventRow: (id: string) =>
      store.db.query("SELECT * FROM events WHERE id = ?").get(id) as Record<string, unknown>,
    auditVerbs: () => rows<{ verb: string }>("SELECT verb FROM audit ORDER BY id").map((r) => r.verb),
  };
}

/** Convenience: a script that immediately resolves. */
export const resolveScript = (summary = "handled"): ScriptTurn[] => [[{ tool: "resolve", args: { summary } }]];
