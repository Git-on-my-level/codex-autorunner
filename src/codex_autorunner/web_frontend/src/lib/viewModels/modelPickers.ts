import { agentCapabilityAllowed, modelReasoningOptions, modelSelectorState, type ModelSelectorState } from './chat';
import type { ChatSummary } from './domain';

export type PickerRecord = Record<string, unknown>;

/** Selector state when switching chats or applying user defaults (no chat-bound agent). */
export type ChatResolvedSelectors =
  | { mode: 'defaults'; agentId: string; agentProfile: string; reasoning: string }
  | {
      mode: 'chat-bound';
      agentId: string;
      agentProfile: string;
      reasoning: string;
      model: string | null;
    };

/** Match `+page` / `loadInitialSupportingData` default-agent path: hub defaults + first agent fallback. */
export function resolveChatSelectorsForActiveChat(
  chat: ChatSummary | null | undefined,
  agents: PickerRecord[],
  configuredDefaultAgentId: string | undefined,
  configuredDefaultProfile: string
): ChatResolvedSelectors {
  if (!chat?.agentId) {
    if (agents.length === 0) {
      return { mode: 'defaults', agentId: '', agentProfile: '', reasoning: '' };
    }
    const configuredDefault = configuredDefaultAgentId
      ? agents.find((record) => agentId(record) === configuredDefaultAgentId)
      : undefined;
    const pick = configuredDefault ? agentId(configuredDefault) : agentId(agents[0]!);
    let agentProfile = '';
    if (pick === 'hermes' && configuredDefaultProfile.trim()) {
      agentProfile = configuredDefaultProfile;
    }
    return { mode: 'defaults', agentId: pick, agentProfile, reasoning: '' };
  }
  return {
    mode: 'chat-bound',
    agentId: chat.agentId,
    agentProfile: chat.agentProfile ?? '',
    reasoning: stringField(chat.raw, 'reasoning') ?? '',
    model: chat.model ?? null
  };
}

export function stringField(record: PickerRecord | undefined | null, key: string): string | null {
  const value = record?.[key];
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

export function agentId(agent: PickerRecord): string {
  return stringField(agent, 'id') ?? stringField(agent, 'agent') ?? agentLabel(agent);
}

export function agentLabel(agent: PickerRecord): string {
  return stringField(agent, 'label') ?? stringField(agent, 'name') ?? stringField(agent, 'id') ?? 'Agent';
}

export function agentRecordForId(agents: PickerRecord[], id: string): PickerRecord | null {
  if (!id) return null;
  return agents.find((entry) => agentId(entry) === id) ?? null;
}

/** Chat list / compact rows: friendly agent label when catalog is loaded, else raw id. */
export function agentDisplayForChat(
  agents: PickerRecord[],
  chat: { agentId: string | null }
): string {
  if (!chat.agentId) return '';
  const rec = agentRecordForId(agents, chat.agentId);
  return rec ? agentLabel(rec) : chat.agentId;
}

export function agentCanListModels(agent: PickerRecord | null): boolean {
  return agentCapabilityAllowed(agent, 'list_models');
}

export function modelValue(model: PickerRecord): string {
  return stringField(model, 'id') ?? stringField(model, 'model') ?? modelLabel(model);
}

export function modelLabel(model: PickerRecord): string {
  return stringField(model, 'label') ?? stringField(model, 'name') ?? stringField(model, 'id') ?? stringField(model, 'model') ?? 'Model';
}

export function modelRecordForValue(catalog: PickerRecord[], value: string): PickerRecord | null {
  if (!value) return null;
  return catalog.find((entry) => modelValue(entry) === value) ?? null;
}

export function modelExists(catalog: PickerRecord[], value: string): boolean {
  return Boolean(modelRecordForValue(catalog, value));
}

export function firstModelValue(catalog: PickerRecord[]): string {
  return catalog[0] ? modelValue(catalog[0]) : '';
}

export function pickerModelState(
  loading: boolean,
  errorMessage: string | null,
  catalog: PickerRecord[]
): ModelSelectorState {
  return modelSelectorState(loading, errorMessage, catalog.length);
}

export function pickerReasoningOptions(catalog: PickerRecord[], selectedModel: string): string[] {
  return modelReasoningOptions(modelRecordForValue(catalog, selectedModel));
}

export type AgentModelSelectionRequest = {
  agents: PickerRecord[];
  agentId: string;
  catalog?: PickerRecord[];
  preferredModel?: string | null;
  rememberedModel?: string | null;
  currentModel?: string | null;
  currentReasoning?: string | null;
  keepReasoning?: boolean;
  allowEmptyModel?: boolean;
};

export type AgentModelSelection = {
  canListModels: boolean;
  model: string;
  reasoning: string;
};

export function resolveAgentModelSelection(request: AgentModelSelectionRequest): AgentModelSelection {
  if (!agentCanListModels(agentRecordForId(request.agents, request.agentId))) {
    return { canListModels: false, model: '', reasoning: '' };
  }
  const catalog = request.catalog ?? [];
  const candidates = [request.preferredModel, request.rememberedModel, request.currentModel]
    .map((value) => (typeof value === 'string' ? value.trim() : ''))
    .filter(Boolean);
  let model = '';
  for (const candidate of candidates) {
    if (modelExists(catalog, candidate)) {
      model = candidate;
      break;
    }
  }
  if (!model && !request.allowEmptyModel) model = firstModelValue(catalog);
  const reasoningOptions = pickerReasoningOptions(catalog, model);
  const currentReasoning = typeof request.currentReasoning === 'string' ? request.currentReasoning.trim() : '';
  const reasoning = request.keepReasoning && currentReasoning && reasoningOptions.includes(currentReasoning) ? currentReasoning : '';
  return { canListModels: true, model, reasoning };
}

export type AgentProfileOption = { id: string; label: string };

/** Hermes profile rows from `/hub/pma/agents` catalog (`profiles[].id` / `display_name`). */
export function agentProfileEntriesForRecord(agent: PickerRecord | null): AgentProfileOption[] {
  if (!agent || agentId(agent) !== 'hermes') return [];
  const raw = agent.profiles;
  if (!Array.isArray(raw)) return [];
  const out: AgentProfileOption[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    const rec = entry as Record<string, unknown>;
    const id = stringField(rec, 'id');
    if (!id) continue;
    const label =
      stringField(rec, 'display_name') ??
      stringField(rec, 'displayName') ??
      stringField(rec, 'label') ??
      id;
    out.push({ id, label });
  }
  out.sort((left, right) => left.id.localeCompare(right.id));
  return out;
}
