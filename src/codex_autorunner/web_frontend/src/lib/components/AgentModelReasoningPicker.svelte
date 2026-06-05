<script lang="ts">
  /**
   * Shared agent + optional Hermes profile + model + reasoning ("effort") rows.
   *
   * **Hub chat threads:** before the first message, render via `ChatThreadPreMessagePickers`
   * in the transcript stack only — do not embed profile (or duplicate these rows) inside the
  * chat composer form.
  */
  import ModelReasoningPicker from '$lib/components/ModelReasoningPicker.svelte';
  import type { DropdownSelectOption } from '$lib/components/DropdownSelect';
  import DropdownSelect from '$lib/components/DropdownSelect.svelte';
  import {
    agentCanListModels,
    agentId,
    agentLabel,
    agentProfileEntriesForRecord,
    agentRecordForId,
    type PickerRecord
  } from '$lib/viewModels/modelPickers';

  type Variant = 'chat' | 'ticket';

  let {
    agents = [],
    fallbackAgentIds = [],
    agentValue = $bindable(''),
    profileValue = $bindable(''),
    models = [],
    modelValue = $bindable(''),
    reasoningValue = $bindable(''),
    loading = false,
    modelCatalogError = null,
    variant = 'chat',
    allowEmptyModelOption = false,
    unsetModelLabel = 'default',
    emptyModelLabel = 'Configured model',
    defaultReasoningLabel = undefined,
    showAgent = undefined,
    enableHermesProfile = true,
    onAgentChange = undefined,
    onchange = undefined
  }: {
    agents?: PickerRecord[];
    fallbackAgentIds?: string[];
    agentValue?: string;
    profileValue?: string;
    models?: PickerRecord[];
    modelValue?: string;
    reasoningValue?: string;
    loading?: boolean;
    modelCatalogError?: string | null;
    variant?: Variant;
    allowEmptyModelOption?: boolean;
    unsetModelLabel?: string;
    emptyModelLabel?: string;
    defaultReasoningLabel?: string;
    showAgent?: boolean;
    /** When false, suppress Hermes profile dropdown (e.g. ticket settings until profile is persisted). */
    enableHermesProfile?: boolean;
    onAgentChange?: (() => void) | undefined;
    onchange?: (() => void) | undefined;
  } = $props();

  const rowClass = $derived(variant === 'ticket' ? 'ticket-inline-field' : 'start-picker-row');
  const agentSpanLabel = $derived(variant === 'ticket' ? 'Agent' : 'agent');
  const profileSpanLabel = $derived(variant === 'ticket' ? 'Profile' : 'profile');
  const modelLabelText = $derived(variant === 'ticket' ? 'Model' : 'model');
  const reasoningLabelText = $derived(variant === 'ticket' ? 'Reasoning' : 'effort');
  const reasoningAriaLabel = $derived(variant === 'ticket' ? 'Reasoning' : 'Effort');
  const resolvedDefaultReasoningLabel = $derived(defaultReasoningLabel ?? (variant === 'ticket' ? 'default' : 'Default'));

  const agentPickerEntries = $derived.by(() => {
    if (agents.length > 0) {
      return agents.map((rec) => ({ id: agentId(rec), label: agentLabel(rec) }));
    }
    return fallbackAgentIds.map((id) => ({ id, label: id }));
  });
  const agentOptions = $derived.by<DropdownSelectOption[]>(() => {
    return agentPickerEntries.map((entry) => ({ value: entry.id, label: entry.label }));
  });

  const resolvedShowAgent = $derived(showAgent ?? agentPickerEntries.length > 0);

  const selectedAgentRecord = $derived(agentRecordForId(agents, agentValue));
  const selectedAgentCanListModels = $derived(agentCanListModels(selectedAgentRecord));
  const showModelSelector = $derived(
    Boolean(selectedAgentCanListModels && (loading || models.length > 0 || Boolean(modelCatalogError)))
  );

  const profilePickerEntries = $derived(agentProfileEntriesForRecord(selectedAgentRecord));
  const profileOptions = $derived.by<DropdownSelectOption[]>(() => {
    const entries: DropdownSelectOption[] = [{ value: '', label: 'Default' }];
    if (profileValue && !profilePickerEntries.some((entry) => entry.id === profileValue)) {
      entries.push({ value: profileValue, label: profileValue });
    }
    entries.push(...profilePickerEntries.map((entry) => ({ value: entry.id, label: entry.label })));
    return entries;
  });
  const showHermesProfilePicker = $derived.by(() => {
    if (!enableHermesProfile) return false;
    if ((agentValue || '').toLowerCase() !== 'hermes') return false;
    if (profilePickerEntries.length > 0) return true;
    return Boolean(profileValue.trim());
  });

  $effect(() => {
    if ((agentValue || '').toLowerCase() !== 'hermes') {
      profileValue = '';
    }
  });

  function handleAgentSelectChange(): void {
    onAgentChange?.();
    onchange?.();
  }

  function handleModelReasoningChange(): void {
    onchange?.();
  }

  function handleProfileChange(): void {
    onchange?.();
  }
</script>

{#if resolvedShowAgent}
  <DropdownSelect
    bind:value={agentValue}
    options={agentOptions}
    labelText={agentSpanLabel}
    ariaLabel="Agent"
    rowClass={rowClass}
    searchable={agentOptions.length > 8}
    searchPlaceholder="Search agents"
    onchange={handleAgentSelectChange}
  />
{/if}

{#if showHermesProfilePicker}
  <DropdownSelect
    bind:value={profileValue}
    options={profileOptions}
    labelText={profileSpanLabel}
    ariaLabel="Hermes profile"
    rowClass={rowClass}
    searchable={profileOptions.length > 8}
    searchPlaceholder="Search profiles"
    onchange={handleProfileChange}
  />
{/if}

<ModelReasoningPicker
  bind:modelValue
  bind:reasoningValue
  {models}
  {loading}
  errorMessage={modelCatalogError}
  {rowClass}
  {modelLabelText}
  {reasoningLabelText}
  modelAriaLabel="Model"
  {reasoningAriaLabel}
  {emptyModelLabel}
  {unsetModelLabel}
  {allowEmptyModelOption}
  defaultReasoningLabel={resolvedDefaultReasoningLabel}
  showModel={showModelSelector}
  showReasoning={showModelSelector}
  onchange={handleModelReasoningChange}
/>
