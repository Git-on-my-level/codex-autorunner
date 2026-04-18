// GENERATED FILE - do not edit directly. Source: static_src/
const DEFAULT_VERSION = 1;
function buildKey(config, target) {
    return `${config.keyPrefix}${target}`;
}
export function saveChatHistory(config, target, messages) {
    const key = buildKey(config, target);
    const data = {
        version: config.version ?? DEFAULT_VERSION,
        target,
        messages: messages.slice(-(config.maxMessages || 50)),
        lastUpdated: new Date().toISOString(),
    };
    try {
        localStorage.setItem(key, JSON.stringify(data));
    }
    catch (err) {
        console.warn("localStorage quota exceeded, clearing old chat history", err);
        clearOldChatHistory(config);
        try {
            localStorage.setItem(key, JSON.stringify(data));
        }
        catch (err2) {
            console.error("Failed to save chat history after cleanup", err2);
        }
    }
}
export function loadChatHistory(config, target) {
    const key = buildKey(config, target);
    try {
        const raw = localStorage.getItem(key);
        if (!raw)
            return [];
        const data = JSON.parse(raw);
        const version = config.version ?? DEFAULT_VERSION;
        if (data.version !== version)
            return [];
        return data.messages || [];
    }
    catch {
        return [];
    }
}
export function clearChatHistory(config, target) {
    localStorage.removeItem(buildKey(config, target));
}
function clearOldChatHistory(config) {
    const entries = [];
    for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key?.startsWith(config.keyPrefix)) {
            try {
                const data = JSON.parse(localStorage.getItem(key) || "{}");
                entries.push({ key, lastUpdated: data.lastUpdated || "" });
            }
            catch {
                // ignore parse errors
            }
        }
    }
    entries
        .sort((a, b) => a.lastUpdated.localeCompare(b.lastUpdated))
        .slice(0, Math.ceil(entries.length / 2))
        .forEach((entry) => localStorage.removeItem(entry.key));
}
