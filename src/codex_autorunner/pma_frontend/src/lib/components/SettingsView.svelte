<script lang="ts">
  import type {
    SettingsAgentStatus,
    SettingsSensitiveAction,
    SettingsSessionState,
    SettingsStatusItem,
    SettingsViewModel
  } from '$lib/viewModels/settings';
  import type { SensitiveApprovalRequest } from '$lib/viewModels/domain';
  import SensitiveApprovalCard from './SensitiveApprovalCard.svelte';
  import PageHero from './PageHero.svelte';
  import { untrack } from 'svelte';

  let {
    state: viewState,
    view = null,
    sessionBaselineEpoch = 0,
    errorMessage = null,
    saveError = null,
    pendingAction = null,
    onSessionChange,
    onRequestSensitiveAction,
    onConfirmSensitiveAction,
    onCancelSensitiveAction,
    onApprovalDecision
  }: {
    state: 'loading' | 'error' | 'ready';
    view?: SettingsViewModel | null;
    errorMessage?: string | null;
    saveError?: string | null;
    pendingAction?: SettingsSensitiveAction | null;
    onSessionChange?: (session: SettingsSessionState) => void;
    onRequestSensitiveAction?: (action: SettingsSensitiveAction) => void;
    onConfirmSensitiveAction?: (action: SettingsSensitiveAction) => void;
    onCancelSensitiveAction?: () => void;
    onApprovalDecision?: (approval: SensitiveApprovalRequest, decision: 'approve' | 'decline') => void;
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
      view.session.stopAfterRuns !== baseline.stopAfterRuns
    );
  });

  function patchSession(key: keyof SettingsSessionState, value: string): void {
    if (!view) return;
    onSessionChange?.({ ...view.session, [key]: value });
  }

  function requestSavePreferences(): void {
    if (!view) return;
    onRequestSensitiveAction?.({
      id: 'update-runtime-preferences',
      label: 'Update runtime preferences',
      description: 'Save PMA model and run-limit overrides for future autorunner work.',
      available: true,
      reason: 'Requests explicit approval through /api/session/settings/approvals before writing /api/session/settings.'
    });
  }

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape' && pendingAction) onCancelSensitiveAction?.();
  }
</script>

<svelte:window onkeydown={handleWindowKeydown} />

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
          <div class={view.approvals.length > 0 ? 'waiting' : 'neutral'}>
            <dd>{view.approvals.length}</dd>
            <dt>Pending approvals</dt>
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
      </div>
      <div class="settings-form-footer">
        <p class="permission-note">PMA has full permission for normal coding work. Sensitive CAR operations require approval.</p>
        <button
          type="button"
          class="ghost-button"
          class:dirty={sessionDirty}
          disabled={!sessionDirty}
          onclick={requestSavePreferences}
        >
          {sessionDirty ? 'Save preferences' : 'Saved'}
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

    <section class="settings-section">
      <div class="settings-section-head">
        <h2 class="settings-section-title">Sensitive CAR actions</h2>
        {#if view.approvals.length > 0}
          <span class="status-pill waiting">{view.approvals.length} pending</span>
        {/if}
      </div>
      {#if view.approvals.length === 0}
        <div class="state-panel empty-state compact-empty">
          <strong>No approvals waiting</strong>
          <p>Sensitive CAR requests will appear here when PMA needs an explicit decision.</p>
        </div>
      {:else}
        <div class="settings-approval-list">
          {#each view.approvals as approval (approval.id)}
            <SensitiveApprovalCard {approval} onDecision={onApprovalDecision} />
          {/each}
        </div>
      {/if}
      <div class="sensitive-action-grid">
        {#each view.sensitiveActions as action}
          {#if action.available}
            <article class="sensitive-action available">
              <div>
                <strong>{action.label}</strong>
                <p>{action.description}</p>
                <span>{action.reason}</span>
              </div>
              <button type="button" onclick={() => onRequestSensitiveAction?.(action)}>Review</button>
            </article>
          {/if}
        {/each}
      </div>
      <details class="advanced-panel">
        <summary>Unavailable sensitive actions</summary>
        <div class="sensitive-action-grid">
          {#each view.sensitiveActions as action}
            {#if !action.available}
              <article class="sensitive-action unavailable">
                <div>
                  <strong>{action.label}</strong>
                  <p>{action.description}</p>
                  <span>Unavailable: {action.reason}</span>
                </div>
              </article>
            {/if}
          {/each}
        </div>
      </details>
    </section>

    <details class="settings-section advanced-panel">
      <summary>Advanced / debug</summary>
      {@render statusList(view.advanced)}
    </details>
  {/if}
</section>

{#if pendingAction}
  <div class="modal-backdrop" role="presentation">
    <div class="approval-modal" role="dialog" aria-modal="true" aria-labelledby="settings-approval-title">
      <span class="approval-type">Sensitive settings approval</span>
      <h2 id="settings-approval-title">{pendingAction.label}</h2>
      <p>{pendingAction.description}</p>
      <p class="muted">{pendingAction.reason}</p>
      <div class="approval-actions">
        <button type="button" onclick={() => onCancelSensitiveAction?.()}>Cancel</button>
        <button class="danger-action" type="button" onclick={() => onConfirmSensitiveAction?.(pendingAction)}>
          Request approval
        </button>
      </div>
    </div>
  </div>
{/if}

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
