<script lang="ts">
  import ModelReasoningPicker from '$lib/components/ModelReasoningPicker.svelte';
  import {
    agentCanListModels,
    agentId,
    agentLabel,
    agentRecordForId,
    type PickerRecord
  } from '$lib/viewModels/modelPickers';

  type Variant = 'chat' | 'ticket';

  let {
    agents = [],
    fallbackAgentIds = [],
    agentValue = $bindable(''),
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
    onAgentChange = undefined,
    onchange = undefined
  }: {
    agents?: PickerRecord[];
    fallbackAgentIds?: string[];
    agentValue?: string;
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
    onAgentChange?: (() => void) | undefined;
    onchange?: (() => void) | undefined;
  } = $props();

  const rowClass = $derived(variant === 'ticket' ? 'ticket-inline-field' : 'start-picker-row');
  const agentSpanLabel = $derived(variant === 'ticket' ? 'Agent' : 'agent');
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

  const resolvedShowAgent = $derived(showAgent ?? agentPickerEntries.length > 0);

  const selectedAgentRecord = $derived(agentRecordForId(agents, agentValue));
  const selectedAgentCanListModels = $derived(agentCanListModels(selectedAgentRecord));
  const showModelSelector = $derived(
    Boolean(selectedAgentCanListModels && (loading || models.length > 0 || Boolean(modelCatalogError)))
  );

  function handleAgentSelectChange(): void {
    onAgentChange?.();
    onchange?.();
  }

  function handleModelReasoningChange(): void {
    onchange?.();
  }
</script>

{#if resolvedShowAgent}
  <label class={rowClass}>
    <span>{agentSpanLabel}</span>
    <select aria-label="Agent" bind:value={agentValue} onchange={handleAgentSelectChange}>
      {#if agentValue && !agentPickerEntries.some((entry) => entry.id === agentValue)}
        <option value={agentValue}>{agentValue}</option>
      {/if}
      {#each agentPickerEntries as entry (entry.id)}
        <option value={entry.id}>{entry.label}</option>
      {/each}
    </select>
  </label>
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
