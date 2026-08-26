/**
 * WS-B owns src/policy/: action-class vocabulary, policy.toml loader/hot-reload,
 * gates (rate limits, dedupe, breaker, budget).
 * Scaffold stub: everything escalates; nothing is auto. Safe by default.
 */
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import type { PolicyPort, PolicyVerdict } from "../ports.ts";

export function createPolicy(_store: Store, _config: CarConfig): PolicyPort {
  return {
    check(_actionClass: string, _args: Record<string, unknown>): PolicyVerdict {
      return "escalate";
    },
    gate(_actionClass: string, _dedupeHash: string): string | null {
      return null;
    },
    escalateOnly(): boolean {
      return false;
    },
  };
}
