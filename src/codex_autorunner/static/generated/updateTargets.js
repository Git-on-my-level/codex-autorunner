// GENERATED FILE - do not edit directly. Source: static_src/
const DEFAULT_UPDATE_TARGET = "both";
const UPDATE_TARGET_ALIASES = new Map([
    ["all", "both"],
    ["both", "both"],
    ["web", "web"],
    ["hub", "web"],
    ["server", "web"],
    ["ui", "web"],
    ["chat", "chat"],
    ["chat-apps", "chat"],
    ["apps", "chat"],
    ["telegram", "telegram"],
    ["tg", "telegram"],
    ["bot", "telegram"],
    ["discord", "discord"],
    ["dc", "discord"],
]);
const updateTargetRegistry = new Map();
function titleCaseTarget(value) {
    return value
        .split(/[-_\s]+/)
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ");
}
function fallbackTargetOption(value) {
    const normalized = normalizeUpdateTarget(value);
    return updateTargetRegistry.get(normalized) || {
        value: normalized,
        label: titleCaseTarget(normalized),
        description: titleCaseTarget(normalized),
        includesWeb: normalized === "both" || normalized === "web",
        restartNotice: "The selected services will restart.",
    };
}
export function normalizeUpdateTarget(value, fallback = DEFAULT_UPDATE_TARGET) {
    if (typeof value !== "string")
        return fallback;
    const normalized = value.trim().toLowerCase();
    if (!normalized)
        return fallback;
    return UPDATE_TARGET_ALIASES.get(normalized) || normalized;
}
export function getUpdateTarget(selectId) {
    const select = selectId ? document.getElementById(selectId) : null;
    return normalizeUpdateTarget(select ? select.value : DEFAULT_UPDATE_TARGET);
}
export function describeUpdateTarget(target) {
    return fallbackTargetOption(target).label;
}
export function includesWebUpdateTarget(target) {
    return fallbackTargetOption(target).includesWeb;
}
export function updateRestartNotice(target) {
    return fallbackTargetOption(target).restartNotice;
}
export function updateTargetOptionsFromResponse(payload) {
    const rawOptions = Array.isArray(payload?.targets) ? payload.targets : [];
    const options = [];
    const seen = new Set();
    rawOptions.forEach((entry) => {
        const value = normalizeUpdateTarget(entry?.value);
        if (!value || seen.has(value))
            return;
        const fallback = fallbackTargetOption(value);
        const option = {
            value,
            label: typeof entry?.label === "string" && entry.label.trim() ? entry.label.trim() : fallback.label,
            description: typeof entry?.description === "string" && entry.description.trim()
                ? entry.description.trim()
                : fallback.description,
            includesWeb: typeof entry?.includes_web === "boolean" ? entry.includes_web : fallback.includesWeb,
            restartNotice: typeof entry?.restart_notice === "string" && entry.restart_notice.trim()
                ? entry.restart_notice.trim()
                : fallback.restartNotice,
        };
        seen.add(value);
        updateTargetRegistry.set(value, option);
        options.push(option);
    });
    return {
        options,
        defaultTarget: normalizeUpdateTarget(payload?.default_target, DEFAULT_UPDATE_TARGET),
    };
}
