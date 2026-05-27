<script lang="ts">
  import type {
    SettingsAgentStatus,
    SettingsSessionState
  } from '$lib/viewModels/settings';
  import AutoDismissNotice from '../AutoDismissNotice.svelte';
  import DropdownSelect from '../DropdownSelect.svelte';
  import type { DropdownSelectOption } from '../DropdownSelect';

  let {
    session,
    agents,
    sessionDirty,
    saving,
    saveError,
    onSessionChange,
    onSavePreferences
  }: {
    session: SettingsSessionState;
    agents: SettingsAgentStatus[];
    sessionDirty: boolean;
    saving: boolean;
    saveError: string | null;
    onSessionChange?: (session: SettingsSessionState) => void;
    onSavePreferences?: () => void;
  } = $props();

  const approvalPolicyOptions: DropdownSelectOption[] = [
    { value: '', label: 'Use server default' },
    { value: 'never', label: 'never' },
    { value: 'unlessTrusted', label: 'unlessTrusted' }
  ];
  const sandboxModeOptions: DropdownSelectOption[] = [
    { value: '', label: 'Use server default' },
    { value: 'dangerFullAccess', label: 'dangerFullAccess' },
    { value: 'workspaceWrite', label: 'workspaceWrite' }
  ];
  const workspaceWriteNetworkOptions: DropdownSelectOption[] = [
    { value: '', label: 'Use server default' },
    { value: 'true', label: 'Enabled' },
    { value: 'false', label: 'Disabled' }
  ];

  function patchSession<K extends keyof SettingsSessionState>(key: K, value: SettingsSessionState[K]): void {
    onSessionChange?.({ ...session, [key]: value });
  }

  function patchAgentModel(agentId: string, value: string): void {
    const agent = agentId.trim().toLowerCase();
    const next = { ...session.modelOverrides };
    const model = value.trim();
    if (model) next[agent] = model;
    else delete next[agent];
    onSessionChange?.({ ...session, modelOverrides: next });
  }

  function patchNetwork(value: string): void {
    patchSession('workspaceWriteNetwork', value === '' ? null : value === 'true');
  }

  function selectedAgentModel(agent: SettingsAgentStatus): string {
    const selected = session.modelOverrides[agent.id] ?? '';
    return agent.modelOptions.some((model) => model.id === selected) ? selected : '';
  }

  function agentModelOptions(agent: SettingsAgentStatus): DropdownSelectOption[] {
    return [
      { value: '', label: 'Use built-in default' },
      ...agent.modelOptions.map((model) => ({ value: model.id, label: model.label }))
    ];
  }
</script>

<section class="settings-section">
  <div class="settings-section-head">
    <h2 class="settings-section-title">Runner overrides</h2>
    <button
      type="button"
      class="ghost-button"
      class:dirty={sessionDirty}
      disabled={!sessionDirty || saving}
      onclick={() => onSavePreferences?.()}
    >
      {saving ? 'Saving…' : sessionDirty ? 'Save preferences' : 'Saved'}
    </button>
  </div>
  <div class="settings-form-grid">
    <label>
      <span>Reasoning override</span>
      <input
        value={session.effortOverride}
        placeholder="Use agent default"
        oninput={(event) => patchSession('effortOverride', event.currentTarget.value)}
      />
    </label>
    <label>
      <span>Stop after runs</span>
      <input
        inputmode="numeric"
        value={session.stopAfterRuns}
        placeholder="No limit"
        oninput={(event) => patchSession('stopAfterRuns', event.currentTarget.value)}
      />
    </label>
    <DropdownSelect
      value={session.approvalPolicy}
      options={approvalPolicyOptions}
      labelText="Approval policy"
      ariaLabel="Approval policy"
      onchange={(value) => patchSession('approvalPolicy', value)}
    />
    <DropdownSelect
      value={session.sandboxMode}
      options={sandboxModeOptions}
      labelText="Sandbox mode"
      ariaLabel="Sandbox mode"
      onchange={(value) => patchSession('sandboxMode', value)}
    />
    <DropdownSelect
      value={session.workspaceWriteNetwork === null ? '' : String(session.workspaceWriteNetwork)}
      options={workspaceWriteNetworkOptions}
      labelText="Workspace-write network"
      ariaLabel="Workspace-write network"
      onchange={patchNetwork}
    />
    <label class="checkbox-field">
      <input
        type="checkbox"
        checked={session.ticketFlowRequireCommit}
        onchange={(event) => patchSession('ticketFlowRequireCommit', event.currentTarget.checked)}
      />
      <span>
        <strong>Ticket flow commits</strong>
        <small>Require a git commit before advancing after a completed ticket</small>
      </span>
    </label>
  </div>
  <AutoDismissNotice message={saveError} tone="danger" />
</section>

<section class="settings-section">
  <h2 class="settings-section-title">Agents</h2>
  {#if agents.length === 0}
    <div class="state-panel empty-state compact-empty">
      <strong>No agents visible</strong>
      <p>No agents are visible from the server.</p>
    </div>
  {:else}
    <div class="agent-status-list">
      {#each agents as agent (agent.id)}
        <article class="agent-status-row">
          <div class="agent-status-id">
            <strong>{agent.name}</strong>
          </div>
          {#if agent.modelStatus === 'available'}
            <div class="agent-model-control">
              <DropdownSelect
                value={selectedAgentModel(agent)}
                options={agentModelOptions(agent)}
                labelText="Default model"
                ariaLabel={`${agent.name} default model`}
                searchable={agent.modelOptions.length > 8}
                searchPlaceholder="Search models"
                onchange={(value) => patchAgentModel(agent.id, value)}
              />
            </div>
          {:else if agent.modelStatus === 'unavailable'}
            <span class="agent-model-muted">Models unavailable</span>
          {:else}
            <span class="agent-model-muted">Model selection unavailable</span>
          {/if}
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .agent-status-id strong {
    font-size: var(--font-size-1);
    font-weight: 600;
  }

  .checkbox-field {
    grid-template-columns: auto 1fr;
    align-items: start;
    gap: var(--space-2);
  }

  .checkbox-field input {
    width: 18px;
    height: 18px;
    margin-top: 2px;
    accent-color: var(--color-accent);
  }

  .checkbox-field span {
    gap: 2px;
  }

  .checkbox-field strong {
    color: var(--color-ink);
    font-size: var(--font-size-0);
    font-weight: 600;
  }

  .checkbox-field small {
    color: var(--color-ink-muted);
    font-size: var(--font-size-0);
    line-height: 1.35;
  }
</style>
