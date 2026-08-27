/**
 * WS-E test helpers: a recording fake Runner, a fake HTTP seam, temp state dirs.
 * No network, no real vendor CLI is ever spawned by these tests.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CarConfig } from "../../src/config/config.ts";
import type { Store } from "../../src/store/db.ts";
import type { RunOpts, RunResult, Runner } from "../../src/actions/runner.ts";
import type { FetchLike, HttpRequestInit } from "../../src/actions/adapters/types.ts";
import type { PolicyPort, PolicyVerdict } from "../../src/ports.ts";
import { matchNeverAutoApprove } from "../../src/policy/index.ts";
import type { Vendor } from "../../src/contract/events.ts";

export interface RunCall {
  argv: string[];
  opts: RunOpts;
}

export interface FakeRunner {
  runner: Runner;
  calls: RunCall[];
  /** argv joined with spaces, for terse assertions. */
  lines(): string[];
}

/**
 * Recording runner. `respond` returns a partial result for a given argv;
 * anything it does not answer succeeds silently with empty output.
 */
export function makeRunner(respond: (argv: string[]) => Partial<RunResult> | undefined = () => undefined): FakeRunner {
  const calls: RunCall[] = [];
  const runner: Runner = async (argv, opts) => {
    calls.push({ argv: [...argv], opts });
    const res = respond(argv) ?? {};
    return { code: res.code ?? 0, stdout: res.stdout ?? "", stderr: res.stderr ?? "" };
  };
  return { runner, calls, lines: () => calls.map((c) => c.argv.join(" ")) };
}

/** `--help` output that advertises the verbs a vendor CLI is expected to have. */
export const HELP_TEXT: Record<string, string> = {
  claude: "Usage: claude [options]\n  -r, --resume [value]  Resume a conversation\n  -p, --print",
  codex: "Usage: codex\nCommands:\n  exec    Run non-interactively\n  resume  Resume a previous session",
  agentctl: "Usage: agentctl\nCommands:\n  run  Launch an exact native argv\n  subscribe  Durable callbacks",
};

/** Default responder: answers every `<cli> --help` probe from HELP_TEXT. */
export function helpResponder(
  overrides: (argv: string[]) => Partial<RunResult> | undefined = () => undefined,
): (argv: string[]) => Partial<RunResult> | undefined {
  return (argv) => {
    const override = overrides(argv);
    if (override) return override;
    if (argv[1] === "--help" && argv[0] && HELP_TEXT[argv[0]] !== undefined) {
      return { code: 0, stdout: HELP_TEXT[argv[0]]! };
    }
    return undefined;
  };
}

export interface FakeHttp {
  fetch: FetchLike;
  calls: { url: string; init?: HttpRequestInit }[];
}

export function makeHttp(
  respond: (url: string, init?: HttpRequestInit) => { ok?: boolean; status?: number; body?: string } = () => ({}),
): FakeHttp {
  const calls: { url: string; init?: HttpRequestInit }[] = [];
  const fetchImpl: FetchLike = async (url, init) => {
    calls.push({ url, init });
    const res = respond(url, init);
    const status = res.status ?? 201;
    return {
      ok: res.ok ?? (status >= 200 && status < 300),
      status,
      text: async () => res.body ?? "",
    };
  };
  return { fetch: fetchImpl, calls };
}

/** Policy stub with per-test overrides. */
export class StubPolicy implements PolicyPort {
  verdict: PolicyVerdict = "auto";
  gateReason: string | null = null;
  escalateOnlyFlag = false;
  checks: { actionClass: string; args: Record<string, unknown> }[] = [];
  gates: { actionClass: string; hash: string }[] = [];

  check(actionClass: string, args: Record<string, unknown>): PolicyVerdict {
    this.checks.push({ actionClass, args });
    return this.verdict;
  }
  gate(actionClass: string, dedupeHash: string): string | null {
    this.gates.push({ actionClass, hash: dedupeHash });
    return this.gateReason;
  }
  escalateOnly(): boolean {
    return this.escalateOnlyFlag;
  }
  /** Real matcher, not a stub: the content rail is never what a test wants out of the way. */
  autoApprovalBlock(text: string): string | null {
    return matchNeverAutoApprove(text);
  }
}

export interface TempState {
  dir: string;
  config: CarConfig;
  cleanup(): void;
}

export function tempState(overrides: Record<string, unknown> = {}): TempState {
  const dir = mkdtempSync(join(tmpdir(), "car-ws-e-"));
  return {
    dir,
    config: CarConfig.parse({ state_dir: dir, ...overrides }),
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

/** Seed a session (and its native ref) the way ingest would. */
export function seedSession(
  store: Store,
  ref: { vendor: Vendor; native_id: string; host?: string; cwd?: string },
): string {
  return store.upsertSession(
    {
      vendor: ref.vendor,
      native_id: ref.native_id,
      host: ref.host ?? "test-host",
      ...(ref.cwd ? { cwd: ref.cwd } : {}),
    },
    "2026-08-26T12:00:00.000Z",
  );
}

/** All audit rows for an object, newest last. */
export function auditVerbs(store: Store, objectId: string): string[] {
  return (
    store.db.query("SELECT verb FROM audit WHERE object_id = ? ORDER BY id ASC").all(objectId) as {
      verb: string;
    }[]
  ).map((r) => r.verb);
}

export function auditRows(
  store: Store,
  verb: string,
): { object_id: string; detail: Record<string, unknown> }[] {
  return (
    store.db.query("SELECT object_id, detail_json FROM audit WHERE verb = ? ORDER BY id ASC").all(verb) as {
      object_id: string;
      detail_json: string;
    }[]
  ).map((r) => ({ object_id: r.object_id, detail: JSON.parse(r.detail_json) as Record<string, unknown> }));
}
