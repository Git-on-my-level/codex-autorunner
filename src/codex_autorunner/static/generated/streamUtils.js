const decoder = new TextDecoder();
export function parseMaybeJson(data) {
    try {
        return JSON.parse(data);
    }
    catch {
        return data;
    }
}
export function handleStreamEvent(event, rawData, handlers) {
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
            const payload = typeof parsed === "object" && parsed !== null ? parsed : {};
            handlers.onUpdate?.(payload);
            break;
        }
        case "event":
        case "app-server": {
            handlers.onEvent?.(parsed);
            break;
        }
        case "error": {
            const message = typeof parsed === "object" && parsed !== null
                ? (parsed.detail || parsed.error || rawData || "Stream error")
                : rawData || "Stream error";
            handlers.onError?.(message);
            break;
        }
        case "interrupted": {
            const message = typeof parsed === "object" && parsed !== null
                ? (parsed.detail || rawData || "Stream interrupted")
                : rawData || "Stream interrupted";
            handlers.onInterrupted?.(message);
            break;
        }
        case "done":
        case "finish": {
            handlers.onDone?.();
            break;
        }
        default: {
            if (typeof parsed === "object" && parsed !== null) {
                const messageObj = parsed;
                if (messageObj.method || messageObj.message) {
                    handlers.onEvent?.(parsed);
                }
            }
            break;
        }
    }
}
export function extractContextRemainingPercent(usage) {
    if (!usage || typeof usage !== "object")
        return null;
    const payload = usage;
    const totalRaw = payload.totalTokens ?? payload.total ?? payload.total_tokens;
    const contextRaw = payload.modelContextWindow ?? payload.contextWindow ?? payload.model_context_window;
    const totalTokens = typeof totalRaw === "number" ? totalRaw : Number(totalRaw);
    const contextWindow = typeof contextRaw === "number" && Number.isFinite(contextRaw)
        ? contextRaw
        : Number(contextRaw);
    if (!Number.isFinite(totalTokens) || !Number.isFinite(contextWindow) || contextWindow <= 0) {
        return null;
    }
    const percentRemaining = Math.round(((contextWindow - totalTokens) / contextWindow) * 100);
    return Math.max(0, Math.min(100, percentRemaining));
}
const ESCAPED_NEWLINE_RE = /\\n(?=event:|data:|\\n)/g;
function normalizeEscapedNewlines(buffer, detected) {
    if (!detected) {
        if (!buffer.includes("\n") && buffer.includes("\\n")) {
            return { text: buffer.replace(ESCAPED_NEWLINE_RE, "\n"), detected: true };
        }
        return { text: buffer, detected: false };
    }
    return { text: buffer.replace(ESCAPED_NEWLINE_RE, "\n"), detected: true };
}
function parseSseChunk(chunk) {
    let event = "message";
    const dataLines = [];
    chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
            event = line.slice(6).trim();
        }
        else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trimStart());
        }
        else if (line.trim()) {
            dataLines.push(line);
        }
    });
    if (!dataLines.length)
        return null;
    return { event, data: dataLines.join("\n") };
}
export async function readEventStream(res, handler, options = {}) {
    if (!res.body)
        throw new Error("Streaming not supported in this browser");
    const reader = res.body.getReader();
    let buffer = "";
    let escapedNewlinesDetected = false;
    for (;;) {
        const { value, done } = await reader.read();
        if (done)
            break;
        const decoded = decoder.decode(value, { stream: true });
        if (options.handleEscapedNewlines) {
            const prev = buffer;
            const norm = normalizeEscapedNewlines(prev, escapedNewlinesDetected);
            escapedNewlinesDetected = norm.detected;
            buffer = norm.text;
            const decodedNorm = normalizeEscapedNewlines(decoded, escapedNewlinesDetected);
            escapedNewlinesDetected = decodedNorm.detected;
            buffer += decodedNorm.text;
        }
        else {
            buffer += decoded;
        }
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() || "";
        for (const chunk of chunks) {
            if (!chunk.trim())
                continue;
            const parsed = parseSseChunk(chunk);
            if (parsed)
                handler(parsed.event, parsed.data);
        }
    }
    if (buffer.trim()) {
        if (options.handleEscapedNewlines && !escapedNewlinesDetected) {
            const norm = normalizeEscapedNewlines(buffer, false);
            buffer = norm.text;
        }
        const parsed = parseSseChunk(buffer);
        if (parsed)
            handler(parsed.event, parsed.data);
    }
}
