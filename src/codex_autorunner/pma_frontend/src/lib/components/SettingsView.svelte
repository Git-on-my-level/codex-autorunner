<script lang="ts">
  import type {
    SettingsAgentStatus,
    SettingsSessionState,
    SettingsStatusItem,
    SettingsViewModel
  } from '$lib/viewModels/settings';
  import PageHero from './PageHero.svelte';
  import { untrack } from 'svelte';

  let {
    state: viewState,
    view = null,
    sessionBaselineEpoch = 0,
    errorMessage = null,
    saveError = null,
    saving = false,
    onSessionChange,
    onSavePreferences
  }: {
    state: 'loading' | 'error' | 'ready';
    view?: SettingsViewModel | null;
    errorMessage?: string | null;
    saveError?: string | null;
    saving?: boolean;
    onSessionChange?: (session: SettingsSessionState) => void;
    onSavePreferences?: () => void;
    sessionBaselineEpoch?: number;
  } = $props();

  let savedSession: SettingsSessionState | null = $state(null);

  $effect(() => {
    sessionBaselineEpoch;
    const snapshot = untrack(() => view);
    if (snapshot) savedSession = { ...snapshot.session };
  });

  const sessionDirty = $derived.by(() => {
    if (!view || !savedSession) return false;
    const baseline = savedSession;
    return (
      view.session.modelOverride !== baseline.modelOverride ||
      view.session.effortOverride !== baseline.effortOverride ||
      view.session.stopAfterRuns !== baseline.stopAfterRuns ||
      view.session.approvalPolicy !== baseline.approvalPolicy ||
      view.session.sandboxMode !== baseline.sandboxMode ||
      view.session.workspaceWriteNetwork !== baseline.workspaceWriteNetwork
    );
  });

  function patchSession(key: keyof SettingsSessionState, value: SettingsSessionState[keyof SettingsSessionState]): void {
    if (!view) return;
    onSessionChange?.({ ...view.session, [key]: value });
  }

  function patchNetwork(value: string): void {
    patchSession('workspaceWriteNetwork', value === '' ? null : value === 'true');
  }
</script>

<section class="page-stack settings-page">
  <PageHero
    title="Settings"
    subtitle="Hub status, agent readiness, attachments, and sensitive CAR actions."
  >
    {#snippet stats()}
      {#if view}
        <dl class="hero-stats" aria-label="Settings summary">
          <div>
            <dd>{view.hub.find((item) => item.label.toLowerCase().includes('mode'))?.value ?? 'Local'}</dd>
            <dt>Hub mode</dt>
          </div>
          <div class="neutral">
            <dd>{sessionDirty ? 'Unsaved' : 'Saved'}</dd>
            <dt>Preferences</dt>
          </div>
        </dl>
      {/if}
    {/snippet}
  </PageHero>

  {#if viewState === 'loading'}
    <div class="state-panel">Loading settings...</div>
  {:else if viewState === 'error'}
    <div class="state-panel error">Could not load settings. {errorMessage}</div>
  {:else if view}
    <section class="settings-section">
      <h2 class="settings-section-title">Hub</h2>
      {@render statusList(view.hub)}
    </section>

    <section class="settings-section">
      <h2 class="settings-section-title">PMA agent &amp; model</h2>
      <div class="settings-form-grid">
        <label>
          <span>Model override</span>
          <input
            value={view.session.modelOverride}
            placeholder="Use server default"
            oninput={(event) => patchSession('modelOverride', event.currentTarget.value)}
          />
        </label>
        <label>
          <span>Reasoning effort</span>
          <input
            value={view.session.effortOverride}
            placeholder="Use server default"
            oninput={(event) => patchSession('effortOverride', event.currentTarget.value)}
          />
        </label>
        <label>
          <span>Stop after runs</span>
          <input
            inputmode="numeric"
            value={view.session.stopAfterRuns}
            placeholder="No limit"
            oninput={(event) => patchSession('stopAfterRuns', event.currentTarget.value)}
          />
        </label>
        <label>
          <span>Approval policy</span>
          <select
            value={view.session.approvalPolicy}
            onchange={(event) => patchSession('approvalPolicy', event.currentTarget.value)}
          >
            <option value="">Use server default</option>
            <option value="never">never</option>
            <option value="unlessTrusted">unlessTrusted</option>
          </select>
        </label>
        <label>
          <span>Sandbox mode</span>
          <select
            value={view.session.sandboxMode}
            onchange={(event) => patchSession('sandboxMode', event.currentTarget.value)}
          >
            <option value="">Use server default</option>
            <option value="dangerFullAccess">dangerFullAccess</option>
            <option value="workspaceWrite">workspaceWrite</option>
          </select>
        </label>
        <label>
          <span>Workspace-write network</span>
          <select
            value={view.session.workspaceWriteNetwork === null ? '' : String(view.session.workspaceWriteNetwork)}
            onchange={(event) => patchNetwork(event.currentTarget.value)}
          >
            <option value="">Use server default</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </select>
        </label>
      </div>
      <div class="settings-form-footer">
        <p class="permission-note">Agent-native approvals apply during agent turns according to the selected policy and sandbox mode.</p>
        <button
          type="button"
          class="ghost-button"
          class:dirty={sessionDirty}
          disabled={!sessionDirty || saving}
          onclick={() => onSavePreferences?.()}
        >
          {saving ? 'Saving' : sessionDirty ? 'Save preferences' : 'Saved'}
        </button>
      </div>
      {#if saveError}
        <p class="compose-error">{saveError}</p>
      {/if}
      <h3 class="settings-subtitle">PMA-capable agents</h3>
      {@render agentList(view.pmaAgents, 'No PMA-capable agents are visible from the server.')}
      <h3 class="settings-subtitle">Coding agents</h3>
      {@render agentList(view.codingAgents, 'No additional coding agents are visible from the server.')}
    </section>

    <div class="settings-grid">
      <section class="settings-section">
        <h2 class="settings-section-title">Integrations</h2>
        {@render statusList(view.integrations)}
      </section>

      <section class="settings-section">
        <h2 class="settings-section-title">Attachments</h2>
        {@render statusList(view.filebox)}
      </section>

      <section class="settings-section">
        <h2 class="settings-section-title">Secrets</h2>
        {@render statusList(view.secrets)}
      </section>
    </div>

    <details class="settings-section advanced-panel">
      <summary>Advanced / debug</summary>
      {@render statusList(view.advanced)}
    </details>
  {/if}
</section>

{#snippet statusList(items: SettingsStatusItem[], compact = false)}
  <dl class:compact class="settings-status-list">
    {#each items as item}
      <div class={item.tone}>
        <dt>{item.label}</dt>
        <dd>{item.value}</dd>
      </div>
    {/each}
  </dl>
{/snippet}

{#snippet agentList(agents: SettingsAgentStatus[], emptyText: string)}
  {#if agents.length === 0}
    <div class="state-panel empty-state compact-empty">
      <strong>No agents visible</strong>
      <p>{emptyText}</p>
    </div>
  {:else}
    <div class="agent-status-list">
      {#each agents as agent}
        <article class="agent-status-row">
          <div>
            <strong>{agent.name}</strong>
            <span>{agent.id} · {agent.providerLabel}</span>
          </div>
          <span class={`status-pill ${agent.modelStatus === 'available' ? 'done' : agent.modelStatus === 'unavailable' ? 'waiting' : 'idle'}`}>
            {agent.modelLabel}
          </span>
        </article>
      {/each}
    </div>
  {/if}
{/snippet}
