/**
 * WS-B owns src/policy/: action-class vocabulary, policy.toml loader/hot-reload,
 * gates (rate limits, dedupe, circuit breaker, budget).
 *
 * Fail-safe by construction: code defines the *shape*, config enables behaviour.
 * A missing/empty/broken policy.toml means every class is disabled, so
 * `check()` returns "escalate" for everything. Nothing is auto by default.
 *
 * DESIGN §5 policy shape:
 *   [classes.reply]                  enabled, max_per_hour, max_per_session_per_hour
 *   [classes.exec.restart_service]   enabled, allowlist, max_per_day
 *   [classes.exec.agentctl_continue] enabled, repos_deny
 *   [guards]                         never_touch_branches, quiet_hours
 *   [budget]                         triage_daily_usd
 */
import { readFileSync, statSync } from "node:fs";
import { parse as parseToml } from "smol-toml";
import { z } from "zod";
import type { Store } from "../store/db.ts";
import { policyPath, type CarConfig } from "../config/config.ts";
import type { PolicyPort, PolicyVerdict } from "../ports.ts";
import { dangerousContent as coreDangerousContent } from "../safety/index.ts";

/* ----------------------------------------------------------------- constants */

/** Identical (class, args) inside this window is a runaway signal, not a retry. */
export const DEDUPE_WINDOW_MINUTES = 30;
/** N failed actions inside BREAKER_WINDOW_MINUTES flips escalate-only mode. */
export const BREAKER_FAILURES = 5;
export const BREAKER_WINDOW_MINUTES = 10;
/** kv key holding the sticky escalate-only flag (cleared by /panic-clear). */
export const ESCALATE_ONLY_KEY = "escalate_only";
/**
 * kv key holding when David last cleared the breaker. Failures before this
 * instant are forgiven — otherwise /panic-clear would re-trip instantly on the
 * same five rows and the button would look broken.
 */
export const BREAKER_CLEARED_AT_KEY = "escalate_only_cleared_at";
/** Digest warning threshold as a fraction of the daily triage budget. */
export const BUDGET_WARN_FRACTION = 0.8;

/**
 * Requests CAR may never approve on its own, whatever a grant says.
 *
 * Autonomy is scoped to a repo or a request lineage, but what makes a request
 * dangerous lives in its text — so class-level policy cannot see it. Without
 * this rail, one tap on "auto-approve read-only greps in omi" would also cover
 * `git push --force origin main` the moment it appeared in the same scope.
 *
 * These are deliberately not configurable off: each names an action that cannot
 * be undone from a Telegram tap. CAR still *escalates* every one of them — the
 * rail costs a notification, never an outcome.
 */
export const NEVER_AUTO_APPROVE: { pattern: RegExp; label: string }[] = [
  { pattern: /\bgit\b[^\n]*\bpush\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b)/i, label: "force push" },
  { pattern: /\bgit\b[^\n]*\breset\b[^\n]*--hard/i, label: "git reset --hard" },
  { pattern: /\bgit\b[^\n]*\bclean\b[^\n]*-[a-z]*f/i, label: "git clean -f" },
  { pattern: /\brm\b[^\n]*-[a-z]*r[a-z]*f|\brm\b[^\n]*-[a-z]*f[a-z]*r/i, label: "recursive force delete" },
  { pattern: /\bsudo\b/i, label: "sudo" },
  { pattern: /\bcurl\b[^\n]*\|[^\n]*\b(sh|bash|zsh)\b/i, label: "curl piped to a shell" },
  { pattern: /\bwget\b[^\n]*\|[^\n]*\b(sh|bash|zsh)\b/i, label: "wget piped to a shell" },
  { pattern: /\bchmod\b[^\n]*\b777\b/i, label: "chmod 777" },
  { pattern: /\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b/i, label: "merging a pull request" },
  { pattern: /\b(npm|bun|yarn|pnpm)\b[^\n]*\bpublish\b/i, label: "publishing a package" },
  { pattern: /\bterraform\b[^\n]*\b(apply|destroy)\b/i, label: "terraform apply/destroy" },
  { pattern: /\bkubectl\b[^\n]*\bdelete\b/i, label: "kubectl delete" },
  { pattern: /\bdrop\s+(table|database)\b/i, label: "dropping a table or database" },
  { pattern: /\b(prod|production)\b[^\n]*\b(deploy|restart|delete|drop)\b/i, label: "a production change" },
  { pattern: /(^|[\s/'"])\.env(\.[\w-]+)?([\s/'"]|$)|\bid_rsa\b|\bcredentials\b|\bsecrets?\.(json|ya?ml|toml)\b/i, label: "credentials or secrets" },
];

/**
 * Match `text` against the built-in rail plus any configured extras. Invalid
 * user regexes are matched literally rather than thrown away, so a typo in
 * policy.toml can never quietly widen what CAR will approve.
 */
export function matchNeverAutoApprove(text: string, extraPatterns: string[] = []): string | null {
  // Keep this compatibility helper on the policy port, but make the
  // non-bypassable built-in decision come from the core safety kernel. This
  // prevents provider/contextual policy from becoming the authority for
  // irreversible content while preserving the existing surface API.
  const coreLabel = coreDangerousContent(text);
  if (coreLabel) return coreLabel;
  for (const { pattern, label } of NEVER_AUTO_APPROVE) {
    if (pattern.test(text)) return label;
  }
  for (const raw of extraPatterns) {
    let re: RegExp;
    try {
      re = new RegExp(raw, "i");
    } catch {
      if (text.toLowerCase().includes(raw.toLowerCase())) return raw;
      continue;
    }
    if (re.test(text)) return raw;
  }
  return null;
}

/* -------------------------------------------------------------------- schema */

const ClassPolicySchema = z.object({
  enabled: z.boolean().default(false),
  max_per_hour: z.number().int().nonnegative().optional(),
  max_per_session_per_hour: z.number().int().nonnegative().optional(),
  max_per_day: z.number().int().nonnegative().optional(),
  allowlist: z.array(z.string()).optional(),
  repos_deny: z.array(z.string()).optional(),
});
export type ClassPolicy = z.infer<typeof ClassPolicySchema>;

const GuardsSchema = z
  .object({
    never_touch_branches: z.array(z.string()).default([]),
    /** "HH:MM-HH:MM" local time; empty disables. Only `urgent` pushes inside it. */
    quiet_hours: z.string().default(""),
    /**
     * Extra case-insensitive regexes that must never be auto-approved. These are
     * *added* to {@link NEVER_AUTO_APPROVE}; the built-ins cannot be switched off
     * from config, because the failure they prevent is unrecoverable.
     */
    never_auto_approve: z.array(z.string()).default([]),
  })
  .prefault({});

const BudgetSchema = z
  .object({
    /** 0 disables the cap. 80% → digest warning; 100% → escalate-only. */
    triage_daily_usd: z.number().nonnegative().default(0),
  })
  .prefault({});

const PolicyFileSchema = z.object({
  classes: z.record(z.string(), z.unknown()).prefault({}),
  guards: GuardsSchema,
  budget: BudgetSchema,
});

export interface PolicyDoc {
  classes: Record<string, ClassPolicy>;
  guards: { never_touch_branches: string[]; quiet_hours: string; never_auto_approve: string[] };
  budget: { triage_daily_usd: number };
  /** Non-empty when the file failed to parse/validate — we then run fully closed. */
  error: string | null;
}

export const EMPTY_POLICY: PolicyDoc = {
  classes: {},
  guards: { never_touch_branches: [], quiet_hours: "", never_auto_approve: [] },
  budget: { triage_daily_usd: 0 },
  error: null,
};

/** Keys that mark a TOML table as an action class rather than a namespace. */
const LEAF_KEYS = new Set([
  "enabled",
  "max_per_hour",
  "max_per_session_per_hour",
  "max_per_day",
  "allowlist",
  "repos_deny",
]);

function isTable(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * `[classes.exec.restart_service]` nests two deep in TOML; flatten to the dotted
 * class name the executor uses ("exec.restart_service").
 */
export function flattenClasses(
  raw: Record<string, unknown>,
  prefix = "",
  out: Record<string, ClassPolicy> = {},
): Record<string, ClassPolicy> {
  for (const [key, value] of Object.entries(raw)) {
    if (!isTable(value)) continue;
    const name = prefix ? `${prefix}.${key}` : key;
    const childKeys = Object.keys(value);
    if (childKeys.some((k) => LEAF_KEYS.has(k))) {
      out[name] = ClassPolicySchema.parse(value);
    }
    const nested: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      if (!LEAF_KEYS.has(k) && isTable(v)) nested[k] = v;
    }
    if (Object.keys(nested).length > 0) flattenClasses(nested, name, out);
  }
  return out;
}

export function parsePolicyText(text: string): PolicyDoc {
  const parsed = PolicyFileSchema.parse(parseToml(text));
  return {
    classes: flattenClasses(parsed.classes),
    guards: parsed.guards,
    budget: parsed.budget,
    error: null,
  };
}

/* --------------------------------------------------------------- glob + time */

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Shell-style `*` globs only — policy patterns are deliberately simple. */
export function globMatch(pattern: string, value: string): boolean {
  const rx = new RegExp(`^${pattern.split("*").map(escapeRegex).join(".*")}$`);
  return rx.test(value);
}

function minutes(n: number): number {
  return n * 60_000;
}

function isoBefore(now: Date, ms: number): string {
  return new Date(now.getTime() - ms).toISOString();
}

/** "23:00-08:00" spanning midnight is supported. */
export function inQuietHours(spec: string, now: Date): boolean {
  const m = /^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/.exec(spec.trim());
  if (!m) return false;
  const cur = now.getHours() * 60 + now.getMinutes();
  const start = Number(m[1]) * 60 + Number(m[2]);
  const end = Number(m[3]) * 60 + Number(m[4]);
  return start <= end ? cur >= start && cur < end : cur >= start || cur < end;
}

function stringArg(args: Record<string, unknown>, ...keys: string[]): string | null {
  for (const k of keys) {
    const v = args[k];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return null;
}

/* --------------------------------------------------------------------- ports */

/**
 * Superset of {@link PolicyPort}: the daemon only needs the port, but triage and
 * the Telegram surface use the extras. `gate`'s third parameter is optional so
 * this stays assignable to the frozen PolicyPort signature.
 */
export interface PolicyEngine extends PolicyPort {
  gate(actionClass: string, dedupeHash: string, carSessionId?: string | null): string | null;
  /** Compact one-liner injected into the triage prompt. */
  summary(): string;
  /** Telegram /panic-clear: lift escalate-only mode. */
  clearBreaker(): void;
  /** True inside configured quiet hours (only `urgent` pushes). */
  quietHours(): boolean;
  /** Current validated document (for the web UI / `card policy`). */
  snapshot(): PolicyDoc;
  /** Budget accounting for the digest. */
  budgetStatus(): { limit: number; spent: number; fraction: number; warn: boolean; exhausted: boolean };
}

/* ------------------------------------------------------------------- factory */

export function createPolicy(store: Store, config: CarConfig): PolicyEngine {
  const path = policyPath(config);
  let doc: PolicyDoc = EMPTY_POLICY;
  let loadedMtimeMs = -1;
  let loadedSize = -1;
  let everLoaded = false;

  /** Hot reload: cheap stat per call; a changed mtime/size re-parses the file. */
  function reloadIfChanged(): void {
    let stat: { mtimeMs: number; size: number } | null = null;
    try {
      const s = statSync(path);
      stat = { mtimeMs: s.mtimeMs, size: s.size };
    } catch {
      stat = null;
    }
    if (!stat) {
      if (!everLoaded || loadedMtimeMs !== -1) {
        // File absent (or just deleted): run fully closed.
        doc = EMPTY_POLICY;
        loadedMtimeMs = -1;
        loadedSize = -1;
        everLoaded = true;
        store.audit("policy", "policy.absent", "policy", path, { effect: "all classes disabled" });
      }
      return;
    }
    if (everLoaded && stat.mtimeMs === loadedMtimeMs && stat.size === loadedSize) return;
    loadedMtimeMs = stat.mtimeMs;
    loadedSize = stat.size;
    everLoaded = true;
    try {
      doc = parsePolicyText(readFileSync(path, "utf8"));
      store.audit("policy", "policy.loaded", "policy", path, {
        classes: Object.keys(doc.classes),
        budget: doc.budget.triage_daily_usd,
      });
    } catch (err) {
      // Invalid policy must never widen permissions: fall back to fully closed.
      doc = { ...EMPTY_POLICY, error: String(err) };
      store.audit("policy", "policy.invalid", "policy", path, {
        error: String(err),
        effect: "all classes disabled",
      });
    }
  }

  function countActions(sql: string, params: unknown[]): number {
    const row = store.db.query(sql).get(...(params as never[])) as { n: number } | null;
    return row ? row.n : 0;
  }

  function failuresInWindow(now: Date): number {
    const windowStart = isoBefore(now, minutes(BREAKER_WINDOW_MINUTES));
    const clearedAt = store.kvGet<string>(BREAKER_CLEARED_AT_KEY);
    // Failures at or before the clear instant are forgiven (strict `>`), so
    // /panic-clear cannot be undone by the very rows it was clearing.
    if (clearedAt && clearedAt >= windowStart) {
      return countActions(
        `SELECT COUNT(*) n FROM actions
          WHERE state = 'failed' AND COALESCE(finished_at, started_at) > ?`,
        [clearedAt],
      );
    }
    return countActions(
      `SELECT COUNT(*) n FROM actions
        WHERE state = 'failed' AND COALESCE(finished_at, started_at) >= ?`,
      [windowStart],
    );
  }

  function breakerTripped(now: Date): boolean {
    if (store.kvGet<boolean>(ESCALATE_ONLY_KEY) === true) return true;
    const failures = failuresInWindow(now);
    if (failures >= BREAKER_FAILURES) {
      store.kvSet(ESCALATE_ONLY_KEY, true);
      store.audit("policy", "policy.breaker_tripped", "policy", ESCALATE_ONLY_KEY, {
        failures,
        window_minutes: BREAKER_WINDOW_MINUTES,
      });
      return true;
    }
    return false;
  }

  function budgetStatus(): {
    limit: number;
    spent: number;
    fraction: number;
    warn: boolean;
    exhausted: boolean;
  } {
    const limit = doc.budget.triage_daily_usd;
    const spent = store.spendToday().cost_usd;
    const fraction = limit > 0 ? spent / limit : 0;
    return {
      limit,
      spent,
      fraction,
      warn: limit > 0 && fraction >= BUDGET_WARN_FRACTION,
      exhausted: limit > 0 && spent >= limit,
    };
  }

  const engine: PolicyEngine = {
    check(actionClass: string, args: Record<string, unknown> = {}): PolicyVerdict {
      reloadIfChanged();
      if (engine.escalateOnly()) return "escalate";

      const cls = doc.classes[actionClass];
      // Unknown or disabled class: impossible, not discouraged (DESIGN §Non-negotiable 5).
      if (!cls || !cls.enabled) return "escalate";

      const branch = stringArg(args, "branch", "target_branch");
      if (branch && doc.guards.never_touch_branches.some((b) => globMatch(b, branch))) return "forbid";

      const repo = stringArg(args, "repo", "repository");
      if (repo && (cls.repos_deny ?? []).some((p) => globMatch(p, repo))) return "forbid";

      if (cls.allowlist) {
        const target = stringArg(args, "target", "service", "name", "template_id");
        if (!target || !cls.allowlist.some((p) => globMatch(p, target))) return "forbid";
      }
      return "auto";
    },

    autoApprovalBlock(text: string): string | null {
      reloadIfChanged();
      return matchNeverAutoApprove(text, doc.guards.never_auto_approve);
    },

    gate(actionClass: string, dedupeHash: string, carSessionId?: string | null): string | null {
      reloadIfChanged();
      const now = store.clock.now();

      if (breakerTripped(now)) {
        return `circuit_breaker: escalate-only mode (${BREAKER_FAILURES} failed actions in ${BREAKER_WINDOW_MINUTES}m); clear with /panic-clear`;
      }

      const budget = budgetStatus();
      if (budget.exhausted) {
        return `budget: daily triage budget $${budget.limit.toFixed(2)} reached (spent $${budget.spent.toFixed(2)})`;
      }

      const dupes = countActions(
        `SELECT COUNT(*) n FROM actions
          WHERE dedupe_hash = ? AND (started_at IS NULL OR started_at >= ?)`,
        [dedupeHash, isoBefore(now, minutes(DEDUPE_WINDOW_MINUTES))],
      );
      if (dupes > 0) {
        return `dedupe: identical ${actionClass} action already attempted within ${DEDUPE_WINDOW_MINUTES}m`;
      }

      const cls = doc.classes[actionClass];
      if (cls) {
        if (cls.max_per_hour !== undefined) {
          const n = countActions("SELECT COUNT(*) n FROM actions WHERE class = ? AND started_at >= ?", [
            actionClass,
            isoBefore(now, minutes(60)),
          ]);
          if (n >= cls.max_per_hour) {
            return `rate_limit: ${actionClass} max_per_hour=${cls.max_per_hour} (${n} in the last hour)`;
          }
        }
        if (cls.max_per_day !== undefined) {
          const n = countActions("SELECT COUNT(*) n FROM actions WHERE class = ? AND started_at >= ?", [
            actionClass,
            isoBefore(now, minutes(60 * 24)),
          ]);
          if (n >= cls.max_per_day) {
            return `rate_limit: ${actionClass} max_per_day=${cls.max_per_day} (${n} in the last 24h)`;
          }
        }
        if (cls.max_per_session_per_hour !== undefined && carSessionId) {
          const n = countActions(
            `SELECT COUNT(*) n FROM actions a
               JOIN decisions d ON d.id = a.decision_id
               JOIN incidents i ON i.id = d.incident_id
              WHERE a.class = ? AND i.car_session_id = ? AND a.started_at >= ?`,
            [actionClass, carSessionId, isoBefore(now, minutes(60))],
          );
          if (n >= cls.max_per_session_per_hour) {
            return `rate_limit: ${actionClass} max_per_session_per_hour=${cls.max_per_session_per_hour} (${n} this hour on this session)`;
          }
        }
      }
      return null;
    },

    escalateOnly(): boolean {
      reloadIfChanged();
      if (breakerTripped(store.clock.now())) return true;
      return budgetStatus().exhausted;
    },

    clearBreaker(): void {
      clearBreaker(store);
    },

    quietHours(): boolean {
      reloadIfChanged();
      return inQuietHours(doc.guards.quiet_hours, store.clock.now());
    },

    snapshot(): PolicyDoc {
      reloadIfChanged();
      return doc;
    },

    budgetStatus() {
      reloadIfChanged();
      return budgetStatus();
    },

    summary(): string {
      reloadIfChanged();
      const parts: string[] = [];
      const names = Object.keys(doc.classes).sort();
      if (names.length === 0) {
        parts.push("action classes: none enabled (everything escalates)");
      } else {
        const rendered = names.map((name) => {
          const c = doc.classes[name]!;
          if (!c.enabled) return `${name}=off`;
          const lim: string[] = [];
          if (c.max_per_hour !== undefined) lim.push(`${c.max_per_hour}/h`);
          if (c.max_per_session_per_hour !== undefined) lim.push(`${c.max_per_session_per_hour}/session/h`);
          if (c.max_per_day !== undefined) lim.push(`${c.max_per_day}/day`);
          if (c.allowlist) lim.push(`allow=[${c.allowlist.join(",")}]`);
          if (c.repos_deny) lim.push(`repos_deny=[${c.repos_deny.join(",")}]`);
          return `${name}=on${lim.length ? ` (${lim.join(", ")})` : ""}`;
        });
        parts.push(`action classes: ${rendered.join("; ")}`);
      }
      if (doc.guards.never_touch_branches.length > 0) {
        parts.push(`never touch branches: ${doc.guards.never_touch_branches.join(", ")}`);
      }
      if (doc.guards.quiet_hours) {
        parts.push(`quiet hours: ${doc.guards.quiet_hours}${engine.quietHours() ? " (ACTIVE)" : ""}`);
      }
      const b = budgetStatus();
      if (b.limit > 0) {
        parts.push(`triage budget: $${b.spent.toFixed(3)} of $${b.limit.toFixed(2)} today`);
      }
      if (doc.error) parts.push("policy.toml INVALID — running fully closed");
      if (store.kvGet<boolean>(ESCALATE_ONLY_KEY) === true || b.exhausted) {
        parts.push("MODE: ESCALATE-ONLY (no autonomous actions)");
      }
      return parts.join(" · ");
    },
  };

  reloadIfChanged();
  return engine;
}

/* ------------------------------------------------------------ free functions */

/**
 * Lift escalate-only mode. Exported standalone for the Telegram `/panic-clear`
 * flow, which holds the store but not the engine instance.
 */
export function clearBreaker(store: Store): void {
  const at = store.clock.now().toISOString();
  store.kvSet(ESCALATE_ONLY_KEY, false);
  store.kvSet(BREAKER_CLEARED_AT_KEY, at);
  store.audit("david", "policy.breaker_cleared", "policy", ESCALATE_ONLY_KEY, { cleared_at: at });
}

/** Flip escalate-only mode on (Telegram `/panic`). */
export function tripBreaker(store: Store, reason: string): void {
  store.kvSet(ESCALATE_ONLY_KEY, true);
  store.audit("david", "policy.breaker_tripped", "policy", ESCALATE_ONLY_KEY, { reason });
}

/** Compact policy description for the triage prompt. Safe on a plain PolicyPort. */
export function policySummary(policy: PolicyPort): string {
  const engine = policy as Partial<PolicyEngine>;
  if (typeof engine.summary === "function") return engine.summary();
  return policy.escalateOnly()
    ? "MODE: ESCALATE-ONLY (no autonomous actions)"
    : "action classes: unknown (policy engine does not expose a summary)";
}
