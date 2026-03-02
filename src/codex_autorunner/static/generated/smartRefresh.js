// GENERATED FILE - do not edit directly. Source: static_src/
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
export function createSmartRefresh(options) {
    let lastSignature = options.initialSignature ?? null;
    const refresh = async (load, request = {}) => {
        const payload = await load();
        const nextSignature = options.getSignature(payload);
        const previousSignature = lastSignature;
        const isInitial = previousSignature === null || request.reason === "initial";
        const isForced = Boolean(request.force);
        const updated = isForced || isInitial || nextSignature !== previousSignature;
        const reason = request.reason ?? (isInitial ? "initial" : "background");
        const ctx = {
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
        }
        else if (options.onSkip) {
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
