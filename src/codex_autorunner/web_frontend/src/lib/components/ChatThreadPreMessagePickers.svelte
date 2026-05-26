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
    modeValue = $bindable<'pma' | 'agent'>('pma'),
    scopeOptions = [],
    scopeLocked = false,
    loading = false,
    modelCatalogError = null,
    showAgent = undefined,
    onAgentChange = undefined,
    onPickerChange = undefined,
    onModeChange = undefined,
    onScopeChange = undefined
  }: {
    agents?: PickerRecord[];
    agentValue?: string;
    profileValue?: string;
    models?: PickerRecord[];
    modelValue?: string;
    reasoningValue?: string;
    scopeValue?: string;
    modeValue?: 'pma' | 'agent';
    scopeOptions?: PmaChatScopeOption[];
    /** When true, the scope is fixed by the route ("+ New chat" from a repo/worktree page). */
    scopeLocked?: boolean;
    loading?: boolean;
    modelCatalogError?: string | null;
    showAgent?: boolean;
    onAgentChange?: (() => void) | undefined;
    /** Fires when agent / profile / model / effort controls change (including programmatic binds). */
    onPickerChange?: (() => void) | undefined;
    /** Fires only when the user explicitly changes the new-chat mode picker. */
    onModeChange?: (() => void) | undefined;
    /** Fires only when the user explicitly changes the new-chat scope picker. */
    onScopeChange?: (() => void) | undefined;
  } = $props();

  const scopedOptions = $derived(scopeOptions.filter((scope) => scope.kind !== 'local'));
  const visibleScopeOptions = $derived(modeValue === 'agent' ? scopedOptions : scopeOptions);
  const lockedScopeOption = $derived(scopeOptions.find((scope) => scope.id === scopeValue) ?? null);
  const lockedScopeLabel = $derived(lockedScopeOption?.label ?? scopeValue);

  function selectMode(next: 'pma' | 'agent'): void {
    if (next === 'agent' && scopedOptions.length === 0) return;
    if (modeValue === next) return;
    modeValue = next;
    onModeChange?.();
  }
</script>

<div class="start-picker-row mode-picker-row">
  <span>mode</span>
  <div class="mode-segment" role="radiogroup" aria-label="Chat mode">
    <button
      type="button"
      class:active={modeValue === 'pma'}
      aria-checked={modeValue === 'pma'}
      role="radio"
      onclick={() => selectMode('pma')}
    >
      PMA
    </button>
    <button
      type="button"
      class:active={modeValue === 'agent'}
      aria-checked={modeValue === 'agent'}
      role="radio"
      disabled={scopedOptions.length === 0}
      title={scopedOptions.length === 0 ? 'Add a repo or worktree to start an agent chat' : 'Agent chat'}
      onclick={() => selectMode('agent')}
    >
      Agent
    </button>
  </div>
</div>

{#if scopeLocked}
  <div class="start-picker-row scope-locked-row">
    <span>scope</span>
    <div class="scope-locked-value-wrap">
      <span class="scope-locked-value">{lockedScopeLabel}</span>
      <span
        class="scope-locked-lock"
        aria-label="Scope locked"
        title="Scoped to this repo — open from Chats to change."
      >🔒</span>
    </div>
  </div>
{:else if scopeOptions.length > 1}
  <ChatScopePicker scopeOptions={visibleScopeOptions} bind:value={scopeValue} onChange={onScopeChange} />
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

<style>
  .mode-segment {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 2px;
    min-width: 0;
    padding: 2px;
    border: 1px solid var(--color-border-subtle);
    border-radius: 7px;
    background: var(--color-surface);
  }

  .mode-segment button {
    min-width: 0;
    min-height: 26px;
    padding: 0 var(--space-2);
    border: none;
    border-radius: 5px;
    background: transparent;
    color: var(--color-ink-muted);
    font-size: var(--font-size-1);
    font-weight: 600;
    cursor: pointer;
  }

  .mode-segment button.active {
    background: var(--color-accent-soft);
    color: var(--color-accent);
  }

  .mode-segment button:focus-visible {
    outline: none;
    box-shadow: var(--shadow-focus);
  }

  .mode-segment button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .scope-locked-row .scope-locked-value-wrap {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
    min-height: 30px;
  }

  .scope-locked-value {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--font-size-1);
    color: var(--color-ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  .scope-locked-lock {
    font-size: 11px;
    color: var(--color-ink-faint);
    cursor: help;
    line-height: 1;
  }
</style>
