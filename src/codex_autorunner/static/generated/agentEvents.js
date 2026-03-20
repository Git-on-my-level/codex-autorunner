// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Shared parsing helpers for agent (app-server) events.
 * Used by ticket chat and live agent output to render rich activity.
 */
const openCodeMessageRoles = {};
const openCodePendingTextByMessage = {};
let openCodePendingTextNoId = "";
let openCodeMessageRolesSeen = false;
export function resetOpenCodeEventState() {
    Object.keys(openCodeMessageRoles).forEach((key) => delete openCodeMessageRoles[key]);
    Object.keys(openCodePendingTextByMessage).forEach((key) => delete openCodePendingTextByMessage[key]);
    openCodePendingTextNoId = "";
    openCodeMessageRolesSeen = false;
}
function asRecord(value) {
    return value && typeof value === "object" ? value : null;
}
function extractOpenCodeProperties(params) {
    const properties = params ? asRecord(params.properties) : null;
    return properties || {};
}
function extractOpenCodePart(params) {
    const properties = extractOpenCodeProperties(params);
    const part = asRecord(properties.part);
    if (part)
        return part;
    return params ? asRecord(params.part) : null;
}
function extractOpenCodeInfo(params) {
    const properties = extractOpenCodeProperties(params);
    const info = asRecord(properties.info);
    if (info)
        return info;
    return params ? asRecord(params.info) : null;
}
function extractOpenCodeMessageId(params) {
    const info = extractOpenCodeInfo(params);
    const infoId = info?.id;
    if (typeof infoId === "string" && infoId.trim())
        return infoId.trim();
    const part = extractOpenCodePart(params);
    const partMessageId = part?.messageID || part?.messageId || part?.message_id;
    if (typeof partMessageId === "string" && partMessageId.trim())
        return partMessageId.trim();
    return null;
}
function extractOpenCodeRole(params) {
    const info = extractOpenCodeInfo(params);
    const role = info?.role;
    if (typeof role === "string")
        return role.trim().toLowerCase();
    const directRole = params ? params.role : null;
    if (typeof directRole === "string")
        return directRole.trim().toLowerCase();
    return "";
}
function extractOpenCodeDeltaText(params) {
    if (!params || typeof params !== "object")
        return "";
    if (typeof params.delta === "string")
        return params.delta;
    if (typeof params.text === "string")
        return params.text;
    if (typeof params.output === "string")
        return params.output;
    const properties = extractOpenCodeProperties(params);
    if (typeof properties.delta === "string" && properties.delta)
        return properties.delta;
    const delta = asRecord(properties.delta);
    const deltaText = delta?.text;
    if (typeof deltaText === "string" && deltaText)
        return deltaText;
    return "";
}
function extractOpenCodePartText(params) {
    const part = extractOpenCodePart(params);
    const text = part?.text;
    if (typeof text === "string" && text)
        return text;
    return "";
}
function extractOpenCodeToolInput(part) {
    if (!part)
        return "";
    for (const key of ["input", "command", "cmd", "script"]) {
        const value = part[key];
        if (typeof value === "string" && value.trim()) {
            return value.trim();
        }
    }
    const args = asRecord(part.args) || asRecord(part.arguments) || asRecord(part.params);
    if (args) {
        for (const key of ["command", "cmd", "script", "input"]) {
            const value = args[key];
            if (typeof value === "string" && value.trim()) {
                return value.trim();
            }
        }
    }
    return "";
}
function mergeOpenCodeText(current, incoming) {
    if (!incoming)
        return current;
    if (!current)
        return incoming;
    if (incoming === current)
        return current;
    if (incoming.length > current.length && incoming.startsWith(current))
        return incoming;
    return `${current}${incoming}`;
}
function extractCommand(item, params) {
    const command = item?.command ?? params?.command;
    if (Array.isArray(command)) {
        return command
            .map((part) => String(part))
            .join(" ")
            .trim();
    }
    if (typeof command === "string")
        return command.trim();
    return "";
}
function extractFiles(payload, limit = Number.POSITIVE_INFINITY) {
    const files = [];
    const addEntry = (entry) => {
        if (files.length >= limit)
            return true;
        if (typeof entry === "string" && entry.trim()) {
            files.push(entry.trim());
            return files.length >= limit;
        }
        if (entry && typeof entry === "object") {
            const entryObj = entry;
            const path = entryObj.path || entryObj.file || entryObj.name;
            if (typeof path === "string" && path.trim()) {
                files.push(path.trim());
                return files.length >= limit;
            }
        }
        return false;
    };
    if (!payload || typeof payload !== "object")
        return files;
    for (const key of ["files", "fileChanges", "paths"]) {
        const value = payload[key];
        if (!Array.isArray(value))
            continue;
        for (const entry of value) {
            if (addEntry(entry))
                return files;
        }
    }
    for (const key of ["path", "file", "name"]) {
        if (addEntry(payload[key]))
            return files;
    }
    return files;
}
function extractCompactPreview(payload) {
    if (!payload || typeof payload !== "object")
        return "";
    const preview = payload.preview;
    return typeof preview === "string" ? preview.trim() : "";
}
function summarizeFileChanges(files, totalCount) {
    const hasTotalCount = typeof totalCount === "number" && Number.isFinite(totalCount);
    const visibleCount = hasTotalCount && totalCount > 4 ? 4 : Math.min(files.length, 4);
    const visible = files.slice(0, visibleCount).filter((value) => value.trim());
    if (visible.length) {
        const effectiveTotal = hasTotalCount ? totalCount : files.length;
        const remaining = Math.max(effectiveTotal - visible.length, 0);
        return {
            summary: remaining > 0 ? `${visible.join(", ")} +${remaining} more` : visible.join(", "),
            detail: hasTotalCount && totalCount > visible.length ? `${totalCount} file changes` : "",
        };
    }
    if (hasTotalCount) {
        return {
            summary: totalCount === 1 ? "1 file change" : `${totalCount} file changes`,
            detail: "",
        };
    }
    return { summary: "File changes", detail: "" };
}
function parseLegacyDiffEntryCount(value) {
    const match = value.match(/^(\d+)\s+diff entries?$/i);
    if (!match)
        return null;
    return Number.parseInt(match[1] || "", 10);
}
function extractErrorMessage(params) {
    if (!params || typeof params !== "object")
        return "";
    const err = params.error;
    if (err && typeof err === "object") {
        const errObj = err;
        const message = typeof errObj.message === "string" ? errObj.message : "";
        const details = typeof errObj.additionalDetails === "string"
            ? errObj.additionalDetails
            : typeof errObj.details === "string"
                ? errObj.details
                : "";
        if (message && details && message !== details) {
            return `${message} (${details})`;
        }
        return message || details;
    }
    if (typeof err === "string")
        return err;
    if (typeof params.message === "string")
        return params.message;
    return "";
}
function hasMeaningfulText(summary, detail) {
    return Boolean(summary.trim() || detail.trim());
}
function inferSignificance(kind, method) {
    if (kind === "thinking")
        return true;
    if (kind === "error")
        return true;
    if (["tool", "command", "file", "output"].includes(kind))
        return true;
    if (method.includes("requestApproval"))
        return true;
    return false;
}
/**
 * Extract output delta text from an event payload.
 */
export function extractOutputDelta(payload) {
    const message = payload && typeof payload === "object" ? payload.message || payload : payload;
    if (!message || typeof message !== "object")
        return "";
    const method = String(message.method || "").toLowerCase();
    const params = message.params || {};
    if (method.includes("outputdelta")) {
        const delta = extractOpenCodeDeltaText(params);
        if (delta)
            return delta;
    }
    if (method === "message.part.updated") {
        return extractOpenCodeDeltaText(params) || extractOpenCodePartText(params);
    }
    return "";
}
/**
 * Parse an app-server event payload into a normalized AgentEvent plus merge hints.
 */
export function parseAppServerEvent(payload) {
    const message = payload && typeof payload === "object" ? payload.message || payload : payload;
    if (!message || typeof message !== "object")
        return null;
    const messageObj = message;
    const method = messageObj.method || "app-server";
    const params = messageObj.params || {};
    const item = params.item || {};
    const itemId = params.itemId || item.id || item.itemId || null;
    const receivedAt = payload && typeof payload === "object"
        ? payload.received_at || payload.receivedAt || Date.now()
        : Date.now();
    if (method === "session.diff") {
        const properties = asRecord(params.properties) || {};
        const diffEntries = Array.isArray(properties.diff) ? properties.diff : null;
        const rawDiffCount = properties.diff_count;
        let diffCount = typeof rawDiffCount === "number" ? rawDiffCount : null;
        if (diffCount == null) {
            const compactFiles = Array.isArray(properties.files) ? properties.files.length : null;
            diffCount = diffEntries?.length ?? compactFiles;
        }
        const filePayload = diffEntries ? { files: diffEntries } : properties;
        const files = extractFiles(filePayload, 10);
        const fallbackPreview = extractCompactPreview(payload) ||
            (typeof params.message === "string" ? params.message.trim() : "") ||
            (typeof params.status === "string" ? params.status.trim() : "");
        const { summary: fileSummary, detail } = summarizeFileChanges(files, diffCount);
        const diffCountLabel = diffCount === null ? "" : (diffCount === 1 ? "1 file change" : `${diffCount} file changes`);
        let summary = fileSummary;
        let detailText = detail;
        if (!files.length && fallbackPreview) {
            const legacyDiffCount = parseLegacyDiffEntryCount(fallbackPreview);
            const effectiveDiffCount = diffCount ?? legacyDiffCount;
            if (effectiveDiffCount !== null && legacyDiffCount !== null) {
                summary = effectiveDiffCount === 1 ? "1 file change" : `${effectiveDiffCount} file changes`;
            }
            else if (fallbackPreview !== "diff updated") {
                summary = fallbackPreview;
                if (!detailText && diffCountLabel && fallbackPreview !== diffCountLabel) {
                    detailText = diffCountLabel;
                }
            }
        }
        return {
            event: {
                id: payload?.id || `${Date.now()}`,
                title: "File change",
                summary,
                detail: detailText,
                kind: "file",
                isSignificant: true,
                time: receivedAt,
                itemId,
                method,
            },
        };
    }
    // Handle reasoning/thinking deltas - accumulate into existing event
    if (method === "item/reasoning/summaryTextDelta") {
        const delta = params.delta || "";
        if (!delta)
            return null;
        const event = {
            id: payload?.id || `${Date.now()}`,
            title: "Thinking",
            summary: delta,
            detail: "",
            kind: "thinking",
            isSignificant: true,
            time: receivedAt,
            itemId,
            method,
        };
        return { event, mergeStrategy: "append" };
    }
    // Handle reasoning part added (paragraph break)
    if (method === "item/reasoning/summaryPartAdded") {
        const event = {
            id: payload?.id || `${Date.now()}`,
            title: "Thinking",
            summary: "",
            detail: "",
            kind: "thinking",
            isSignificant: true,
            time: receivedAt,
            itemId,
            method,
        };
        return { event, mergeStrategy: "newline" };
    }
    if (method === "message.updated" || method === "message.completed") {
        const role = extractOpenCodeRole(params);
        const messageId = extractOpenCodeMessageId(params);
        if (messageId && role) {
            openCodeMessageRoles[messageId] = role;
            openCodeMessageRolesSeen = true;
        }
        if (role === "user") {
            if (messageId) {
                delete openCodePendingTextByMessage[messageId];
            }
            openCodePendingTextNoId = "";
            return null;
        }
        if (role === "assistant" && messageId && openCodePendingTextByMessage[messageId]) {
            const pendingText = openCodePendingTextByMessage[messageId];
            delete openCodePendingTextByMessage[messageId];
            return {
                event: {
                    id: payload?.id || `${Date.now()}`,
                    title: "Agent",
                    summary: pendingText,
                    detail: "",
                    kind: "output",
                    isSignificant: true,
                    time: receivedAt,
                    itemId: messageId,
                    method,
                },
            };
        }
        if (role === "assistant" && openCodePendingTextNoId) {
            const pendingText = openCodePendingTextNoId;
            openCodePendingTextNoId = "";
            return {
                event: {
                    id: payload?.id || `${Date.now()}`,
                    title: "Agent",
                    summary: pendingText,
                    detail: "",
                    kind: "output",
                    isSignificant: true,
                    time: receivedAt,
                    itemId: messageId,
                    method,
                },
            };
        }
        const text = extractOpenCodePartText(params) || extractOpenCodeDeltaText(params);
        if (!text) {
            return null;
        }
        const event = {
            id: payload?.id || `${Date.now()}`,
            title: "Agent",
            summary: text,
            detail: "",
            kind: "output",
            isSignificant: true,
            time: receivedAt,
            itemId: messageId,
            method,
        };
        return { event };
    }
    if (method === "message.part.updated") {
        const part = extractOpenCodePart(params);
        const partType = typeof part?.type === "string" ? part.type.trim().toLowerCase() : "";
        const openCodeMessageId = extractOpenCodeMessageId(params);
        const knownRole = openCodeMessageId ? openCodeMessageRoles[openCodeMessageId] : "";
        if (knownRole === "user") {
            return null;
        }
        if (!part || partType === "" || partType === "text") {
            const delta = extractOpenCodeDeltaText(params);
            const text = delta || extractOpenCodePartText(params);
            if (!text)
                return null;
            if (knownRole !== "assistant") {
                if (openCodeMessageId) {
                    openCodePendingTextByMessage[openCodeMessageId] = mergeOpenCodeText(openCodePendingTextByMessage[openCodeMessageId] || "", text);
                    return null;
                }
                if (openCodeMessageRolesSeen) {
                    openCodePendingTextNoId = mergeOpenCodeText(openCodePendingTextNoId, text);
                    return null;
                }
            }
            const parsedItemId = openCodeMessageId ||
                (typeof part?.id === "string" && part.id) ||
                itemId;
            const event = {
                id: payload?.id || `${Date.now()}`,
                title: "Agent",
                summary: text,
                detail: "",
                kind: "output",
                isSignificant: true,
                time: receivedAt,
                itemId: parsedItemId || null,
                method,
            };
            if (!parsedItemId)
                return { event };
            if (delta)
                return { event, mergeStrategy: "append" };
            return { event, mergeStrategy: "replace" };
        }
        if (partType === "reasoning") {
            const delta = extractOpenCodeDeltaText(params);
            const text = delta || extractOpenCodePartText(params);
            if (!text)
                return null;
            const reasoningItemId = (typeof part.id === "string" && part.id) ||
                openCodeMessageId ||
                itemId;
            const event = {
                id: payload?.id || `${Date.now()}`,
                title: "Thinking",
                summary: text,
                detail: "",
                kind: "thinking",
                isSignificant: true,
                time: receivedAt,
                itemId: reasoningItemId || null,
                method,
            };
            return delta && reasoningItemId ? { event, mergeStrategy: "append" } : { event };
        }
        if (partType === "tool") {
            const toolName = (typeof part.tool === "string" && part.tool) ||
                (typeof part.name === "string" && part.name) ||
                "Tool call";
            const input = extractOpenCodeToolInput(part);
            const state = asRecord(part.state);
            const status = typeof state?.status === "string" ? state.status.trim() : "";
            const exitCode = state?.exitCode;
            const error = asRecord(state?.error)?.message || state?.error;
            const detailParts = [];
            if (input)
                detailParts.push(input);
            if (status)
                detailParts.push(status);
            if (exitCode !== undefined && exitCode !== null)
                detailParts.push(`exit ${exitCode}`);
            if (typeof error === "string" && error.trim())
                detailParts.push(error.trim());
            return {
                event: {
                    id: payload?.id || `${Date.now()}`,
                    title: "Tool",
                    summary: toolName,
                    detail: detailParts.join(" · "),
                    kind: "tool",
                    isSignificant: true,
                    time: receivedAt,
                    itemId: ((typeof part.callID === "string" && part.callID) ||
                        (typeof part.id === "string" && part.id) ||
                        null),
                    method,
                },
            };
        }
        if (partType === "patch") {
            const files = extractFiles(part);
            if (!files.length)
                return null;
            return {
                event: {
                    id: payload?.id || `${Date.now()}`,
                    title: "File change",
                    summary: files.join(", "),
                    detail: "",
                    kind: "file",
                    isSignificant: true,
                    time: receivedAt,
                    itemId: (typeof part.id === "string" && part.id) || null,
                    method,
                },
            };
        }
        if (partType === "usage") {
            const totalTokens = part.totalTokens;
            const summaryText = typeof totalTokens === "number" ? `tokens used: ${totalTokens}` : "";
            if (!summaryText)
                return null;
            return {
                event: {
                    id: payload?.id || `${Date.now()}`,
                    title: "Status",
                    summary: summaryText,
                    detail: "",
                    kind: "status",
                    isSignificant: false,
                    time: receivedAt,
                    itemId: (typeof part.id === "string" && part.id) || null,
                    method,
                },
            };
        }
    }
    let title = method;
    let summary = "";
    let detail = "";
    let kind = "event";
    // Handle generic status updates
    if (method === "status" || params.status) {
        title = "Status";
        summary = params.status || "Processing";
        kind = "status";
    }
    else if (method === "item/completed") {
        const itemType = item.type;
        if (itemType === "commandExecution") {
            title = "Command";
            summary = extractCommand(item, params);
            kind = "command";
            if (item.exitCode !== undefined && item.exitCode !== null) {
                detail = `exit ${item.exitCode}`;
            }
        }
        else if (itemType === "fileChange") {
            title = "File change";
            const files = extractFiles(item);
            summary = files.join(", ") || "Updated files";
            kind = "file";
        }
        else if (itemType === "tool") {
            title = "Tool";
            summary =
                item.name ||
                    item.tool ||
                    item.id ||
                    "Tool call";
            kind = "tool";
        }
        else if (itemType === "agentMessage") {
            title = "Agent";
            summary = item.text || "Agent message";
            kind = "output";
        }
        else {
            title = itemType ? `Item ${itemType}` : "Item completed";
            summary = item.text || item.message || "";
        }
    }
    else if (method === "item/commandExecution/requestApproval") {
        title = "Command approval";
        summary = extractCommand(item, params) || "Approval requested";
        kind = "command";
    }
    else if (method === "item/fileChange/requestApproval") {
        title = "File approval";
        const files = extractFiles(params);
        summary = files.join(", ") || "Approval requested";
        kind = "file";
    }
    else if (method === "turn/completed") {
        title = "Turn completed";
        summary = params.status || "completed";
        kind = "status";
    }
    else if (method === "error") {
        title = "Error";
        summary = extractErrorMessage(params) || "App-server error";
        kind = "error";
    }
    else if (method.includes("outputDelta")) {
        title = "Output";
        summary = params.delta || params.text || "";
        kind = "output";
    }
    else if (params.delta) {
        title = "Delta";
        summary = params.delta;
    }
    const summaryText = typeof summary === "string" ? summary : String(summary ?? "");
    const detailText = typeof detail === "string" ? detail : String(detail ?? "");
    const meaningful = hasMeaningfulText(summaryText, detailText);
    const isStarted = method.includes("item/started");
    if (!meaningful && isStarted) {
        return null;
    }
    if (!meaningful) {
        return null;
    }
    const isSignificant = inferSignificance(kind, method);
    const event = {
        id: payload?.id || `${Date.now()}`,
        title,
        summary: summaryText,
        detail: detailText,
        kind,
        isSignificant,
        time: receivedAt,
        itemId,
        method,
    };
    return { event };
}
