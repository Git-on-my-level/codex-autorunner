// GENERATED FILE - do not edit directly. Source: static_src/
import { resolvePath, getAuthToken, api } from "./utils.js";
const decoder = new TextDecoder();
function parseMaybeJson(data) {
    try {
        return JSON.parse(data);
    }
    catch {
        return data;
    }
}
export async function sendFileChat(target, message, controller, handlers = {}, options = {}) {
    const endpoint = resolvePath("/api/file-chat");
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
    if (!res.body)
        throw new Error("Streaming not supported in this browser");
    const reader = res.body.getReader();
    let buffer = "";
    for (;;) {
        const { value, done } = await reader.read();
        if (done)
            break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() || "";
        for (const chunk of chunks) {
            if (!chunk.trim())
                continue;
            let event = "message";
            const dataLines = [];
            chunk.split("\n").forEach((line) => {
                if (line.startsWith("event:")) {
                    event = line.slice(6).trim();
                }
                else if (line.startsWith("data:")) {
                    dataLines.push(line.slice(5).trimStart());
                }
            });
            if (!dataLines.length)
                continue;
            const rawData = dataLines.join("\n");
            handleStreamEvent(event, rawData, handlers);
        }
    }
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
