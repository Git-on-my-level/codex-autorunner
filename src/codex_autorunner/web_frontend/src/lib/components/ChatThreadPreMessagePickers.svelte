<script lang="ts">
  /**
   * Agent / Hermes profile / model / effort selection for a hub chat thread **before the first
   * message**. This is the only chat surface where runtime profile selection belongs — keep the
   * composer (`<form class="composer">`) free of profile controls so layout stays consistent.
   */
  import AgentModelReasoningPicker from '$lib/components/AgentModelReasoningPicker.svelte';
  import ChatScopePicker from '$lib/components/ChatScopePicker.svelte';
  import type { PickerRecord } from '$lib/viewModels/modelPickers';
  import type { PmaChatScopeOption } from '$lib/viewModels/pmaChat';

  let {
    agents = [],
    agentValue = $bindable(''),
    profileValue = $bindable(''),
    models = [],
    modelValue = $bindable(''),
    reasoningValue = $bindable(''),
    scopeValue = $bindable('local'),
    scopeOptions = [],
    loading = false,
    modelCatalogError = null,
    showAgent = undefined,
    onAgentChange = undefined,
    onPickerChange = undefined,
    onScopeChange = undefined
  }: {
    agents?: PickerRecord[];
    agentValue?: string;
    profileValue?: string;
    models?: PickerRecord[];
    modelValue?: string;
    reasoningValue?: string;
    scopeValue?: string;
    scopeOptions?: PmaChatScopeOption[];
    loading?: boolean;
    modelCatalogError?: string | null;
    showAgent?: boolean;
    onAgentChange?: (() => void) | undefined;
    /** Fires when agent / profile / model / effort controls change (including programmatic binds). */
    onPickerChange?: (() => void) | undefined;
    /** Fires only when the user explicitly changes the new-chat scope picker. */
    onScopeChange?: (() => void) | undefined;
  } = $props();
</script>

{#if scopeOptions.length > 1}
  <ChatScopePicker {scopeOptions} bind:value={scopeValue} onChange={onScopeChange} />
{/if}

<AgentModelReasoningPicker
  {agents}
  bind:agentValue
  bind:profileValue
  bind:modelValue
  bind:reasoningValue
  {models}
  {loading}
  variant="chat"
  enableHermesProfile={true}
  {showAgent}
  {modelCatalogError}
  onAgentChange={onAgentChange}
  onchange={onPickerChange}
/>

