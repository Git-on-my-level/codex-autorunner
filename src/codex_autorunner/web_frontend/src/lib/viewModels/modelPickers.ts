import { agentCapabilityAllowed, modelReasoningOptions, modelSelectorState, type ModelSelectorState } from './pmaChat';

export type PickerRecord = Record<string, unknown>;

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
