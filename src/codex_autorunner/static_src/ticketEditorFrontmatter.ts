import { api } from "./utils.js";
import {
  ensureAgentCatalog,
  getRegisteredAgents,
  getRegisteredAgentProfiles,
} from "./agentControls.js";

export type FrontmatterState = {
  agent: string;
  done: boolean;
  ticketId: string;
  title: string;
  model: string;
  reasoning: string;
  profile: string;
};

export const DEFAULT_FRONTMATTER: FrontmatterState = {
  agent: "codex",
  done: false,
  ticketId: "",
  title: "",
  model: "",
  reasoning: "",
  profile: "",
};

export type TicketData = {
  path?: string;
  index?: number | null;
  chat_key?: string | null;
  frontmatter?: Record<string, unknown> | null;
  body?: string | null;
  errors?: string[];
};

function yamlQuote(value: string): string {
  return JSON.stringify(value ?? "");
}

function formatFrontmatterAgentLabel(agent: {
  id: string;
  name?: string;
}, currentOnly: boolean = false): string {
  const base = agent.name && agent.name !== agent.id
    ? `${agent.name} (${agent.id})`
    : agent.id;
  return currentOnly ? `${base} (current)` : base;
}

function formatFrontmatterProfileLabel(
  profile: { id: string; display_name?: string },
  currentOnly: boolean = false
): string {
  const base =
    profile.display_name && profile.display_name !== profile.id
      ? `${profile.display_name} (${profile.id})`
      : profile.id;
  return currentOnly ? `${base} (current)` : base;
}

export function isHermesAliasAgentId(agentId: string): boolean {
  const normalized = (agentId || "").trim().toLowerCase();
  if (!normalized || normalized === "hermes") return false;
  return normalized.startsWith("hermes-") || normalized.startsWith("hermes_");
}

export function sameUndoSnapshot(
  previous: { body: string; frontmatter: FrontmatterState } | undefined,
  next: { body: string; frontmatter: FrontmatterState }
): boolean {
  if (!previous) return false;
  return (
    previous.body === next.body &&
    previous.frontmatter.agent === next.frontmatter.agent &&
    previous.frontmatter.done === next.frontmatter.done &&
    previous.frontmatter.ticketId === next.frontmatter.ticketId &&
    previous.frontmatter.title === next.frontmatter.title &&
    previous.frontmatter.model === next.frontmatter.model &&
    previous.frontmatter.reasoning === next.frontmatter.reasoning &&
    previous.frontmatter.profile === next.frontmatter.profile
  );
}

export function extractFrontmatter(ticket: TicketData): FrontmatterState {
  const fm = ticket.frontmatter || {};
  const extra =
    typeof fm.extra === "object" && fm.extra ? (fm.extra as Record<string, unknown>) : {};
  const ticketId =
    (fm.ticket_id as string) || (extra.ticket_id as string) || "";
  return {
    agent: (fm.agent as string) || "codex",
    done: Boolean(fm.done),
    ticketId,
    title: (fm.title as string) || "",
    model: (fm.model as string) || "",
    reasoning: (fm.reasoning as string) || "",
    profile: (fm.profile as string) || "",
  };
}

export function getFrontmatterFromForm(
  els: () => {
    fmAgent: HTMLSelectElement | null;
    fmModel: HTMLSelectElement | null;
    fmReasoning: HTMLSelectElement | null;
    fmProfile: HTMLSelectElement | null;
    fmDone: HTMLInputElement | null;
    fmTitle: HTMLInputElement | null;
  },
  lastSavedFrontmatter: FrontmatterState,
  originalFrontmatter: FrontmatterState
): FrontmatterState {
  const { fmAgent, fmModel, fmReasoning, fmProfile, fmDone, fmTitle } = els();
  return {
    agent: fmAgent?.value || "codex",
    done: fmDone?.checked || false,
    ticketId:
      lastSavedFrontmatter.ticketId ||
      originalFrontmatter.ticketId ||
      "",
    title: fmTitle?.value || "",
    model: fmModel?.value || "",
    reasoning: fmReasoning?.value || "",
    profile: fmProfile?.value || "",
  };
}

export function setFrontmatterForm(
  els: () => {
    fmAgent: HTMLSelectElement | null;
    fmModel: HTMLSelectElement | null;
    fmReasoning: HTMLSelectElement | null;
    fmProfile: HTMLSelectElement | null;
    fmDone: HTMLInputElement | null;
    fmTitle: HTMLInputElement | null;
  },
  fm: FrontmatterState
): void {
  const { fmAgent, fmModel, fmReasoning, fmProfile, fmDone, fmTitle } = els();
  if (fmAgent) fmAgent.value = fm.agent;
  if (fmModel) fmModel.value = fm.model;
  if (fmReasoning) fmReasoning.value = fm.reasoning;
  if (fmProfile) fmProfile.value = fm.profile;
  if (fmDone) fmDone.checked = fm.done;
  if (fmTitle) fmTitle.value = fm.title;
}

export function renderFmAgentOptions(
  els: () => { fmAgent: HTMLSelectElement | null },
  selectedAgent: string
): string {
  const { fmAgent } = els();
  if (!fmAgent) return selectedAgent || "codex";

  const agents = getRegisteredAgents().filter(
    (agent) => !isHermesAliasAgentId(agent.id)
  );
  const hasCatalogAgents =
    agents.length > 1 || (agents[0]?.id && agents[0].id !== "codex");
  if (!hasCatalogAgents && fmAgent.options.length > 1) {
    fmAgent.value = Array.from(fmAgent.options).some(
      (option) => option.value === selectedAgent
    )
      ? selectedAgent
      : fmAgent.value || "codex";
    return fmAgent.value || "codex";
  }

  const currentMissing =
    Boolean(selectedAgent) && !agents.some((agent) => agent.id === selectedAgent);
  const nextValue = agents.some((agent) => agent.id === selectedAgent)
    ? selectedAgent
    : agents[0]?.id || "codex";
  fmAgent.innerHTML = "";
  for (const agent of agents) {
    const option = document.createElement("option");
    option.value = agent.id;
    option.textContent = formatFrontmatterAgentLabel(agent);
    fmAgent.appendChild(option);
  }
  if (currentMissing) {
    const option = document.createElement("option");
    option.value = selectedAgent;
    option.textContent = formatFrontmatterAgentLabel({ id: selectedAgent }, true);
    fmAgent.appendChild(option);
  }
  fmAgent.value = nextValue;
  if (currentMissing) {
    fmAgent.value = selectedAgent;
  }
  return fmAgent.value || nextValue;
}

export function renderFmProfileOptions(
  els: () => { fmProfile: HTMLSelectElement | null },
  agent: string,
  currentProfile: string
): void {
  const { fmProfile } = els();
  if (!fmProfile) return;

  const normalizedCurrent = currentProfile.trim();
  const profiles = getRegisteredAgentProfiles(agent);
  const currentMissing =
    Boolean(normalizedCurrent) &&
    !profiles.some((profile) => profile.id === normalizedCurrent);

  fmProfile.innerHTML = "";

  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = profiles.length || normalizedCurrent
    ? "Default profile"
    : "No profiles";
  fmProfile.appendChild(defaultOption);

  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = formatFrontmatterProfileLabel(profile);
    fmProfile.appendChild(option);
  }

  if (currentMissing) {
    const option = document.createElement("option");
    option.value = normalizedCurrent;
    option.textContent = formatFrontmatterProfileLabel(
      { id: normalizedCurrent },
      true
    );
    fmProfile.appendChild(option);
  }

  const shouldShow = profiles.length > 0 || Boolean(normalizedCurrent);
  fmProfile.classList.toggle("hidden", !shouldShow);
  fmProfile.disabled = !shouldShow;
  fmProfile.value = normalizedCurrent;
  if (fmProfile.value !== normalizedCurrent) {
    fmProfile.value = "";
  }
}

export function refreshFrontmatterAgentProfileControls(
  els: () => { fmAgent: HTMLSelectElement | null; fmProfile: HTMLSelectElement | null },
  agent: string,
  profile: string
): void {
  const selectedAgent = renderFmAgentOptions(els, agent);
  renderFmProfileOptions(els, selectedAgent, profile);
}

export async function syncFrontmatterAgentProfileControls(
  els: () => { fmAgent: HTMLSelectElement | null; fmProfile: HTMLSelectElement | null },
  agent: string,
  profile: string
): Promise<void> {
  refreshFrontmatterAgentProfileControls(els, agent, profile);
  try {
    await ensureAgentCatalog();
  } catch {
    return;
  }
  refreshFrontmatterAgentProfileControls(els, agent, profile);
}

const fmModelCatalogs = new Map<string, { default_model: string; models: Array<{ id: string; display_name?: string; supports_reasoning: boolean; reasoning_options: string[] }> } | null>();

export async function refreshFmModelOptions(
  els: () => { fmModel: HTMLSelectElement | null; fmReasoning: HTMLSelectElement | null },
  agent: string,
  preserveSelection: boolean = false
): Promise<void> {
  const { fmModel, fmReasoning } = els();
  if (!fmModel || !fmReasoning) return;

  const currentModel = preserveSelection ? fmModel.value : "";
  const currentReasoning = preserveSelection ? fmReasoning.value : "";

  if (!fmModelCatalogs.has(agent)) {
    try {
      const data = await api(`/api/agents/${encodeURIComponent(agent)}/models`, { method: "GET" }) as Record<string, unknown>;
      const models = Array.isArray(data?.models) ? data.models as Array<{ id: string; display_name?: string; supports_reasoning: boolean; reasoning_options: string[] }> : [];
      const catalog = {
        default_model: (data?.default_model as string) || "",
        models,
      };
      fmModelCatalogs.set(agent, catalog);
    } catch {
      fmModelCatalogs.set(agent, null);
    }
  }

  const catalog = fmModelCatalogs.get(agent);

  fmModel.innerHTML = "";
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "(default)";
  fmModel.appendChild(defaultOption);

  if (catalog?.models?.length) {
    fmModel.disabled = false;
    for (const m of catalog.models) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.display_name && m.display_name !== m.id ? `${m.display_name} (${m.id})` : m.id;
      fmModel.appendChild(opt);
    }
    if (currentModel && catalog.models.some((m) => m.id === currentModel)) {
      fmModel.value = currentModel;
    }
  } else {
    fmModel.disabled = true;
  }

  fmModel.classList.toggle("hidden", !catalog?.models?.length);

  refreshFmReasoningOptions(els, catalog, fmModel.value, currentReasoning);
}

export function refreshFmReasoningOptions(
  els: () => { fmReasoning: HTMLSelectElement | null },
  catalog: { models: Array<{ id: string; supports_reasoning: boolean; reasoning_options: string[] }> } | null | undefined,
  modelId: string,
  currentReasoning: string = ""
): void {
  const { fmReasoning } = els();
  if (!fmReasoning) return;

  const model = catalog?.models?.find((m) => m.id === modelId);
  const show = Boolean(
    model?.supports_reasoning && model.reasoning_options?.length
  );
  fmReasoning.classList.toggle("hidden", !show);
  fmReasoning.innerHTML = "";

  if (!show) {
    fmReasoning.disabled = true;
    return;
  }

  fmReasoning.disabled = false;
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "(default)";
  fmReasoning.appendChild(defaultOption);

  for (const r of model.reasoning_options) {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = r;
    fmReasoning.appendChild(opt);
  }
  if (currentReasoning && model.reasoning_options.includes(currentReasoning)) {
    fmReasoning.value = currentReasoning;
  }
}

export function getCatalogForAgent(agent: string): { models: Array<{ id: string; display_name?: string; supports_reasoning: boolean; reasoning_options: string[] }> } | null | undefined {
  return fmModelCatalogs.get(agent);
}

export function buildTicketContent(
  els: () => { content: HTMLTextAreaElement | null; fmAgent: HTMLSelectElement | null; fmModel: HTMLSelectElement | null; fmReasoning: HTMLSelectElement | null; fmProfile: HTMLSelectElement | null; fmDone: HTMLInputElement | null; fmTitle: HTMLInputElement | null },
  getFm: () => FrontmatterState
): string {
  const { content } = els();
  const fm = getFm();
  const body = content?.value || "";

  const lines: string[] = ["---"];

  lines.push(`agent: ${yamlQuote(fm.agent)}`);
  lines.push(`done: ${fm.done}`);
  if (fm.ticketId) lines.push(`ticket_id: ${yamlQuote(fm.ticketId)}`);
  if (fm.title) lines.push(`title: ${yamlQuote(fm.title)}`);
  if (fm.profile) lines.push(`profile: ${yamlQuote(fm.profile)}`);
  if (fm.model) lines.push(`model: ${yamlQuote(fm.model)}`);
  if (fm.reasoning) lines.push(`reasoning: ${yamlQuote(fm.reasoning)}`);

  lines.push("---");
  lines.push("");
  lines.push(body);

  return lines.join("\n");
}
