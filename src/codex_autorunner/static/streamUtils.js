// GENERATED FILE - do not edit directly. Source: static_src/
const decoder = new TextDecoder();
export function parseMaybeJson(data) {
    try {
        return JSON.parse(data);
    }
    catch {
        return data;
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
export async function readEventStream(res, handler) {
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
            handler(event, dataLines.join("\n"));
        }
    }
}
