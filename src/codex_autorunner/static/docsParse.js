// GENERATED FILE - do not edit directly. Source: static_src/
export function parseChatPayload(payload) {
    if (!payload)
        return { response: "", content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: "" };
    if (typeof payload === "string")
        return { response: payload, content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: "" };
    const p = payload;
    if (p.status && p.status !== "ok") {
        if (p.status === "interrupted") {
            return {
                interrupted: true,
                detail: p.detail || "Doc chat interrupted",
                response: "", content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: ""
            };
        }
        return { error: p.detail || "Doc chat failed", response: "", content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: "" };
    }
    return {
        response: p.response ||
            p.message ||
            p.agent_message ||
            p.agentMessage ||
            p.content ||
            "",
        content: p.content || "",
        patch: p.patch || "",
        drafts: normalizeDraftMap(p.drafts || p.draft),
        updated: Array.isArray(p.updated)
            ? p.updated.filter((entry) => typeof entry === "string")
            : [],
        createdAt: p.created_at || p.createdAt || "",
        baseHash: p.base_hash || p.baseHash || "",
        agentMessage: p.agent_message || p.agentMessage || "",
    };
}
export function parseSpecIngestPayload(payload) {
    if (!payload || typeof payload !== "object") {
        return { error: "Spec ingest failed", todo: "", progress: "", opinions: "", spec: "", summary: "", patch: "", agentMessage: "" };
    }
    const p = payload;
    if (p.status && p.status !== "ok") {
        if (p.status === "interrupted") {
            return {
                interrupted: true,
                todo: p.todo || "",
                progress: p.progress || "",
                opinions: p.opinions || "",
                spec: p.spec || "",
                summary: p.summary || "",
                patch: p.patch || "",
                agentMessage: p.agent_message || p.agentMessage || "",
            };
        }
        return { error: p.detail || "Spec ingest failed", todo: "", progress: "", opinions: "", spec: "", summary: "", patch: "", agentMessage: "" };
    }
    return {
        todo: p.todo || "",
        progress: p.progress || "",
        opinions: p.opinions || "",
        spec: p.spec || "",
        summary: p.summary || "",
        patch: p.patch || "",
        agentMessage: p.agent_message || p.agentMessage || "",
    };
}
export function parseMaybeJson(raw) {
    try {
        return JSON.parse(raw);
    }
    catch (err) {
        if (typeof raw === "string" && raw.includes("\n")) {
            try {
                return JSON.parse(raw.replace(/\n/g, "\\n"));
            }
            catch (_retryErr) {
                // fall through
            }
        }
        if (typeof raw === "string" && raw.includes("\\n")) {
            try {
                return JSON.parse(raw.replace(/\\n/g, "\\\\n"));
            }
            catch (_retryErr) {
                // fall through
            }
        }
        return raw;
    }
}
export function recoverDraftMap(raw) {
    if (typeof raw !== "string" || !raw.includes("\"drafts\""))
        return null;
    const candidates = [
        raw,
        raw.replace(/\n/g, "\\n"),
        raw.replace(/\\n/g, "\n"),
        raw.replace(/\\n/g, "\\\\n"),
    ];
    for (const candidate of candidates) {
        try {
            const parsed = JSON.parse(candidate);
            const drafts = normalizeDraftMap(parsed.drafts || parsed.draft);
            if (Object.keys(drafts).length)
                return drafts;
        }
        catch (_err) {
            // try next candidate
        }
    }
    return null;
}
export function recoverPatchFromRaw(raw) {
    if (typeof raw !== "string" || !raw.includes("\"patch\""))
        return null;
    const match = raw.match(/"patch"\s*:\s*"([\s\S]*?)"(?:,|\})/);
    if (!match)
        return null;
    return match[1]
        .replace(/\\\\n/g, "\n")
        .replace(/\\"/g, "\"")
        .replace(/\\\\/g, "\\");
}
export function normalizeDraftPayload(payload) {
    if (!payload || typeof payload !== "object")
        return null;
    const p = payload;
    const content = typeof p.content === "string" ? p.content : "";
    const patch = typeof p.patch === "string" ? p.patch : "";
    if (!content && !patch)
        return null;
    return {
        content,
        patch,
        agentMessage: typeof p.agent_message === "string"
            ? p.agent_message
            : typeof p.agentMessage === "string"
                ? p.agentMessage
                : "",
        createdAt: typeof p.created_at === "string"
            ? p.created_at
            : typeof p.createdAt === "string"
                ? p.createdAt
                : "",
        baseHash: typeof p.base_hash === "string"
            ? p.base_hash
            : typeof p.baseHash === "string"
                ? p.baseHash
                : "",
    };
}
export function normalizeDraftMap(raw) {
    if (!raw || typeof raw !== "object")
        return {};
    const drafts = {};
    Object.entries(raw).forEach(([kind, entry]) => {
        const normalized = normalizeDraftPayload(entry);
        if (normalized)
            drafts[kind] = normalized;
    });
    return drafts;
}
