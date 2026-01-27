// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Shared parsing helpers for agent (app-server) events.
 * Used by ticket chat and live agent output to render rich activity.
 */
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
function extractFiles(payload) {
    const files = [];
    const addEntry = (entry) => {
        if (typeof entry === "string" && entry.trim()) {
            files.push(entry.trim());
            return;
        }
        if (entry && typeof entry === "object") {
            const entryObj = entry;
            const path = entryObj.path || entryObj.file || entryObj.name;
            if (typeof path === "string" && path.trim()) {
                files.push(path.trim());
            }
        }
    };
    if (!payload || typeof payload !== "object")
        return files;
    for (const key of ["files", "fileChanges", "paths"]) {
        const value = payload[key];
        if (Array.isArray(value)) {
            value.forEach(addEntry);
        }
    }
    for (const key of ["path", "file", "name"]) {
        addEntry(payload[key]);
    }
    return files;
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
    if (!method.includes("outputdelta"))
        return "";
    const params = message.params || {};
    if (typeof params.delta === "string")
        return params.delta;
    if (typeof params.text === "string")
        return params.text;
    if (typeof params.output === "string")
        return params.output;
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
