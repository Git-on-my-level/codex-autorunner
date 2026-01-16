export function parseChatPayload(payload) {
  if (!payload) return { response: "" };
  if (typeof payload === "string") return { response: payload };
  if (payload.status && payload.status !== "ok") {
    if (payload.status === "interrupted") {
      return {
        interrupted: true,
        detail: payload.detail || "Doc chat interrupted",
      };
    }
    return { error: payload.detail || "Doc chat failed" };
  }
  return {
    response:
      payload.response ||
      payload.message ||
      payload.agent_message ||
      payload.agentMessage ||
      payload.content ||
      "",
    content: payload.content || "",
    patch: payload.patch || "",
    drafts: normalizeDraftMap(payload.drafts || payload.draft),
    updated: Array.isArray(payload.updated)
      ? payload.updated.filter((entry) => typeof entry === "string")
      : [],
    createdAt: payload.created_at || payload.createdAt || "",
    baseHash: payload.base_hash || payload.baseHash || "",
    agentMessage: payload.agent_message || payload.agentMessage || "",
  };
}

export function parseSpecIngestPayload(payload) {
  if (!payload || typeof payload !== "object") {
    return { error: "Spec ingest failed" };
  }
  if (payload.status && payload.status !== "ok") {
    if (payload.status === "interrupted") {
      return {
        interrupted: true,
        todo: payload.todo || "",
        progress: payload.progress || "",
        opinions: payload.opinions || "",
        spec: payload.spec || "",
        summary: payload.summary || "",
        patch: payload.patch || "",
        agentMessage: payload.agent_message || payload.agentMessage || "",
      };
    }
    return { error: payload.detail || "Spec ingest failed" };
  }
  return {
    todo: payload.todo || "",
    progress: payload.progress || "",
    opinions: payload.opinions || "",
    spec: payload.spec || "",
    summary: payload.summary || "",
    patch: payload.patch || "",
    agentMessage: payload.agent_message || payload.agentMessage || "",
  };
}

export function parseMaybeJson(raw) {
  try {
    return JSON.parse(raw);
  } catch (err) {
    if (typeof raw === "string" && raw.includes("\n")) {
      try {
        return JSON.parse(raw.replace(/\n/g, "\\n"));
      } catch (_retryErr) {
        // fall through
      }
    }
    if (typeof raw === "string" && raw.includes("\\n")) {
      try {
        return JSON.parse(raw.replace(/\\n/g, "\\\\n"));
      } catch (_retryErr) {
        // fall through
      }
    }
    return raw;
  }
}

export function recoverDraftMap(raw) {
  if (typeof raw !== "string" || !raw.includes("\"drafts\"")) return null;
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
      if (Object.keys(drafts).length) return drafts;
    } catch (_err) {
      // try next candidate
    }
  }
  return null;
}

export function recoverPatchFromRaw(raw) {
  if (typeof raw !== "string" || !raw.includes("\"patch\"")) return null;
  const match = raw.match(/"patch"\s*:\s*"([\s\S]*?)"(?:,|\})/);
  if (!match) return null;
  return match[1]
    .replace(/\\\\n/g, "\n")
    .replace(/\\"/g, "\"")
    .replace(/\\\\/g, "\\");
}

export function normalizeDraftPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const content = typeof payload.content === "string" ? payload.content : "";
  const patch = typeof payload.patch === "string" ? payload.patch : "";
  if (!content && !patch) return null;
  return {
    content,
    patch,
    agentMessage:
      typeof payload.agent_message === "string"
        ? payload.agent_message
        : typeof payload.agentMessage === "string"
        ? payload.agentMessage
        : "",
    createdAt:
      typeof payload.created_at === "string"
        ? payload.created_at
        : typeof payload.createdAt === "string"
        ? payload.createdAt
        : "",
    baseHash:
      typeof payload.base_hash === "string"
        ? payload.base_hash
        : typeof payload.baseHash === "string"
        ? payload.baseHash
        : "",
  };
}

export function normalizeDraftMap(raw) {
  if (!raw || typeof raw !== "object") return {};
  const drafts = {};
  Object.entries(raw).forEach(([kind, entry]) => {
    const normalized = normalizeDraftPayload(entry);
    if (normalized) drafts[kind] = normalized;
  });
  return drafts;
}
