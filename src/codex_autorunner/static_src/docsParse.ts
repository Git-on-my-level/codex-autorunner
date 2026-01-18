export function parseChatPayload(payload: unknown): {
  response: string;
  content: string;
  patch: string;
  drafts: Record<string, unknown>;
  updated: string[];
  createdAt: string;
  baseHash: string;
  agentMessage: string;
  error?: string;
  interrupted?: boolean;
} {
  if (!payload) return { response: "", content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: "" };
  if (typeof payload === "string") return { response: payload, content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: "" };
  const p = payload as Record<string, unknown>;
  if (p.status && p.status !== "ok") {
    if (p.status === "interrupted") {
      return {
        interrupted: true,
        detail: (p.detail as string | undefined) || "Doc chat interrupted",
        response: "", content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: ""
      } as any;
    }
    return { error: (p.detail as string | undefined) || "Doc chat failed", response: "", content: "", patch: "", drafts: {}, updated: [], createdAt: "", baseHash: "", agentMessage: "" };
  }
  return {
    response:
      (p.response as string | undefined) ||
      (p.message as string | undefined) ||
      (p.agent_message as string | undefined) ||
      (p.agentMessage as string | undefined) ||
      (p.content as string | undefined) ||
      "",
    content: (p.content as string | undefined) || "",
    patch: (p.patch as string | undefined) || "",
    drafts: normalizeDraftMap(p.drafts || p.draft),
    updated: Array.isArray(p.updated)
      ? (p.updated as unknown[]).filter((entry) => typeof entry === "string")
      : [],
    createdAt: (p.created_at as string | undefined) || (p.createdAt as string | undefined) || "",
    baseHash: (p.base_hash as string | undefined) || (p.baseHash as string | undefined) || "",
    agentMessage: (p.agent_message as string | undefined) || (p.agentMessage as string | undefined) || "",
  };
}

export function parseSpecIngestPayload(payload: unknown): {
  todo: string;
  progress: string;
  opinions: string;
  spec: string;
  summary: string;
  patch: string;
  agentMessage: string;
  error?: string;
  interrupted?: boolean;
} {
  if (!payload || typeof payload !== "object") {
    return { error: "Spec ingest failed", todo: "", progress: "", opinions: "", spec: "", summary: "", patch: "", agentMessage: "" };
  }
  const p = payload as Record<string, unknown>;
  if (p.status && p.status !== "ok") {
    if (p.status === "interrupted") {
      return {
        interrupted: true,
        todo: (p.todo as string | undefined) || "",
        progress: (p.progress as string | undefined) || "",
        opinions: (p.opinions as string | undefined) || "",
        spec: (p.spec as string | undefined) || "",
        summary: (p.summary as string | undefined) || "",
        patch: (p.patch as string | undefined) || "",
        agentMessage: (p.agent_message as string | undefined) || (p.agentMessage as string | undefined) || "",
      };
    }
    return { error: (p.detail as string | undefined) || "Spec ingest failed", todo: "", progress: "", opinions: "", spec: "", summary: "", patch: "", agentMessage: "" };
  }
  return {
    todo: (p.todo as string | undefined) || "",
    progress: (p.progress as string | undefined) || "",
    opinions: (p.opinions as string | undefined) || "",
    spec: (p.spec as string | undefined) || "",
    summary: (p.summary as string | undefined) || "",
    patch: (p.patch as string | undefined) || "",
    agentMessage: (p.agent_message as string | undefined) || (p.agentMessage as string | undefined) || "",
  };
}

export function parseMaybeJson(raw: string): unknown {
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

export function recoverDraftMap(raw: string): Record<string, unknown> | null {
  if (typeof raw !== "string" || !raw.includes("\"drafts\"")) return null;
  const candidates = [
    raw,
    raw.replace(/\n/g, "\\n"),
    raw.replace(/\\n/g, "\n"),
    raw.replace(/\\n/g, "\\\\n"),
  ];
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as Record<string, unknown>;
      const drafts = normalizeDraftMap(parsed.drafts || parsed.draft);
      if (Object.keys(drafts).length) return drafts;
    } catch (_err) {
      // try next candidate
    }
  }
  return null;
}

export function recoverPatchFromRaw(raw: string): string | null {
  if (typeof raw !== "string" || !raw.includes("\"patch\"")) return null;
  const match = raw.match(/"patch"\s*:\s*"([\s\S]*?)"(?:,|\})/);
  if (!match) return null;
  return match[1]
    .replace(/\\\\n/g, "\n")
    .replace(/\\"/g, "\"")
    .replace(/\\\\/g, "\\");
}

export interface DraftPayload {
  content: string;
  patch: string;
  agentMessage: string;
  createdAt: string;
  baseHash: string;
}

export function normalizeDraftPayload(payload: unknown): DraftPayload | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Record<string, unknown>;
  const content = typeof p.content === "string" ? p.content : "";
  const patch = typeof p.patch === "string" ? p.patch : "";
  if (!content && !patch) return null;
  return {
    content,
    patch,
    agentMessage:
      typeof p.agent_message === "string"
        ? p.agent_message
        : typeof p.agentMessage === "string"
        ? p.agentMessage
        : "",
    createdAt:
      typeof p.created_at === "string"
        ? p.created_at
        : typeof p.createdAt === "string"
        ? p.createdAt
        : "",
    baseHash:
      typeof p.base_hash === "string"
        ? p.base_hash
        : typeof p.baseHash === "string"
        ? p.baseHash
        : "",
  };
}

export function normalizeDraftMap(raw: unknown): Record<string, DraftPayload> {
  if (!raw || typeof raw !== "object") return {};
  const drafts: Record<string, DraftPayload> = {};
  Object.entries(raw as Record<string, unknown>).forEach(([kind, entry]) => {
    const normalized = normalizeDraftPayload(entry);
    if (normalized) drafts[kind] = normalized;
  });
  return drafts;
}
