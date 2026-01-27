// GENERATED FILE - do not edit directly. Source: static_src/
const listeners = new Map();
export function subscribe(event, handler) {
    if (!listeners.has(event)) {
        listeners.set(event, new Set());
    }
    const set = listeners.get(event);
    set.add(handler);
    return () => set.delete(handler);
}
export function publish(event, payload) {
    const set = listeners.get(event);
    if (!set)
        return;
    for (const handler of Array.from(set)) {
        try {
            handler(payload);
        }
        catch (err) {
            console.error(`Error in '${event}' subscriber`, err);
        }
    }
}
