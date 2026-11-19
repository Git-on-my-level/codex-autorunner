/**
 * Runtime capability probes. DESIGN §2/§8: adapters probe the installed vendor
 * CLI and never trust docs (installed codex-cli 0.145.0 has `codex exec resume`
 * but no `codex queue`).
 *
 * One `<cli> --help` per vendor, cached in `kv` for 24h keyed by vendor.
 */
import type { Store } from "../store/db.ts";
import type { Runner } from "./runner.ts";

/** Verbs we look for in `--help` output. */
export const PROBE_VERBS = ["resume", "queue", "exec", "run", "subscribe"] as const;
export type ProbeVerb = (typeof PROBE_VERBS)[number];

export interface CliCapabilities {
  vendor: string;
  cli: string;
  /** True when `--help` exited 0, i.e. the probe itself is trustworthy. */
  ok: boolean;
  verbs: ProbeVerb[];
  resume: boolean;
  queue: boolean;
  probed_at: string;
}

export const CAPABILITY_TTL_MS = 24 * 60 * 60 * 1000;
export const PROBE_TIMEOUT_MS = 10_000;

/** Vendor (contract enum) → the executable that speaks for it. */
const CLI_FOR_VENDOR: Record<string, string> = {
  "claude-code": "claude",
  claude: "claude",
  codex: "codex",
  agentctl: "agentctl",
  cursor: "cursor-agent",
};

export function cliForVendor(vendor: string): string {
  return CLI_FOR_VENDOR[vendor] ?? vendor;
}

export function capabilityKey(vendor: string): string {
  return `actions:caps:${vendor}`;
}

/**
 * Probe (or read the cached probe of) a vendor CLI's capabilities.
 * Cached for 24h; `force` re-probes. Never throws.
 */
export async function probeCapabilities(
  store: Store,
  runner: Runner,
  vendor: string,
  opts: { force?: boolean; timeoutMs?: number } = {},
): Promise<CliCapabilities> {
  const key = capabilityKey(vendor);
  const now = store.clock.now();
  const cached = store.kvGet<CliCapabilities>(key);
  if (!opts.force && cached && now.getTime() - Date.parse(cached.probed_at) < CAPABILITY_TTL_MS) {
    return cached;
  }

  const cli = cliForVendor(vendor);
  let res: { code: number; stdout: string; stderr: string };
  try {
    res = await runner([cli, "--help"], { timeoutMs: opts.timeoutMs ?? PROBE_TIMEOUT_MS });
  } catch (err) {
    res = { code: 127, stdout: "", stderr: String(err) };
  }
  const help = `${res.stdout}\n${res.stderr}`;
  const ok = res.code === 0;
  const verbs = ok ? PROBE_VERBS.filter((v) => new RegExp(`\\b${v}\\b`, "i").test(help)) : [];
  const caps: CliCapabilities = {
    vendor,
    cli,
    ok,
    verbs: [...verbs],
    resume: verbs.includes("resume"),
    queue: verbs.includes("queue"),
    probed_at: now.toISOString(),
  };
  store.kvSet(key, caps);
  store.audit(
    "adapter:probe",
    ok ? "capability.probed" : "capability.probe_failed",
    "vendor",
    vendor,
    { cli, code: res.code, verbs: caps.verbs },
  );
  return caps;
}

/**
 * Capability gate. A *successful* probe that does not mention the verb is
 * believed (→ unsupported, fall back). A failed probe means "unknown", and we
 * attempt anyway rather than degrading every reply on a flaky `--help`.
 */
export function supports(caps: CliCapabilities, verb: ProbeVerb): boolean {
  if (!caps.ok) return true;
  return caps.verbs.includes(verb);
}
