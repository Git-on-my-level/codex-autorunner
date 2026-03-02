// GENERATED FILE - do not edit directly. Source: static_src/
import { resolvePath, getAuthToken, api } from "./utils.js";
import { extractContextRemainingPercent, readEventStream, parseMaybeJson } from "./streamUtils.js";
export async function sendFileChat(target, message, controller, handlers = {}, options = {}) {
    const endpoint = resolvePath(options.basePath || "/api/file-chat");
    const headers = {
        "Content-Type": "application/json",
    };
    const token = getAuthToken();
    if (token)
        headers.Authorization = `Bearer ${token}`;
    const payload = {
        target,
        message,
        stream: true,
    };
    if (options.clientTurnId)
        payload.client_turn_id = options.clientTurnId;
    if (options.agent)
        payload.agent = options.agent;
    if (options.model)
        payload.model = options.model;
    if (options.reasoning)
        payload.reasoning = options.reasoning;
    const res = await fetch(endpoint, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
        signal: controller.signal,
    });
    if (!res.ok) {
        const text = await res.text();
        let detail = text;
        try {
            const parsed = JSON.parse(text);
            detail =
                parsed.detail || parsed.error || parsed.message || text;
        }
        catch {
            // ignore
        }
        throw new Error(detail || `Request failed (${res.status})`);
    }
    const contentType = res.headers.get("content-type") || "";
    if (contentType.includes("text/event-stream")) {
        await readFileChatStream(res, handlers);
    }
    else {
        const responsePayload = contentType.includes("application/json") ? await res.json() : await res.text();
        handlers.onUpdate?.(responsePayload);
        handlers.onDone?.();
    }
}
async function readFileChatStream(res, handlers) {
    await readEventStream(res, (event, raw) => handleStreamEvent(event, raw, handlers));
}
function handleStreamEvent(event, rawData, handlers) {
    const parsed = parseMaybeJson(rawData);
    switch (event) {
        case "status": {
            const status = typeof parsed === "string" ? parsed : parsed.status || "";
            handlers.onStatus?.(status);
            break;
        }
        case "token": {
            const token = typeof parsed === "string"
                ? parsed
                : parsed.token || parsed.text || rawData || "";
            handlers.onToken?.(token);
            break;
        }
        case "token_usage": {
            if (typeof parsed === "object" && parsed !== null) {
                const usage = parsed;
                const percent = extractContextRemainingPercent(usage);
                if (percent !== null) {
                    handlers.onTokenUsage?.(percent, usage);
                }
            }
            break;
        }
        case "update": {
            handlers.onUpdate?.(parsed);
            break;
        }
        case "event":
        case "app-server": {
            handlers.onEvent?.(parsed);
            break;
        }
        case "error": {
            const msg = typeof parsed === "object" && parsed !== null
                ? (parsed.detail || parsed.error || rawData || "File chat failed")
                : rawData || "File chat failed";
            handlers.onError?.(msg);
            break;
        }
        case "interrupted": {
            const msg = typeof parsed === "object" && parsed !== null
                ? (parsed.detail || rawData || "File chat interrupted")
                : rawData || "File chat interrupted";
            handlers.onInterrupted?.(msg);
            break;
        }
        case "done":
        case "finish": {
            handlers.onDone?.();
            break;
        }
        default:
            // treat unknown as event for visibility
            handlers.onEvent?.(parsed);
            break;
    }
}
export async function fetchPendingDraft(target) {
    try {
        const res = (await api(`/api/file-chat/pending?target=${encodeURIComponent(target)}`));
        if (!res || typeof res !== "object")
            return null;
        return {
            target: res.target || target,
            content: res.content || "",
            patch: res.patch || "",
            agent_message: res.agent_message || undefined,
            created_at: res.created_at || undefined,
            base_hash: res.base_hash || undefined,
            current_hash: res.current_hash || undefined,
            is_stale: Boolean(res.is_stale),
        };
    }
    catch {
        return null;
    }
}
export async function applyDraft(target, options = {}) {
    const res = (await api("/api/file-chat/apply", {
        method: "POST",
        body: { target, force: Boolean(options.force) },
    }));
    return {
        content: res.content || "",
        agent_message: res.agent_message || undefined,
    };
}
export async function discardDraft(target) {
    const res = (await api("/api/file-chat/discard", {
        method: "POST",
        body: { target },
    }));
    return {
        content: res.content || "",
    };
}
export async function interruptFileChat(target) {
    await api("/api/file-chat/interrupt", { method: "POST", body: { target } });
}
export function newClientTurnId(prefix = "filechat") {
    try {
        if (typeof crypto !== "undefined" && "randomUUID" in crypto && typeof crypto.randomUUID === "function") {
            return crypto.randomUUID();
        }
    }
    catch {
        // ignore
    }
    return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
export async function fetchActiveFileChat(clientTurnId, basePath = "/api/file-chat/active") {
    const suffix = clientTurnId ? `?client_turn_id=${encodeURIComponent(clientTurnId)}` : "";
    const path = `${basePath}${suffix}`;
    try {
        const res = (await api(path));
        return res || {};
    }
    catch {
        return {};
    }
}
export function streamTurnEvents(meta, handlers = {}) {
    if (!meta.threadId || !meta.turnId)
        return null;
    const ctrl = new AbortController();
    const token = getAuthToken();
    const headers = {};
    if (token)
        headers.Authorization = `Bearer ${token}`;
    const url = resolvePath(`${meta.basePath || "/api/file-chat/turns"}/${encodeURIComponent(meta.turnId)}/events?thread_id=${encodeURIComponent(meta.threadId)}&agent=${encodeURIComponent(meta.agent || "codex")}`);
    void (async () => {
        try {
            const res = await fetch(url, { method: "GET", headers, signal: ctrl.signal });
            if (!res.ok) {
                handlers.onError?.("Failed to stream events");
                return;
            }
            const contentType = res.headers.get("content-type") || "";
            if (!contentType.includes("text/event-stream"))
                return;
            await readEventStream(res, (event, raw) => {
                if (event === "app-server" || event === "event") {
                    handlers.onEvent?.(parseMaybeJson(raw));
                }
            });
        }
        catch (err) {
            handlers.onError?.(err.message || "Event stream failed");
        }
    })();
    return ctrl;
}
