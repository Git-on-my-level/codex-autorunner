export type SmartRefreshReason = "initial" | "background" | "manual";

export interface SmartRefreshContext {
  isInitial: boolean;
  isForced: boolean;
  previousSignature: string | null;
  nextSignature: string;
  updated: boolean;
  reason: SmartRefreshReason;
}

export interface SmartRefreshResult {
  updated: boolean;
  signature: string;
  previousSignature: string | null;
  reason: SmartRefreshReason;
}

export interface SmartRefreshOptions<T> {
  getSignature: (payload: T) => string;
  render: (payload: T, ctx: SmartRefreshContext) => void | Promise<void>;
  onSkip?: (payload: T, ctx: SmartRefreshContext) => void | Promise<void>;
  initialSignature?: string | null;
}

export interface SmartRefreshRequest {
  force?: boolean;
  reason?: SmartRefreshReason;
}

export interface SmartRefresh<T> {
  refresh: (
    load: () => Promise<T>,
    request?: SmartRefreshRequest
  ) => Promise<SmartRefreshResult>;
  reset: () => void;
  getSignature: () => string | null;
}

/**
 * Create a signature-aware refresh helper that only calls render when data changes.
 *
 * Usage:
 * const smartRefresh = createSmartRefresh({
 *   getSignature: (payload) => payload.items.map((i) => i.id).join("|"),
 *   render: (payload) => renderList(payload.items),
 * });
 *
 * await smartRefresh.refresh(loadItems, { reason: "background" });
 */
export function createSmartRefresh<T>(options: SmartRefreshOptions<T>): SmartRefresh<T> {
  let lastSignature: string | null = options.initialSignature ?? null;

  const refresh = async (
    load: () => Promise<T>,
    request: SmartRefreshRequest = {}
  ): Promise<SmartRefreshResult> => {
    const payload = await load();
    const nextSignature = options.getSignature(payload);
    const previousSignature = lastSignature;
    const isInitial = previousSignature === null || request.reason === "initial";
    const isForced = Boolean(request.force);
    const updated = isForced || isInitial || nextSignature !== previousSignature;
    const reason: SmartRefreshReason = request.reason ?? (isInitial ? "initial" : "background");

    const ctx: SmartRefreshContext = {
      isInitial,
      isForced,
      previousSignature,
      nextSignature,
      updated,
      reason,
    };

    if (updated) {
      lastSignature = nextSignature;
      await options.render(payload, ctx);
    } else if (options.onSkip) {
      await options.onSkip(payload, ctx);
    }

    return {
      updated,
      signature: nextSignature,
      previousSignature,
      reason,
    };
  };

  return {
    refresh,
    reset: () => {
      lastSignature = null;
    },
    getSignature: () => lastSignature,
  };
}
