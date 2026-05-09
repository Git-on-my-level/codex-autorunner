<script lang="ts">
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
  <label class={`${rowClass} ${modelState.state}`}>
    <span>{modelLabelText}</span>
    <select aria-label={modelAriaLabel} bind:value={selectedModel} disabled={modelState.disabled} onchange={handleChange}>
      {#if models.length === 0}
        <option value="">{emptyModelLabel}</option>
      {:else}
        {#if allowEmptyModelOption}
          <option value="">{unsetModelLabel}</option>
        {/if}
        {#each models as model (modelValue(model))}
          <option value={modelValue(model)}>{modelLabel(model)}</option>
        {/each}
        {#if currentModelMissing}
          <option value={selectedModel}>{selectedModel}</option>
        {/if}
      {/if}
    </select>
  </label>
{/if}

{#if showReasoningPicker}
  <label class={rowClass}>
    <span>{reasoningLabelText}</span>
    <select aria-label={reasoningAriaLabel} bind:value={reasoningValue} onchange={handleChange}>
      <option value="">{defaultReasoningLabel}</option>
      {#each reasoningOptions as effort (effort)}
        <option value={effort}>{effort}</option>
      {/each}
    </select>
  </label>
{/if}
