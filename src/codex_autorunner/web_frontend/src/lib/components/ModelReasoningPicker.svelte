<script lang="ts">
  import type { DropdownSelectOption } from '$lib/components/DropdownSelect';
  import DropdownSelect from '$lib/components/DropdownSelect.svelte';
  import {
    modelExists,
    modelLabel,
    modelValue,
    pickerModelState,
    pickerReasoningOptions,
    type PickerRecord
  } from '$lib/viewModels/modelPickers';

  let {
    models = [],
    modelValue: selectedModel = $bindable(''),
    reasoningValue = $bindable(''),
    loading = false,
    errorMessage = null,
    rowClass = 'start-picker-row',
    modelLabelText = 'model',
    reasoningLabelText = 'effort',
    modelAriaLabel = 'Model',
    reasoningAriaLabel = 'Effort',
    emptyModelLabel = 'Configured model',
    unsetModelLabel = 'default',
    allowEmptyModelOption = false,
    defaultReasoningLabel = 'Default',
    showModel = true,
    showReasoning = true,
    onchange = undefined
  }: {
    models?: PickerRecord[];
    modelValue?: string;
    reasoningValue?: string;
    loading?: boolean;
    errorMessage?: string | null;
    rowClass?: string;
    modelLabelText?: string;
    reasoningLabelText?: string;
    modelAriaLabel?: string;
    reasoningAriaLabel?: string;
    emptyModelLabel?: string;
    /** Label for the explicit blank model choice when `allowEmptyModelOption` is true. */
    unsetModelLabel?: string;
    /** When true and the catalog is non-empty, prepend a `value=""` option (tickets: hub default model). */
    allowEmptyModelOption?: boolean;
    defaultReasoningLabel?: string;
    showModel?: boolean;
    showReasoning?: boolean;
    onchange?: (() => void) | undefined;
  } = $props();

  const modelState = $derived(pickerModelState(loading, errorMessage, models));
  const reasoningOptions = $derived(pickerReasoningOptions(models, selectedModel));
  const showReasoningPicker = $derived(Boolean(showReasoning && reasoningOptions.length > 0));
  const currentModelMissing = $derived(Boolean(selectedModel) && !modelExists(models, selectedModel));
  const modelOptions = $derived.by<DropdownSelectOption[]>(() => {
    if (models.length === 0) return [{ value: '', label: emptyModelLabel }];
    const entries: DropdownSelectOption[] = [];
    if (allowEmptyModelOption) entries.push({ value: '', label: unsetModelLabel });
    entries.push(...models.map((model) => ({ value: modelValue(model), label: modelLabel(model) })));
    if (currentModelMissing) entries.push({ value: selectedModel, label: selectedModel });
    return entries;
  });
  const effortOptions = $derived<DropdownSelectOption[]>([
    { value: '', label: defaultReasoningLabel },
    ...reasoningOptions.map((effort) => ({ value: effort, label: effort }))
  ]);

  $effect(() => {
    if (reasoningValue && !reasoningOptions.includes(reasoningValue)) {
      reasoningValue = '';
    }
  });

  function handleChange(): void {
    if (reasoningValue && !reasoningOptions.includes(reasoningValue)) reasoningValue = '';
    onchange?.();
  }
</script>

{#if showModel}
  <DropdownSelect
    bind:value={selectedModel}
    options={modelOptions}
    labelText={modelLabelText}
    ariaLabel={modelAriaLabel}
    rowClass={`${rowClass} ${modelState.state}`}
    disabled={modelState.disabled}
    searchable={modelOptions.length > 8}
    searchPlaceholder="Search models"
    onchange={handleChange}
  />
{/if}

{#if showReasoningPicker}
  <DropdownSelect
    bind:value={reasoningValue}
    options={effortOptions}
    labelText={reasoningLabelText}
    ariaLabel={reasoningAriaLabel}
    rowClass={rowClass}
    onchange={handleChange}
  />
{/if}
