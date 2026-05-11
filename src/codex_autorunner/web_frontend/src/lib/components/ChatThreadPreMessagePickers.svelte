<script lang="ts">
  /**
   * Agent / Hermes profile / model / effort selection for a hub chat thread **before the first
   * message**. This is the only chat surface where runtime profile selection belongs — keep the
   * composer (`<form class="composer">`) free of profile controls so layout stays consistent.
   */
  import AgentModelReasoningPicker from '$lib/components/AgentModelReasoningPicker.svelte';
  import type { PickerRecord } from '$lib/viewModels/modelPickers';

  let {
    agents = [],
    agentValue = $bindable(''),
    profileValue = $bindable(''),
    models = [],
    modelValue = $bindable(''),
    reasoningValue = $bindable(''),
    loading = false,
    modelCatalogError = null,
    showAgent = undefined,
    onAgentChange = undefined,
    onPickerChange = undefined
  }: {
    agents?: PickerRecord[];
    agentValue?: string;
    profileValue?: string;
    models?: PickerRecord[];
    modelValue?: string;
    reasoningValue?: string;
    loading?: boolean;
    modelCatalogError?: string | null;
    showAgent?: boolean;
    onAgentChange?: (() => void) | undefined;
    /** Fires when agent / profile / model / effort controls change (including programmatic binds). */
    onPickerChange?: (() => void) | undefined;
  } = $props();
</script>

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
